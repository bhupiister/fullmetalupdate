# -*- coding: utf-8 -*-

from datetime import datetime, timedelta
import os
import os.path
import re
import logging
import json
from threading import Lock, Thread
import socket as s
import subprocess
import asyncio
import shutil
from urllib.parse import parse_qs, urlparse
import async_timeout

import fullmetalupdate.updater as updater_paths
from fullmetalupdate.updater import AsyncUpdater
from rauc_hawkbit.ddi.client import DDIClient, APIError
from rauc_hawkbit.ddi.client import (
    ConfigStatusExecution, ConfigStatusResult)
from rauc_hawkbit.ddi.deployment_base import (
    DeploymentStatusExecution, DeploymentStatusResult)
from rauc_hawkbit.ddi.cancel_action import (
    CancelStatusExecution, CancelStatusResult)
from aiohttp.client_exceptions import ClientOSError, ClientResponseError

PATH_REBOOT_DATA = '/var/local/fullmetalupdate/reboot_data.json'
DIR_NOTIFY_SOCKET = '/tmp/fullmetalupdate/'
UPDATE_GAP_PERCENT = 10


class HawkbitManagementClient(object):
    def __init__(self, session, host, ssl, bearer_token, timeout=10):
        self.session = session
        self.host = host
        self.ssl = ssl
        self.bearer_token = bearer_token
        self.timeout = timeout
        self.logger = logging.getLogger('fullmetalupdate_hawkbit')

    def build_api_url(self, api_path):
        if api_path.startswith('http://') or api_path.startswith('https://'):
            return api_path

        protocol = 'https' if self.ssl else 'http'
        return '{protocol}://{host}/{api_path}'.format(
            protocol=protocol, host=self.host, api_path=api_path.lstrip('/'))

    def _headers(self):
        return {
            'Accept': 'application/json',
            'Authorization': 'Bearer {}'.format(self.bearer_token),
        }

    async def get_resource(self, api_path):
        url = self.build_api_url(api_path)
        self.logger.debug('Management API GET {}'.format(url))

        with async_timeout.timeout(self.timeout):
            async with self.session.get(url, headers=self._headers()) as resp:
                if resp.status != 200:
                    error_description = await resp.text()
                    if error_description:
                        self.logger.debug('Management API error: {}'.format(error_description))
                    raise APIError('Management API {status}: {reason}'.format(
                        status=resp.status, reason=resp.reason))

                return await resp.json()

    async def get_distribution_sets(self):
        distribution_sets = []
        page_size = 100
        offset = 0
        next_page = '/rest/v1/distributionsets?offset=0&limit={}'.format(
            page_size)

        while next_page:
            page = await self.get_resource(next_page)
            content = page.get('content', [])
            distribution_sets.extend(content)

            if not content:
                break

            total = page.get('total')
            if total is not None and len(distribution_sets) >= total:
                break

            next_link = page.get('_links', {}).get('next')
            next_page = (next_link.get('href')
                         if isinstance(next_link, dict) else None)

            if not next_page:
                if len(content) < page_size:
                    break
                offset += 1
                next_page = (
                    '/rest/v1/distributionsets?offset={}&limit={}'.format(
                        offset, page_size))

        return distribution_sets

    async def get_distribution_set_id(self, target_id, action_id):
        action = await self.get_resource(
            '/rest/v1/targets/{targetId}/actions/{actionId}'.format(
                targetId=target_id,
                actionId=action_id))

        distributionset = action.get('_links', {}).get('distributionset')
        if not distributionset or 'href' not in distributionset:
            raise APIError('Management API action has no distributionset link')

        distribution_set_id = urlparse(distributionset['href']).path.rstrip('/').rsplit('/', 1)[-1]
        if not distribution_set_id:
            raise APIError('Management API action has an invalid distributionset link')

        try:
            return int(distribution_set_id)
        except ValueError:
            raise APIError(
                'Management API action has an invalid distributionset ID')

    async def get_distribution_set(self, distribution_set_id):
        return await self.get_resource(
            '/rest/v1/distributionsets/{distributionSetId}'.format(
                distributionSetId=distribution_set_id))

    async def get_distribution_set_metadata(self, distribution_set_id):
        metadata = await self.get_resource(
            '/rest/v1/distributionsets/{distributionSetId}/metadata?offset=0&limit=100'.format(
                distributionSetId=distribution_set_id))
        return {
            entry['key']: entry.get('value')
            for entry in metadata.get('content', [])
        }


