# -*- coding: utf-8 -*-

import logging
import os
import shutil
import subprocess
import json
import gi
import stat
from pathlib import Path
import errno
import subprocess
import time

gi.require_version("OSTree", "1.0")
from gi.repository import OSTree, GLib, Gio
from pydbus import SystemBus

PATH_APPS = '/apps'
PATH_REPO_OS = '/ostree/repo/'
PATH_REPO_APPS = PATH_APPS + '/ostree_repo'
PATH_SYSTEMD_UNITS = '/etc/systemd/system/'
PATH_CURRENT_REVISIONS = '/var/local/fullmetalupdate/current_revs.json'
VALIDATE_CHECKOUT = 'CheckoutDone'
FILE_AUTOSTART = 'auto.start'
CONTAINER_UID = 1000
CONTAINER_GID = 1000
OSTREE_DEPTH = 1

class DBUSException(Exception):
    pass


class AsyncUpdater(object):
    """ FullMetalUpdate client updater library.

        Provides methods to perform all different step of a FMU update.

        :param logging logger: Logger used to print information regarding the update proceedings or to report errors.
        :param pydbus.SystemBus systemd: Allow to use systemd services exposed over D-Bus.
        :param OSTree.Sysroot sysroot: Python instance of rootfs (root file system) of the system.
        :param OSTree.Repo repo_containers: Python instance of the OSTree remote repository for containers.
        :param OSTree.Repo repo_os: Python instance of the OSTree remote repository for the OS.
    """

    def __init__(self):
        """ Constructor of AsyncUpdater Class.
        """

        self.ostree_remote_attributes = None

        self.logger = logging.getLogger('fullmetalupdate_container_updater')

        self.mark_os_successful()

        bus = SystemBus()
        self.systemd = bus.get('.systemd1')

        self.sysroot = OSTree.Sysroot.new_default()
        self.sysroot.load(None)
        self.logger.info("Cleaning the sysroot")
        self.sysroot.cleanup(None)

        [_, repo] = self.sysroot.get_repo()
        self.repo_os = repo

        self.remote_name_os = None
        self.repo_containers = OSTree.Repo.new(Gio.File.new_for_path(PATH_REPO_APPS))
        if os.path.exists(PATH_REPO_APPS):
            self.logger.info("Preinstalled OSTree for containers, we use it")
            self.repo_containers.open(None)
        else:
            self.logger.info("No preinstalled OSTree for containers, we create one")
            self.repo_containers.create(OSTree.RepoMode.ARCHIVE_Z2, None)

    def mark_os_successful(self):
        """ This method marks the currently running OS as successful by setting the init_var u-boot environment variable to 1.

        :returns: - True if the variable was successfully set
                  - False otherwise
        :raises subprocess.CalledProcessError: Exception raised if u-boot environment variable failed to be set up to 1.
        """
        try:
            if subprocess.call(["fw_setenv", "success", "1"]) == 0:
                self.logger.info("Setting success u-boot environment variable to 1 succeeded")
            else:
                self.logger.error("Setting success u-boot environment variable to 1 failed")

        except subprocess.CalledProcessError as e:
            self.logger.error("Ostree rollback post-process commands failed ({})".format(str(e)))

    def check_for_rollback(self, revision):
        """
        Function used to execute the different commands needed for the rollback to be effective.

        We check :
         - if the booted deployment's revision matches the server's revision
         - if so, check if there is a pending deployment (meaning we've rollbacked) and
           undeploy it.

        :param checksum revision: Checksum of revision stored on OSTree remote repository.
        :returns: - True when the system has rollbacked
                  - False otherwise
        :raises subprocess.CalledProcessError: Exception raised if rollback post-processing fails.
        """
        try:
            has_rollbacked = False

            # returns [pendings deployments, rollback deployments]
            deployments = self.sysroot.query_deployments_for(None)

            # the deployment we are booted on
            booted_deployment_rev = self.sysroot.get_booted_deployment().get_csum()

            if booted_deployment_rev != revision:
                has_rollbacked = True
                self.logger.warning("The system rollbacked. Checking if we needed to undeploy")
                if deployments[0] is not None:
                    self.logger.info("There is a pending deployment. Undeploying...")
                    # 0 is the index of the pending deployment (if there is one)
                    if subprocess.call(["ostree", "admin", "undeploy", "0"]) != 0:
                        self.logger.error("Undeployment failed")
                    else:
                        self.logger.info("Undeployment successful")
            else:
                self.logger.info("No undeployment needed")

            return has_rollbacked

        except subprocess.CalledProcessError as e:
            self.logger.error("Ostree rollback post-process commands failed ({})".format(str(e)))
            return False
    def init_ostree_remotes(self, ostree_remote_attributes):
        """
        Initialize OSTree remotes (OS + containers) with optional TLS options.

        :param dict ostree_remote_attributes:
            {
              'name': 'fullmetalupdate',
              'url': 'https://ostreem1.mosaicone.cloud',
              'gpg-verify': False,
              'tls-ca-path': '/etc/.../ostree.crt',
              'tls-client-cert-path': '/etc/.../device.crt',
              'tls-client-key-path': '/etc/.../device.key',
            }
        """
        res = True
        self.ostree_remote_attributes = ostree_remote_attributes

        # Base options
        opts_dict = {
            'gpg-verify': GLib.Variant('b', ostree_remote_attributes['gpg-verify']),
        }

        # Inject TLS settings if provided
        ca  = ostree_remote_attributes.get('tls-ca-path')
        cli = ostree_remote_attributes.get('tls-client-cert-path')
        key = ostree_remote_attributes.get('tls-client-key-path')

        if ca:
            opts_dict['tls-ca-path'] = GLib.Variant('s', ca)
        if cli:
            opts_dict['tls-client-cert-path'] = GLib.Variant('s', cli)
        if key:
            opts_dict['tls-client-key-path'] = GLib.Variant('s', key)

        opts = GLib.Variant('a{sv}', opts_dict)

        try:
            # ---------------- OS remote ----------------
            os_remote = ostree_remote_attributes['name']
            os_url    = ostree_remote_attributes['url']

            self.logger.info("Initalize remotes for the OS ostree: %s", os_remote)

            if os_remote in self.repo_os.remote_list():
                self.logger.info(
                    "OS remote %s already exists, deleting and re-adding with TLS options",
                    os_remote
                )
                self.repo_os.remote_delete(os_remote, None)

            self.repo_os.remote_add(os_remote, os_url, opts, None)
            self.remote_name_os = os_remote

            # ---------------- containers remote ----------------
            remote_name    = "mad-matisse-gen3-containers"
            containers_url = ostree_remote_attributes['url']

            self.logger.info(
                "Initalize remotes for the containers ostree: %s", remote_name
            )

            if remote_name in self.repo_containers.remote_list():
                self.logger.info(
                    "Containers remote %s already exists, deleting and re-adding",
                    remote_name
                )
                self.repo_containers.remote_delete(remote_name, None)

            self.repo_containers.remote_add(remote_name, containers_url, opts, None)

        except GLib.Error as e:
            self.logger.error("OSTRee remote initialization failed (%s)", str(e))
            res = False

        return res

    def set_current_revision(self, container_name, rev):
        """
        This method writes rev into a json file containing the current working rev for the containers.

        :param string container_name: Name of the container.
        :param string rev: Revision to write in json file.
        :raises FileNotFoundError: Exception raised if json file needs to be created.
        """
        try:
            with open(PATH_CURRENT_REVISIONS, "r") as f:
                current_revs = json.load(f)
            current_revs.update({container_name: rev})
            with open(PATH_CURRENT_REVISIONS, "w") as f:
                json.dump(current_revs, f, indent=4)
        except FileNotFoundError:
            with open(PATH_CURRENT_REVISIONS, "w") as f:
                current_revs = {container_name: rev}
                json.dump(current_revs, f, indent=4)

    def get_previous_rev(self, container_name):
        """
        This method returns the previous working revision of a notify container.

        :param string container_name: Name of the container.
        :returns: - The rev sha for container_name
                  - None if the container isn't found
        :raises KeyError: Execution raised if container associated with container_name doesn't exist.
        :raises FileNotFoundError: Execution raised if no file with previous rev exists.
        """
        try:
            with open(PATH_CURRENT_REVISIONS, "r") as f:
                current_revs = json.load(f)
            return current_revs[container_name]
        except (FileNotFoundError, KeyError):
            return None

    def create_whiteouts(self, store_dir: Path, whiteout_dir):
        store_dir = Path(store_dir)
        whiteout_dir = Path(whiteout_dir)
        whiteout_dir.mkdir(parents=True, exist_ok=True)
        self.logger.info(f"Creating whiteout markers for special files in {store_dir}...")

        for file in store_dir.rglob("*"):
            if not file.exists():
                continue

            file_stat = os.lstat(file)

            if file.is_symlink():
                continue

            if stat.S_ISFIFO(file_stat.st_mode) or stat.S_ISBLK(file_stat.st_mode) or \
               stat.S_ISCHR(file_stat.st_mode) or stat.S_ISSOCK(file_stat.st_mode):
                relative_path = file.relative_to(store_dir)
                whiteout_file = store_dir / f".wh.{file.name}"
                metadata_file = whiteout_dir / f"{relative_path}.meta"

                self.logger.info(f"Whiteout: {file} -> {whiteout_file}")
                whiteout_file.touch()
                metadata_file.parent.mkdir(parents=True, exist_ok=True)

                metadata = [
                    f"{file_stat.st_mode:o} {file_stat.st_uid} {file_stat.st_gid}"
                ]

                if stat.S_ISCHR(file_stat.st_mode) or stat.S_ISBLK(file_stat.st_mode):
                    metadata.append(f"{os.major(file_stat.st_rdev)} {os.minor(file_stat.st_rdev)}")

                metadata_file.write_text("\n".join(metadata))
                os.chmod(file, stat.S_IWUSR)
                file.unlink()

    def init_checkout_existing_containers(self):
        """
        This method manages:
            - it checks out the containers installed on the target ;
            - then it copies the service files from /apps partition to the right location ;
            - then it regenerates systemd dependancy tree ;
            - last but not least, it starts the containers.

        :returns: - True if the containers are successfully initialized
                  - False otherwise
        """
        res = True
        self.logger.info("Getting refs from repo:{}".format(PATH_REPO_APPS))

        try:
            [_, refs] = self.repo_containers.list_refs(None, None)
            self.logger.info("There are {} containers to be started.".format(len(refs)))

            self.disable_watcher()
            self.disable_podman()

            for ref in refs:
                container_name = ref.split(':')[1]
                if not os.path.isfile(PATH_APPS + '/' + container_name + '/' + VALIDATE_CHECKOUT):
                    self.checkout_container(container_name, None)
                    self.update_container_ids(container_name)
                    # Prashant to add the whiteout file creation step here
                    store_dir = os.path.join(PATH_APPS + '/' + container_name)
                    whiteout_dir = os.path.join(PATH_APPS, ".whiteout-metadata")
                    self.create_whiteouts(store_dir, whiteout_dir)
                if not res:
                    self.logger.error("Error when checking out container:{}".format(container_name))
                    break
                self.create_unit(container_name)


            self.systemd.Reload()

            self.enable_watcher()
            self.enable_podman()

            for ref in refs:
                container_name = ref.split(':')[1]
                if os.path.isfile(PATH_APPS + '/' + container_name + '/' + FILE_AUTOSTART):
                    self.start_unit(container_name)
        except (GLib.Error, Exception) as e:
            self.logger.error("Error checking out containers repo ({})".format(e))
            res = False
        finally:
            return res

    def enable_podman(self):
        service = self.systemd.ListUnitsByNames(['podman.socket'])
        if service[0][2] != 'not-found':
            self.logger.info("Start the service {}".format('podman.socket'))
            self.start_service('podman.socket')

        service = self.systemd.ListUnitsByNames(['podman.service'])
        if service[0][2] != 'not-found':
            self.logger.info("Start the service {}".format('podman.service'))
            self.start_service('podman.service')

    def disable_podman(self):
        service = self.systemd.ListUnitsByNames(['podman.socket'])
        if service[0][2] != 'not-found':
            self.logger.info("Stop the service {}".format('podman.socket'))
            self.stop_service('podman.socket')

        service = self.systemd.ListUnitsByNames(['podman.service'])
        if service[0][2] != 'not-found':
            self.logger.info("Stop the service {}".format('podman.service'))
            self.stop_service('podman.service')

    def enable_watcher(self):
        service = self.systemd.ListUnitsByNames(['containers-watcher.service'])
        if service[0][2] != 'not-found':
            self.logger.info("Start the containers-watcher.service")
            self.start_service('containers-watcher.service')

    def disable_watcher(self):
        service = self.systemd.ListUnitsByNames(['containers-watcher.service'])
        if service[0][2] != 'not-found':
            self.logger.info("Stop the containers-watcher.service")
            self.stop_service('containers-watcher.service')

    def create_unit(self, container_name):
        """
        This method copies the .service file from /apps partition to /etc/systemd/system/ in order to create the unit for the relevant container.

        :param string container_name: Name of the container.
        """
        self.logger.info("Copy the service file to /etc/systemd/system/{}.service".format(container_name))
        shutil.copy(PATH_APPS + '/' + container_name + '/systemd.service',
                    PATH_SYSTEMD_UNITS + container_name + '.service')

    def start_unit(self, container_name):
        """
        This method enables and then starts the systemd unit for the relevant container.

        :param string container_name: Name of the container.
        """
        self.logger.info("Enable the container {}".format(container_name))
        self.systemd.EnableUnitFiles([container_name + '.service'], False, False)
        self.logger.info("Since FILE_AUTOSTART is present, start the container using systemd")
        self.systemd.StartUnit(container_name + '.service', "replace")

    def start_service(self, service_name):
        """
        This method enables and then starts the systemd unit for the relevant service.

        :param string service_name: Name of the Service.
        """
        self.logger.info("Enable the service {}".format(service_name))
        self.systemd.EnableUnitFiles([service_name], False, False)
        self.systemd.StartUnit(service_name, "replace")

    def stop_unit(self, container_name):
        """
        This method stops the systemd unit for the relevant container.

        :param string container_name: Name of the container.
        """
        self.logger.info("Since FILE_AUTOSTART is not present, stop the container using systemd")
        self.systemd.StopUnit(container_name + '.service', "replace")
        self.logger.info("Disable the container {}".format(container_name))
        self.systemd.DisableUnitFiles([container_name + '.service'], False)

    def stop_service(self, service_name):
        """
        This method stops the systemd unit for the relevant service.

        :param string service_name: Name of the service.
        """
        self.logger.info("Disable the service {}".format(service_name))
        self.systemd.StopUnit(service_name, "replace")
        self.systemd.DisableUnitFiles([service_name], False)

    def pull_ostree_ref(self, is_container, ref_sha, ref_name=None):
        """
        Wrapper method to pull a ref from an OSTree remote repository.

        :param boolean is_container: - True to pull a container image
                                     - False to pull an OS image
        :param string ref_sha: SHA checksum of the ref commit to pull.
        :param string ref_name: Name of the ref commit to pull (can be the name of the container, if None, the OS name will be set).
        """
        res = True

        if is_container:
            repo = self.repo_containers
        else:
            repo = self.repo_os
            ref_name = self.remote_name_os

        try:
            progress = OSTree.AsyncProgress.new()
            progress.connect('changed', OSTree.Repo.pull_default_console_progress_changed, None)

            opts = GLib.Variant('a{sv}', {'flags': GLib.Variant('i', OSTree.RepoPullFlags.NONE),
                                          'refs': GLib.Variant('as', (ref_sha,)),
                                          'depth': GLib.Variant('i', OSTREE_DEPTH)})
            self.logger.info("Pulling remote {} from OSTree repo ({})".format(ref_name, ref_sha))
            res = repo.pull_with_options(ref_name, opts, progress, None)
            progress.finish()
            self.logger.info("Upgrader pulled {} from OSTree repo ({})".format(ref_name, ref_sha))
        except GLib.Error as e:
            self.logger.error("Pulling {} from OSTree repo failed ({})".format(ref_name, str(e)))
            raise
        if not res:
            raise Exception("Pulling {} failed (returned False)".format(ref_name))

    def init_container_remote(self, container_name):
        """
        If the container does not exist, initialize its remote.

        :param string container_name: Name of the container.
        """

        # returns [('container-hello-world.service', 'description', 'loaded', 'failed', 'failed', '', '/org/freedesktop/systemd1/unit/wtk_2dnodejs_2ddemo_2eservice', 0, '', '/')]
        service = self.systemd.ListUnitsByNames([container_name + '.service'])

        try:
            if (service[0][2] == 'not-found'):
                # New service added, we need to connect to its remote
                opts = GLib.Variant('a{sv}',
                                    {'gpg-verify': GLib.Variant('b', self.ostree_remote_attributes['gpg-verify'])})
                # Check if this container was not installed previously
                if container_name not in self.repo_containers.remote_list():
                    self.logger.info("New container added to the target, "
                                     "we install the remote: {}".format(container_name))
                    self.repo_containers.remote_add(container_name,
                                                    self.ostree_remote_attributes['url'],
                                                    opts, None)
                else:
                    self.logger.info("New container {} added to the target but the remote "
                                     "already exists, we do nothing".format(container_name))
        except GLib.Error as e:
            self.logger.error("Initializing {} remote failed ({})".format(container_name, str(e)))
            raise

    def update_container_ids(self, container_name):
        """
        By default, the container are checked out as root. This method sets the uid and
        gid of all the container related files to 1000 (UID) and 1000 (GID).

        :param string container_name: Name of the container.
        """
        self.logger.info("Update the UID and GID of the rootfs")
        os.chown(PATH_APPS + '/' + container_name, CONTAINER_UID, CONTAINER_GID)
        for dirpath, dirnames, filenames in os.walk(PATH_APPS + '/' + container_name):
            for dname in dirnames:
                os.lchown(os.path.join(dirpath, dname), CONTAINER_UID, CONTAINER_GID)
            for fname in filenames:
                os.lchown(os.path.join(dirpath, fname), CONTAINER_UID, CONTAINER_GID)

    def handle_container(self, container_name, autostart, autoremove):
        """
        This method will handle the container execution or deletion based on the autostart
        and autoremove arguments.

        :param string container_name: Name of the container.
        :param int autostart: set to 1 if the container should be automatically started, 0 otherwise
        :param int autoremove: if set to 1, the container's directory will be deleted
        :returns: - True if the relevant container started correctly
                  - False otherwise
        """
        try:
            if autoremove == 1:
                self.logger.info("Remove the directory: {}".format(PATH_APPS + '/' + container_name))
                shutil.rmtree(PATH_APPS + '/' + container_name)
            else:
                service = self.systemd.ListUnitsByNames([container_name + '.service'])
                if service[0][2] == 'not-found':
                    self.logger.info("First installation of the container {} on the "
                                    "system, we create and start the service".format(container_name))
                    if os.path.isfile(PATH_APPS + '/' + container_name + '/' + FILE_AUTOSTART):
                        self.start_unit(container_name)
                else:
                    if autostart == 1:
                        if not os.path.isfile(PATH_APPS + '/' + container_name + '/' + FILE_AUTOSTART):
                            open(PATH_APPS + '/' + container_name + '/' + FILE_AUTOSTART, 'a').close()
                        self.start_unit(container_name)
                    else:
                        if os.path.isfile(PATH_APPS + '/' + container_name + '/' + FILE_AUTOSTART):
                            os.remove(PATH_APPS + '/' + container_name + '/' + FILE_AUTOSTART)
        except Exception as e:
            self.logger.error("UpdateTest :: Handling {} failed ({})".format(container_name, e))
            return False
        return True

    @staticmethod
    def _force_rmtree(path, logger, max_attempts=5):
        """
        Aggressively remove a directory tree, handling races with podman/mounts.

        - Retries on EBUSY and ENOTEMPTY with extra cleanup.
        - Final fallback: `rm -rf` via subprocess.
        """
        path = os.path.abspath(path)
        logger.info("force_rmtree: cleaning %s", path)

        for attempt in range(1, max_attempts + 1):
            if not os.path.exists(path):
                logger.info("force_rmtree: %s already gone (attempt %d)", path, attempt)
                return True

            try:
                shutil.rmtree(path)
                logger.info("force_rmtree: rmtree succeeded on attempt %d for %s",
                            attempt, path)
                return True
            except OSError as e:
                err = getattr(e, "errno", None)
                logger.warning(
                    "force_rmtree: attempt %d failed on %s: %s (errno=%s)",
                    attempt, path, e, err
                )

                # Typical races: something still mounted or recreated in the dir
                if err in (errno.EBUSY, errno.ENOTEMPTY):
                    try:
                        AsyncUpdater._umount_all_under(path, logger)
                        AsyncUpdater._lazy_unmount_container_shm(logger)
                    except Exception as ce:
                        logger.warning("force_rmtree: cleanup helpers failed: %s", ce)

                    # Backoff a bit before retrying
                    time.sleep(1.0 * attempt)
                    continue

                # Any other errno: break out and go to rm -rf fallback
                break

        # Last resort: do what you did manually: rm -rf
        try:
            logger.warning("force_rmtree: using 'rm -rf %s' as last resort", path)
            subprocess.run(["rm", "-rf", path], check=False)
            if not os.path.exists(path):
                logger.info("force_rmtree: rm -rf succeeded for %s", path)
                return True
            else:
                logger.error("force_rmtree: rm -rf did not remove %s", path)
        except Exception as e:
            logger.error("force_rmtree: rm -rf failed for %s: %s", path, e)

        return False

    @staticmethod
    def _umount_all_under(root_path, logger):
        """
        Lazy-unmount any mountpoints that live at or beneath root_path.
        Handles overlay/merged, tmpfs, bind mounts, etc.
        """
        try:
            cp = subprocess.run(["mount"], capture_output=True, text=True, check=False)
            mps = []
            root = os.path.abspath(root_path).rstrip("/")
            for line in cp.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 3:
                    mnt = parts[2]
                    if mnt == root or mnt.startswith(root + "/"):
                        mps.append(mnt)
            # Unmount deepest-first
            for mnt in sorted(set(mps), key=len, reverse=True):
                subprocess.run(["umount", "-l", mnt], check=False)
                logger.info("Unmounted mount under app dir: %s", mnt)
        except Exception as e:
            logger.warning("umount-under failed for %s: %s", root_path, e)

    @staticmethod
    def _stop_stack_gracefully(container_name, logger, apps_root):
        load_path = os.path.join(apps_root, container_name, 'load')
        if os.path.exists(load_path) and os.access(load_path, os.X_OK):
            try:
                subprocess.run([load_path, 'stop'], check=False, timeout=120)
                logger.info("Called load stop for %s", container_name)
            except Exception as e:
                logger.warning("load stop failed for %s: %s", container_name, e)
        # wait up to ~30s for common podman shm mounts to disappear
        for _ in range(30):
            cp = subprocess.run(
                "mount | grep -E '/containers/.*/userdata/shm'",
                shell=True, check=False
            )
            if cp.returncode != 0:
                break
            time.sleep(1)

    @staticmethod
    def _lazy_unmount_container_shm(logger):
        try:
            cp = subprocess.run(
                "mount | awk '/containers\\/.*\\/userdata\\/shm/ {print $3}'",
                shell=True, check=False, capture_output=True, text=True
            )
            for mnt in [m for m in cp.stdout.splitlines() if m]:
                subprocess.run(['umount', '-l', mnt], check=False)
                logger.info("Lazy-unmounted lingering shm: %s", mnt)
        except Exception as e:
            logger.warning("Unable to lazy-unmount shm: %s", e)

    def checkout_container(self, container_name, rev_number):
        """
        This method checks out a container into its corresponding folder, to a given commit revision.
        Before that, it stops the container using systemd, if found.

        :param string container_name: Name of the container.
        :param string rev_number: Commit revision.
        """
        service = self.systemd.ListUnitsByNames([container_name + '.service'])
        if service[0][2] != 'not-found':
            self.logger.info("Stop the container {}".format(container_name))
            self.stop_unit(container_name)
        self._stop_stack_gracefully(container_name, self.logger, PATH_APPS)
        res = True
        rootfs_fd = None
        try:
            options = OSTree.RepoCheckoutAtOptions()
            options.overwrite_mode = OSTree.RepoCheckoutOverwriteMode.UNION_IDENTICAL
            options.process_whiteouts = True
            options.bareuseronly_dirs = True
            options.no_copy_fallback = True
            options.mode = OSTree.RepoCheckoutMode.USER

            self.logger.info("Getting rev from repo:{}".format(container_name + ':' + container_name))

            if rev_number is None:
                rev = self.repo_containers.resolve_rev(container_name + ':' + container_name, False)[1]
            else:
                rev = rev_number
            self.logger.info("Rev value:{}".format(rev))
            full_path = os.path.join(PATH_APPS, container_name)
            self._umount_all_under(full_path, self.logger)
            if os.path.isdir(full_path):
                if not self._force_rmtree(full_path, self.logger):
                    raise Exception(f"Failed to cleanup {full_path} before checkout")

            os.mkdir(PATH_APPS + '/' + container_name)
            self.logger.info("Create directory {}/{}".format(PATH_APPS, container_name))
            rootfs_fd = os.open(PATH_APPS + '/' + container_name, os.O_DIRECTORY)
            res = self.repo_containers.checkout_at(options, rootfs_fd, PATH_APPS + '/' + container_name, rev)
            open(PATH_APPS + '/' + container_name + '/' + VALIDATE_CHECKOUT, 'a').close()

        except GLib.Error as e:
            self.logger.error("Checking out {} failed ({})".format(container_name, str(e)))
            raise
        if rootfs_fd is not None:
            os.close(rootfs_fd)
        if not res:
            raise Exception("Checking out {} failed (returned False)")

    def ostree_stage_tree(self, rev_number):
        """
        Wrapper around sysroot.stage_tree()

        Deploy new revision, however finalization only occurs at shutdown time.

        :param string rev_number: Commit revision.
        """
        try:
            booted_dep = self.sysroot.get_booted_deployment()
            if booted_dep is None:
                raise Exception("Not booted in an OSTree system")
            [_, checksum] = self.repo_os.resolve_rev(rev_number, False)
            origin = booted_dep.get_origin()
            osname = booted_dep.get_osname()

            [res, _] = self.sysroot.stage_tree(osname, checksum, origin, booted_dep, None, None)

            self.logger.info("Staged the new OS tree. The new deployment will be ready after a reboot")

        except GLib.Error as e:
            self.logger.error("Failed while staging new OS tree ({})".format(e))
            raise
        if not res:
            raise Exception("Failed while staging new OS tree (returned False)")

    def delete_init_var(self):
        """
        This method delete u-boot's environment variable init_var, to restart the rollback procedure.
        """
        try:
            self.logger.info("Deleting init_var u-boot environment variable")
            if subprocess.call(["fw_setenv", "init_var"]) != 0:
                self.logger.error("Deleting init_var variable from u-boot environment failed")
            else:
                self.logger.info("Deleting init_var variable from u-boot environment succeeded")
        except subprocess.CalledProcessError as e:
            self.logger.error("Deleting init_var variable from u-boot environment failed ({})".format(e))
            raise