class FullMetalUpdateDDIClient(AsyncUpdater):
    """
    Client broker communicating via DBUS and HawkBit DDI HTTP interface. Inherits from AsyncUpdater Class.

    :param logging logger: Logger used to print information regarding the update proceedings or to report errors.
    :param DDIClient ddi: Client enabling easy GET / POST / PUT request to Hawkbit Server.
    :param int action_id: Unique identifier of an Hawkbit update.
    :param list feedbackThreads: List of feedback threads to make accesses easier.
    :param dictionnary feedbackResults: Each feedback thread is associated with a status_update and a message, which describes if
        the starting process of the associated container went well or not.
        feedbackResults =  {container-sd-notify : {status_result : ... , msg : ...}}
    :param Lock mutexResults: Mutex that protects feedbackResults from concurrent accesses (accesses from main thread and accesses
        from feedback threads). 
    """

    def __init__(self, session, host, ssl, tenant_id, target_name, auth_token,
                 attributes, dev_mode=False, dev_state_dir=None,
                 management_client=None):
        """ Constructor of FullMetalUpdateDDIClient Class.
        """
        if dev_mode:
            self._configure_client_dev_paths(dev_state_dir)

        super(FullMetalUpdateDDIClient, self).__init__(dev_mode, dev_state_dir)

        self.attributes = attributes

        self.logger = logging.getLogger('fullmetalupdate_hawkbit')
        self.ddi = DDIClient(session, host, ssl, auth_token, tenant_id, target_name)
        self.management = management_client
        self.action_id = None
        self.feedbackThreads = []
        self.feedbackResults = None
        self.mutexResults = Lock()

        os.makedirs(os.path.dirname(PATH_REBOOT_DATA), exist_ok=True)
        os.makedirs(DIR_NOTIFY_SOCKET, exist_ok=True)

    def _configure_client_dev_paths(self, dev_state_dir):
        global PATH_REBOOT_DATA, DIR_NOTIFY_SOCKET

        if not dev_state_dir:
            dev_state_dir = os.path.join(os.getcwd(), ".fmu-dev")

        dev_state_dir = os.path.abspath(dev_state_dir)
        PATH_REBOOT_DATA = os.path.join(dev_state_dir, "reboot_data.json")
        DIR_NOTIFY_SOCKET = os.path.join(dev_state_dir, "notify")

    def _get_installed_update_versions(self):
        versions = {
            'os_version': None,
            'apps_version': None,
        }

        try:
            result = subprocess.run(
                ['sysinfo'],
                check=True,
                capture_output=True,
                text=True)
        except (OSError, subprocess.CalledProcessError) as e:
            self.logger.error(
                "Cannot read installed update versions using sysinfo: {}".format(
                    e))
            return versions

        version_labels = {
            'Build Version:': 'os_version',
            'Apps Version:': 'apps_version',
        }

        for line in result.stdout.splitlines():
            line = line.strip()
            for label, key in version_labels.items():
                if line.startswith(label) and versions[key] is None:
                    versions[key] = line[len(label):].strip() or None

        return versions

    def _parse_update_info(self, value):
        if isinstance(value, str):
            self.logger.debug("Raw update info metadata: %r", value)
            info = json.loads(value, parse_float=float, parse_int=int)
        elif isinstance(value, (dict, list)):
            self.logger.debug("Update info metadata is already parsed: %r", value)
            info = value
        else:
            raise TypeError("Unsupported update info metadata type: {}".format(type(value).__name__))

        self.logger.debug("Parsed update info metadata: %r", info)
        return info

    async def _get_distribution_sets(self):
        if not self.management:
            return []

        return await self.management.get_distribution_sets()

    async def _add_metadata_to_distribution_sets(self, distribution_sets):
        if not self.management:
            return distribution_sets

        for distribution_set in distribution_sets:
            distribution_set['metadata'] = (
                await self.management.get_distribution_set_metadata(
                    distribution_set['id']))

        return distribution_sets

    async def _get_distribution_set_info(self, distribution_set_id):
        if not self.management:
            return None

        metadata = await self.management.get_distribution_set_metadata(distribution_set_id)

        if 'info' in metadata:
            self.logger.info("Using Distribution Set metadata 'info'")
            return self._parse_update_info(metadata['info'])

        return None

    def _check_partition_space_mb(self, partition_path):
        try:
            disk_usage = shutil.disk_usage(partition_path)
        except FileNotFoundError:
            self.logger.error("Cannot check free space: partition path {} does not exist".format(partition_path))
            return 0
        except OSError as e:
            self.logger.error("Cannot check free space on partition {}: {}".format(partition_path, e))
            return 0

        free_space_mb = disk_usage.free / 1024 / 1024
        self.logger.info(
            "Partition %s free space: %.2f MB available",
            partition_path,
            free_space_mb
        )

        return free_space_mb

    async def start_polling(self, wait_on_error=60):
        """ 
        Wrapper around self.poll_base_resource() for exception handling.
        
        :param int wait_on_error: Timeout before retry on polling failled
        """

        while True:
            try:
                await self.poll_base_resource()
            except asyncio.CancelledError:
                self.logger.info('Polling cancelled')
                break
            except asyncio.TimeoutError:
                self.logger.warning('Polling failed due to TimeoutError')
            except (APIError, TimeoutError, ClientOSError, ClientResponseError) as e:
                # log error and start all over again
                self.logger.warning('Polling failed with a temporary error: {}'.format(e))
            except Exception:
                self.logger.exception('Polling failed with an unexpected exception:')
            self.action_id = None
            self.logger.info('Retry will happen in {} seconds'.format(
                wait_on_error))
            await asyncio.sleep(wait_on_error)

    async def identify(self):
        """
        Identify target against HawkBit.
        """

        self.logger.info('Sending identifying information to HawkBit')
        # identify
        await self.ddi.configData(ConfigStatusExecution.closed,
                                  ConfigStatusResult.success, **self.attributes)

    async def cancel(self, base):
        """
        Acknoledges cancelation request, retrives ID of Hawkbit update to be cancelled, cancels the relevant Hawkbit update 
        and finally notify the Hawkbit server about the result of the cancelation process.

        TODO : Implement Hawkbit Update cancelation (this method does not seem to do what it is meant to do)
        
        :param dictionnary base: Dictionnary storing information about a Hawkbit update.
        """
        self.logger.info('Received cancelation request')
        # retrieve action id from URL
        deployment = base['_links']['cancelAction']['href']
        match = re.search('/cancelAction/(.+)$', deployment)
        action_id, = match.groups()
        # retrieve stop_id
        stop_info = await self.ddi.cancelAction[action_id]()
        stop_id = stop_info['cancelAction']['stopId']
        # Reject cancel request
        self.logger.info('Rejecting cancelation request')
        await self.ddi.cancelAction[stop_id].feedback(
            CancelStatusExecution.rejected,
            CancelStatusResult.success,
            status_details=("Cancelling not supported",))

    async def process_deployment(self, base):
        """
        This method performs a Hawkbit update in several steps :
            - Retrieves information about the Hawkbit update based on base dictionnary ;
            - Notifies Hawkbit server about the appropriate start of the update ;
            - All chunks are then parsed and processed, ie 
                1) OS chunks cause a system update (see update_system method) and a system reboot ;
                2) Apps chunks cause apps updates (see update_container method). An app / container that implements the notify feature of systemd is 
                    associated with a feedback thread, which monitors its execution and feedbacks the FMU client if the app succesfully started or not;
            - Systemd dependency tree is regenerated in order to take into account every change in service files (new service files or updated
                service files), including new dependencies, changes in startup scripts, etc ;
            - Containers are then restarted ;
            - Finally, Hawkbit server is notified with the result of the update (failure or success) : the details (exit code, name, etc) about 
                which app failed to start is given.

        :param dictionnary base: Dictionnary storing information about a Hawkbit update.
        """
        feedbackMsg = ''
        
        if self.action_id is not None:
            self.logger.info('Deployment is already in progress')
            return

        # retrieve action id and resource parameter from URL
        deployment = base['_links']['deploymentBase']['href']
        match = re.search(r'/deploymentBase/([^/?]+)', deployment)
        if not match:
            raise APIError("Invalid deploymentBase href: {}".format(deployment))
        action_id = match.group(1)
        resource = parse_qs(urlparse(deployment).query).get('c', [None])[0]
        # fetch deployment information
        deploy_info = await self.ddi.deploymentBase[action_id](resource)
        reboot_needed = False

        chunks_qty = len(deploy_info['deployment']['chunks'])

        if chunks_qty == 0:
            msg = 'Deployment without chunks found. Ignoring'
            status_execution = DeploymentStatusExecution.closed
            status_result = DeploymentStatusResult.failure
            await self.ddi.deploymentBase[action_id].feedback(
                status_execution, status_result, [msg])
            raise APIError(msg)
        else:
            msg = "FullMetalUpdate:Proceeding"
            percentage = {"cnt": 0, "of": chunks_qty}
            status_execution = DeploymentStatusExecution.proceeding
            status_result = DeploymentStatusResult.none
            await self.ddi.deploymentBase[action_id].feedback(
                status_execution, status_result, [msg],
                percentage=percentage)

        self.action_id = action_id

        # all DSs
        distribution_sets_raw = [
            item for item in await self._get_distribution_sets()
            if (isinstance(item.get('version'), str)
                and re.fullmatch(
                    r'[0-9]+\.[0-9]+(?:\.[0-9]+)?', item['version']))
        ]

        distribution_set_id = None
        distribution_set = None
        update_type = ''

        try:
            distribution_set_id = await self.management.get_distribution_set_id(
                self.ddi.controller_id,
                action_id)
        except Exception as e:
            self.logger.warning(
                "Failed to read Distribution Set metadata info via Management API; "
                "falling back to chunk metadata: {}".format(e))

        if distribution_set_id is not None:
            distribution_set = next((item for item in distribution_sets_raw if item.get('id') == distribution_set_id), None)

        if distribution_set is not None:
            update_type = distribution_set.get('type', '')
            partition_to_check = {
                'app': updater_paths.PATH_APPS,
                'os': updater_paths.PATH_OS,
            }.get(update_type)

            # filter out distribution_sets_raw based on update type (os, app)
            distribution_sets_raw = [item for item in distribution_sets_raw if item.get('type') == update_type]
            # sort them ascending by version
            distribution_sets_raw.sort(
                key=lambda item: (
                        tuple(int(part) for part in item['version'].split('.'))
                        + (0,) * (2 - item['version'].count('.'))))
        # hydrate distribution sets with their metadata
        distribution_sets = (
            await self._add_metadata_to_distribution_sets(distribution_sets_raw)
            if distribution_sets_raw is not None
            else []
        )

        for item in distribution_sets:
            metadata = item.get('metadata', {})
            if 'info' in metadata:
                parsed_info = self._parse_update_info(metadata['info'])
                item['distribution_set_info'] = parsed_info

        installed_update_versions = self._get_installed_update_versions()
        current_ds_version = {
            'os': installed_update_versions.get('os_version'),
            'app': installed_update_versions.get('apps_version'),
        }.get(update_type)

        target_ds_version = (
            distribution_set.get('version')
            if distribution_set is not None
            else None
        )

        distributions_to_apply = []
        if (isinstance(current_ds_version, str)
                and re.fullmatch(
                    r'[0-9]+\.[0-9]+(?:\.[0-9]+)?', current_ds_version)
                and isinstance(target_ds_version, str)
                and re.fullmatch(
                    r'[0-9]+\.[0-9]+(?:\.[0-9]+)?', target_ds_version)):
            current_version = tuple(
                int(part) for part in current_ds_version.split('.'))
            current_version += (0,) * (3 - len(current_version))
            target_version = tuple(
                int(part) for part in target_ds_version.split('.'))
            target_version += (0,) * (3 - len(target_version))

            distributions_to_apply = [
                item for item in distribution_sets
                if (current_version
                    < (tuple(
                        int(part) for part in item['version'].split('.'))
                       + (0,) * (2 - item['version'].count('.')))
                    <= target_version)
            ]

        update_total_size_mb = 0
        for item in distributions_to_apply:
            total_size_mb = item.get(
                'distribution_set_info', {}).get('totalSizeMB', 0)
            try:
                total_size_mb = float(total_size_mb)
            except (TypeError, ValueError):
                self.logger.error(
                    "Invalid totalSizeMB metadata value: {}".format(
                        total_size_mb))
                total_size_mb = 0

            update_total_size_mb += total_size_mb

        if update_total_size_mb and partition_to_check is not None:
            free_space_mb = self._check_partition_space_mb(partition_to_check)
            if free_space_mb < update_total_size_mb * (100 + UPDATE_GAP_PERCENT) / 100:
                msg = "Not enough free space on app partition {}: {:.2f} MB available, {:.2f} MB required".format(
                    partition_to_check,
                    free_space_mb,
                    update_total_size_mb
                )
                self.logger.error(msg)
                await self.ddi.deploymentBase[self.action_id].feedback(
                    DeploymentStatusExecution.closed,
                    DeploymentStatusResult.failure,
                    [msg])
                self.action_id = None
                return

        seq = ('name', 'version', 'rev', 'part', 'autostart', 'autoremove', 'status_execution', 'status_update', 'status_result', 'notify', 'timeout')
        updates = []

        # Update process
        for chunk in deploy_info['deployment']['chunks']:
            update = dict.fromkeys(seq)
            # parse the metadata included in the update
            for meta in chunk['metadata']:
                if meta['key'] == 'rev':
                    update['rev'] = meta['value']
                if meta['key'] == 'autostart':
                    update['autostart'] = int(meta['value'])
                if meta['key'] == 'autoremove':
                    update['autoremove'] = int(meta['value'])
                if meta['key'] == 'notify':
                    update['notify'] = int(meta['value'])
                if meta['key'] == 'timeout':
                    update['timeout'] = int(meta['value'])
            update['name'] = chunk['name']
            update['version'] = chunk['version']
            update['part'] = chunk['part']

            self.logger.info("Updating chunk part: {}".format(update['part']))

            if update['part'] == 'os':

                # checking if we just rebooted and we need to send the feedback in which
                # case we don't need to pull the update image again
                [feedback, reboot_data] = self.feedback_for_os_deployment(update['rev'])
                if feedback:
                    await self.ddi.deploymentBase[reboot_data['action_id']].feedback(
                        DeploymentStatusExecution(reboot_data['status_execution']),
                        DeploymentStatusResult(reboot_data['status_result']),
                        [reboot_data['msg']])
                    self.action_id = None
                    return

                self.logger.info("OS {} v.{} - updating...".format(update['name'], update['version']))
                update['status_update'] = self.update_system(update['rev'])
                update['status_execution'] = DeploymentStatusExecution.closed
                if not update['status_update']:
                    msg = "OS {} v.{} Deployment failed".format(update['name'], update['version'])
                    self.logger.error(msg)
                    update['status_result'] = DeploymentStatusResult.failure
                    await self.ddi.deploymentBase[self.action_id].feedback(
                        update['status_execution'], update['status_result'], [msg])
                    return
                else:
                    msg = "OS {} v.{} Deployment succeed".format(update['name'], update['version'])
                    self.logger.info(msg)
                    update['status_result'] = DeploymentStatusResult.success
                    reboot_needed = True
                    self.write_reboot_data(self.action_id,
                                           update['status_execution'],
                                           update['status_result'],
                                           msg)

            elif update['part'] == 'bApp':
                self.logger.info("App {} v.{} - updating...".format(update['name'], update['version']))
                update['status_update'] = self.update_container(update['name'], update['rev'], update['autostart'], update['autoremove'], update['notify'], update['timeout'])
                update['status_execution'] = DeploymentStatusExecution.closed
                updates.append(update)

        self.systemd.Reload()

        seq = [update['name'] for update in updates]
        self.mutexResults.acquire()
        self.feedbackResults = dict.fromkeys(seq)
        self.mutexResults.release()

        # Container restart process
        for update in updates:
            update['status_update'] &= self.handle_container(update['name'], update['autostart'], update['autoremove'])

        final_result = True
        fails = ""
        feedbackThreadIt = iter(self.feedbackThreads)

        # Hawkbit server feedback process
        for update in updates:
            if update['notify'] == 1:
                next(feedbackThreadIt).join()
                self.mutexResults.acquire()
                update['status_update'] &= self.feedbackResults[update['name']]['status_update']
                feedbackMsg = self.feedbackResults[update['name']]['msg']
                self.mutexResults.release()

            if not update['status_update']:
               msg = "App {} v.{} Deployment failed\n {}".format(update['name'], update['version'], feedbackMsg)
               self.logger.error(msg)
               update['status_result'] = DeploymentStatusResult.failure
               fails += update['name'] + " "
            else:
               msg = "App {} v.{} Deployment succeed".format(update['name'], update['version'])
               self.logger.info(msg)
               update['status_result'] = DeploymentStatusResult.success

            final_result &= (update['status_result'] == DeploymentStatusResult.success)
        
        if(final_result):
            msg = "Hawkbit Update Success : All applications have been updated and correctly restarted."
            self.logger.info(msg)
            status_result = DeploymentStatusResult.success
        else:
            msg = "Hawkbit Update Failure : " + fails + "failed to update and / or to restart."
            self.logger.error(msg)
            status_result = DeploymentStatusResult.failure
        await self.ddi.deploymentBase[self.action_id].feedback(DeploymentStatusExecution.closed, status_result, [msg])

        self.action_id = None
        if reboot_needed:
            try:
                subprocess.run("reboot")
            except subprocess.CalledProcessError as e:
                self.logger.error("Reboot failed: {}".format(e))

    async def sleep(self, base):
        """ 
        Timeout between two Hawkbit server polling tryouts. This sleep time is suggested by HawkBit. 

        :param dictionnary base: Dictionnary storing information about a Hawkbit update.
        """
        sleep_str = base['config']['polling']['sleep']
        self.logger.info('Will sleep for {}'.format(sleep_str))
        t = datetime.strptime(sleep_str, '%H:%M:%S')
        delta = timedelta(hours=t.hour, minutes=t.minute, seconds=t.second)
        await asyncio.sleep(delta.total_seconds())

    async def poll_base_resource(self):
        """
        This method polls the server for new updates and takes action depending on polling results.

        Wrapped in start_polling() to ease exceptions handling, this method continuously runs polling the server.
        """

        while True:
            base = await self.ddi()

            if '_links' in base:
                if 'configData' in base['_links']:
                    await self.identify()
                if 'deploymentBase' in base['_links']:
                    await self.process_deployment(base)
                if 'cancelAction' in base['_links']:
                    await self.cancel(base)

            await self.sleep(base)

    def update_container(self, container_name, rev_number, autostart, autoremove, notify=None, timeout=None):
        """
        Wrapper method to execute the different steps of a container update.

        :param string container_name: Name of the container.
        :param string rev_number: Commit revision.
        :param int autostart: set to 1 if the container should be automatically started, 0 otherwise
        :param int autoremove: if set to 1, the container's directory will be deleted
        :param int action_id: Unique identifier of an Hawkbit update.
        :param int notify: Set to 1 if the container is a notify container.
        :param int timeout: Timeout value of the communication socket.
        """
        try:
            self.init_container_remote(container_name)
            self.pull_ostree_ref(True, rev_number, container_name)
            self.checkout_container(container_name, rev_number)
            self.update_container_ids(container_name)
            if (autostart == 1) and (notify == 1) and (autoremove != 1):
                feedback_thread = self.create_and_start_feedback_thread(container_name, rev_number, autostart, autoremove, timeout)
                self.feedbackThreads.append(feedback_thread)
            self.create_unit(container_name)
        except Exception as e:
            self.logger.error("Updating {} failed ({})".format(container_name, e))
            return False
        return True

    def update_system(self, rev_number):
        """
        Wrapper method to execute the different steps of a OS update.

        :param string rev_number: Commit revision.
        """
        try:
            self.pull_ostree_ref(False, rev_number)
            self.ostree_stage_tree(rev_number)
            self.delete_init_var()
        except Exception as e:
            self.logger.error("Updating the OS failed ({})".format(e))
            return False
        return True

    def write_reboot_data(self, action_id, status_execution, status_result, msg):
        """
        Write information about the current update in a json file.

        :param int action_id: Unique identifier of an Hawkbit update.
        :param DeploymentStatusExecution status_execution: Execution status of the current Hawkbit update.
        :param DeploymentStatusResult status_result: Result status of the current Hawkbit update.
        :param string msg: Message to be sent to the Hawkbit server.
        :raises IOError: Exception raised if writting reboot data into a json file failed.
        """
        # the enums are not serializable thus we store their value
        reboot_data = {
            "action_id": action_id,
            "status_execution": status_execution.value,
            "status_result": status_result.value,
            "msg": msg
        }

        try:
            with open(PATH_REBOOT_DATA, "w") as f:
                json.dump(reboot_data, f)
        except IOError as e:
            self.logger.error("Writing reboot data failed ({})".format(e))

    def feedback_for_os_deployment(self, revision):
        """
        This method will generate a feedback message for the Hawkbit server and
        return the reboot data which will be used by the the DDI client to return
        the appropriate feedback message.

        :param checksum revision: Checksum of revision stored on OSTree remote repository.
        :returns: (True, reboot_data) if the reboot data json file has been correctly found and updated.
        :raises FileNotFoundError: Exception raised if the reboot data json file has not been found.
        """

        reboot_data = None
        try:
            with open(PATH_REBOOT_DATA, "r") as f:
                reboot_data = json.load(f) # json.load() return a JSON object (similar to a dictionnary whose keys are strings and whose values are JSON types)
            if reboot_data is None:
                self.logger.error("Rebooting data loading failed")
        except FileNotFoundError:
            return (False, None)

        if self.check_for_rollback(revision):
            reboot_data.update({"status_result": DeploymentStatusResult.failure.value})
            reboot_data.update({"msg": "Deployment has failed and system has rollbacked"})

        os.remove(PATH_REBOOT_DATA)

        return (True, reboot_data)

    def create_and_start_feedback_thread(self, container_name, rev, autostart, autoremove, timeout):
        """
        This method is called to initialize and start the feedback thread used to
        feedback the server the status of a container whose notify variable is set. See the
        container_feedbacker thread method.

        :param string container_name: Name of the container.
        :param string rev: Commit revision, used for rollbacking.
        :param int autostart: Autostart variable of the container, used for rollbacking.
        :param int autoremove: Autoremove of the container, used for rollbacking.
        :param int timeout: Timeout value of the communication socket.
        """
        sock_name = "fullmetalupdate_notify_" + container_name + ".sock"
        self.logger.info("Creating socket {}".format(sock_name))
        sock = s.socket(s.AF_UNIX, s.SOCK_STREAM)
        sock.settimeout(timeout)
        if os.path.exists(DIR_NOTIFY_SOCKET + sock_name):
            os.remove(DIR_NOTIFY_SOCKET + sock_name)
        sock.bind(DIR_NOTIFY_SOCKET + sock_name)

        container_feedbackd = Thread(
            target=self.container_feedbacker,
            args=(sock,
                  container_name,
                  rev,
                  autostart,
                  autoremove),
            name= "container-feedback-" + container_name)
        container_feedbackd.start()
        return container_feedbackd

    def container_feedbacker(self,
                             socket,
                             container_name,
                             rev_number,
                             autostart,
                             autoremove):
        """
        This thread method is used to feedback the server for containers which provide
        the notify feature of systemd. It will trigger a rollback on the container in case
        of failure (if possible).

        This method will wait on an Unix socket for information about the notify result,
        and proceed in consequence.

        :param socket socket: Socket used for communication between the container service and this thread.
        :param string container_name: Name of the container.
        :param string rev: Commit revision, used for rollbacking.
        :param int autostart: Autostart variable of the container, used for rollbacking.
        :param int autoremove: Autoremove of the container, used for rollbacking.
        """

        try:
            sock_name = "fullmetalupdate_notify_" + container_name + ".sock"
            socket.listen(1)
            [conn, _] = socket.accept()
            datagram = conn.recv(1024)

            if datagram:
                systemd_info = datagram.strip().decode("utf-8").split()
                self.logger.debug("Datagram received : {}".format(systemd_info))

                if systemd_info[0] == 'success':
                    # feedback the server positively
                    msg = "Container " + container_name + " started successfully"
                    status_update = True
                    self.logger.info(msg)
                    # Write this new revision for future updates
                    self.set_current_revision(container_name, rev_number)
                else:
                    # rollback + feedback the server negatively
                    status_update = False
                    end_msg = self.rollback_container(container_name, autostart, autoremove)
                    msg = "Container " + container_name + " failed to start with result :" \
                        + "\n\tSERVICE_RESULT=" + systemd_info[0] \
                        + "\n\tEXIT_CODE=" + systemd_info[1] \
                        + "\n\tEXIT_STATUS=" + systemd_info[2] \
                        + end_msg
                    self.logger.info(msg)
        except s.timeout:
            # socket timeout, try to rollback if possible
            status_update = False
            msg = "Container " + container_name + " failed to start : the socket timed out."
            self.logger.error(msg)
            end_msg = self.rollback_container(container_name,
                                              autostart,
                                              autoremove)
            msg += end_msg

        socket.close()
        self.logger.info("Removing socket {}".format(sock_name))
        try:
            os.remove(DIR_NOTIFY_SOCKET + sock_name)
        except FileNotFoundError as e:
            self.logger.error("Error while removing socket ({})".format(e))
        
        self.mutexResults.acquire()
        self.feedbackResults[container_name] = dict.fromkeys(("status_update", "msg"))
        self.feedbackResults[container_name]["status_update"] = status_update
        self.feedbackResults[container_name]["msg"] = msg
        self.mutexResults.release()

    def rollback_container(self, container_name, autostart, autoremove):
        """
        This method Rollbacks the container, if possible, and returns a message that will
        be sent to the server.

        :param string container_name: Name of the container.
        :param int autostart: Autostart variable of the container, used for rollbacking.
        :param int autoremove: Autoremove of the container, used for rollbacking.

        :returns: End of the message that will be sent, which depends on the status of the rollback (performed or not)
        :rtype: string
        """

        end_msg = ""
        previous_rev = self.get_previous_rev(container_name)

        if previous_rev is None:
            end_msg = "\nFirst installation of the container, cannot rollback."
        else:
            res = self.update_container(container_name, previous_rev, autostart, autoremove)
            self.systemd.Reload()
            res &= self.handle_container(container_name, autostart, autoremove)
            if res:
                end_msg = "\nContainer has rollbacked."
            else:
                end_msg = "\nContainer has failed to rollback."

        return end_msg
