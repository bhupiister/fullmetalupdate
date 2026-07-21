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
import time

gi.require_version("OSTree", "1.0")
from gi.repository import OSTree, GLib, Gio
from pydbus import SystemBus

PATH_APPS = '/apps'
PATH_OS = '/sysroot'
PATH_REPO_OS = '/ostree/repo/'
PATH_REPO_APPS = PATH_APPS + '/ostree_repo'
PATH_SYSTEMD_UNITS = '/etc/systemd/system/'
PATH_CURRENT_REVISIONS = '/var/local/fullmetalupdate/current_revs.json'
VALIDATE_CHECKOUT = 'CheckoutDone'
FILE_AUTOSTART = 'auto.start'
CONTAINER_UID = 1000
CONTAINER_GID = 1000
OSTREE_DEPTH = 1


class _DevSystemd:
    def ListUnitsByNames(self, names):
        return [(name, "", "not-found", "", "", "", "", "", "", "") for name in names]

    def Reload(self):
        return None

    def EnableUnitFiles(self, *args, **kwargs):
        return None

    def DisableUnitFiles(self, *args, **kwargs):
        return None

    def StartUnit(self, *args, **kwargs):
        return None

    def StopUnit(self, *args, **kwargs):
        return None


class DBUSException(Exception):
    pass


class AsyncUpdater(object):
    def __init__(self, dev_mode=False, dev_state_dir=None):
        self.ostree_remote_attributes = None
        self.logger = logging.getLogger('fullmetalupdate_container_updater')
        self.dev_mode = dev_mode

        if self.dev_mode:
            self._configure_dev_paths(dev_state_dir)
            self.logger.warning("Running in dev mode; hardware/systemd/OSTree sysroot actions are mocked")
        else:
            self.mark_os_successful()

        if self.dev_mode:
            self.systemd = _DevSystemd()
        else:
            bus = SystemBus()
            self.systemd = bus.get('.systemd1')

        if self.dev_mode:
            self.sysroot = None
            self.repo_os = self._open_or_create_repo(PATH_REPO_OS)
        else:
            self.sysroot = OSTree.Sysroot.new_default()
            self.sysroot.load(None)
            self.logger.info("Cleaning the sysroot")
            self.sysroot.cleanup(None)

            [_, repo] = self.sysroot.get_repo()
            self.repo_os = repo

        self.remote_name_os = None
        self.repo_containers = self._open_or_create_repo(PATH_REPO_APPS)

    def _configure_dev_paths(self, dev_state_dir):
        global PATH_APPS, PATH_OS, PATH_REPO_OS, PATH_REPO_APPS
        global PATH_SYSTEMD_UNITS, PATH_CURRENT_REVISIONS

        if not dev_state_dir:
            dev_state_dir = os.path.join(os.getcwd(), ".fmu-dev")

        dev_state_dir = os.path.abspath(dev_state_dir)
        PATH_APPS = os.path.join(dev_state_dir, "apps")
        PATH_OS = os.path.join(dev_state_dir, "sysroot")
        PATH_REPO_OS = os.path.join(dev_state_dir, "ostree", "repo")
        PATH_REPO_APPS = os.path.join(PATH_APPS, "ostree_repo")
        PATH_SYSTEMD_UNITS = os.path.join(dev_state_dir, "systemd")
        PATH_CURRENT_REVISIONS = os.path.join(dev_state_dir, "current_revs.json")

        os.makedirs(PATH_APPS, exist_ok=True)
        os.makedirs(PATH_OS, exist_ok=True)
        os.makedirs(os.path.dirname(PATH_REPO_OS), exist_ok=True)
        os.makedirs(PATH_SYSTEMD_UNITS, exist_ok=True)
        os.makedirs(os.path.dirname(PATH_CURRENT_REVISIONS), exist_ok=True)

    def _open_or_create_repo(self, path):
        repo = OSTree.Repo.new(Gio.File.new_for_path(path))
        if os.path.exists(path):
            self.logger.info("Using OSTree repo: %s", path)
            repo.open(None)
        else:
            self.logger.info("Creating OSTree repo: %s", path)
            repo.create(OSTree.RepoMode.ARCHIVE_Z2, None)
        return repo

    def mark_os_successful(self):
        try:
            if subprocess.call(["fw_setenv", "success", "1"]) == 0:
                self.logger.info("Setting success u-boot environment variable to 1 succeeded")
            else:
                self.logger.error("Setting success u-boot environment variable to 1 failed")
        except subprocess.CalledProcessError as e:
            self.logger.error("Ostree rollback post-process commands failed ({})".format(str(e)))

    def check_for_rollback(self, revision):
        try:
            has_rollbacked = False
            deployments = self.sysroot.query_deployments_for(None)
            booted_deployment_rev = self.sysroot.get_booted_deployment().get_csum()

            if booted_deployment_rev != revision:
                has_rollbacked = True
                self.logger.warning("The system rollbacked. Checking if we needed to undeploy")
                if deployments[0] is not None:
                    self.logger.info("There is a pending deployment. Undeploying...")
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
        res = True
        self.ostree_remote_attributes = ostree_remote_attributes

        opts_dict = {
            'gpg-verify': GLib.Variant('b', ostree_remote_attributes['gpg-verify']),
        }

        ca = ostree_remote_attributes.get('tls-ca-path')
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
            os_remote = ostree_remote_attributes['name']
            os_url = ostree_remote_attributes['url']

            self.logger.info("Initalize remotes for the OS ostree: %s", os_remote)

            if os_remote in self.repo_os.remote_list():
                self.logger.info(
                    "OS remote %s already exists, deleting and re-adding with TLS options",
                    os_remote
                )
                self.repo_os.remote_delete(os_remote, None)

            self.repo_os.remote_add(os_remote, os_url, opts, None)
            self.remote_name_os = os_remote

            remote_name = "mad-matisse-gen3-containers"
            containers_url = ostree_remote_attributes['url']

            self.logger.info(
                "Initalize remotes for the containers ostree: %s",
                remote_name
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
        rev = rev.strip()

        try:
            try:
                with open(PATH_CURRENT_REVISIONS, "r") as f:
                    current_revs = json.load(f)
            except FileNotFoundError:
                current_revs = {}

            current_revs[container_name] = rev
            with open(PATH_CURRENT_REVISIONS, "w") as f:
                json.dump(current_revs, f, indent=4)
        except Exception as e:
            self.logger.error(
                "Failed to write current_revs.json for %s: %s",
                container_name, e
            )

        try:
            self.update_container_ref(container_name, rev)
        except Exception as e:
            self.logger.error(
                "Failed to update container ref for %s to %s: %s",
                container_name, rev, e
            )

    def get_previous_rev(self, container_name):
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

    def _read_checked_out_revision(self, container_name):
        marker = os.path.join(PATH_APPS, container_name, ".podkeeper-ostree-commit")
        try:
            with open(marker, "r") as f:
                return f.read().strip()
        except FileNotFoundError:
            return None
        except Exception as e:
            self.logger.warning(
                "Failed reading checked-out revision marker for %s: %s",
                container_name, e
            )
            return None

    def _mark_checkout_done(self, container_name):
        validate_path = os.path.join(PATH_APPS, container_name, VALIDATE_CHECKOUT)
        with open(validate_path, "a"):
            pass

    def _checkout_required(self, container_name, target_rev):
        full_path = os.path.join(PATH_APPS, container_name)
        validate_path = os.path.join(full_path, VALIDATE_CHECKOUT)

        if not os.path.isdir(full_path):
            self.logger.info(
                "Checkout required for %s because %s is missing",
                container_name, full_path
            )
            return True

        checked_out_rev = self._read_checked_out_revision(container_name)

        if os.path.isfile(validate_path):
            if checked_out_rev and target_rev and checked_out_rev != target_rev:
                self.logger.info(
                    "Checkout required for %s because checked-out rev %s != target rev %s",
                    container_name, checked_out_rev, target_rev
                )
                return True

            self.logger.info(
                "Skipping checkout for %s because %s exists",
                container_name, validate_path
            )
            return False

        if checked_out_rev and target_rev and checked_out_rev == target_rev:
            self.logger.info(
                "Skipping checkout for %s because existing checkout rev %s already matches target rev; creating %s",
                container_name, checked_out_rev, validate_path
            )
            self._mark_checkout_done(container_name)
            return False

        self.logger.info(
            "Checkout required for %s because %s is missing and existing rev is %s while target rev is %s",
            container_name, validate_path, checked_out_rev, target_rev
        )
        return True

    def init_checkout_existing_containers(self):
        res = True
        self.logger.info("Getting refs from repo:{}".format(PATH_REPO_APPS))

        try:
            [_, refs] = self.repo_containers.list_refs(None, None)
            self.logger.info("There are {} containers to be started.".format(len(refs)))

            self.disable_watcher()
            self.disable_podman()

            for ref_name, rev in refs.items():
                container_name = ref_name.split(':')[1] if ':' in ref_name else ref_name

                if self._checkout_required(container_name, rev):
                    self.checkout_container(container_name, rev)
                    self.update_container_ids(container_name)

                    store_dir = os.path.join(PATH_APPS, container_name)
                    whiteout_dir = os.path.join(PATH_APPS, ".whiteout-metadata")
                    self.create_whiteouts(store_dir, whiteout_dir)

                if not res:
                    self.logger.error("Error when checking out container:{}".format(container_name))
                    break

                self.create_unit(container_name)

            self.systemd.Reload()

            self.enable_watcher()
            self.enable_podman()

            for ref_name in refs:
                container_name = ref_name.split(':')[1] if ':' in ref_name else ref_name
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
        self.logger.info("Copy the service file to /etc/systemd/system/{}.service".format(container_name))
        shutil.copy(PATH_APPS + '/' + container_name + '/systemd.service',
                    PATH_SYSTEMD_UNITS + container_name + '.service')

    def start_unit(self, container_name):
        self.logger.info("Enable the container {}".format(container_name))
        self.systemd.EnableUnitFiles([container_name + '.service'], False, False)
        self.logger.info("Since FILE_AUTOSTART is present, start the container using systemd")
        self.systemd.StartUnit(container_name + '.service', "replace")

    def start_service(self, service_name):
        self.logger.info("Enable the service {}".format(service_name))
        self.systemd.EnableUnitFiles([service_name], False, False)
        self.systemd.StartUnit(service_name, "replace")

    def stop_unit(self, container_name):
        self.logger.info("Since FILE_AUTOSTART is not present, stop the container using systemd")
        self.systemd.StopUnit(container_name + '.service', "replace")
        self.logger.info("Disable the container {}".format(container_name))
        self.systemd.DisableUnitFiles([container_name + '.service'], False)

    def stop_service(self, service_name):
        self.logger.info("Disable the service {}".format(service_name))
        self.systemd.StopUnit(service_name, "replace")
        self.systemd.DisableUnitFiles([service_name], False)

    def pull_ostree_ref(self, is_container, ref_sha, ref_name=None):
        res = True

        if is_container:
            repo = self.repo_containers
        else:
            repo = self.repo_os
            ref_name = self.remote_name_os

        try:
            progress = OSTree.AsyncProgress.new()
            progress.connect('changed', OSTree.Repo.pull_default_console_progress_changed, None)

            opts = GLib.Variant(
                'a{sv}',
                {
                    'flags': GLib.Variant('i', OSTree.RepoPullFlags.NONE),
                    'refs': GLib.Variant('as', (ref_sha,)),
                    'depth': GLib.Variant('i', OSTREE_DEPTH),
                }
            )
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
        service = self.systemd.ListUnitsByNames([container_name + '.service'])

        try:
            if service[0][2] == 'not-found':
                opts = GLib.Variant(
                    'a{sv}',
                    {'gpg-verify': GLib.Variant('b', self.ostree_remote_attributes['gpg-verify'])}
                )
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
        self.logger.info("Update the UID and GID of the rootfs")
        os.chown(PATH_APPS + '/' + container_name, CONTAINER_UID, CONTAINER_GID)
        for dirpath, dirnames, filenames in os.walk(PATH_APPS + '/' + container_name):
            for dname in dirnames:
                os.lchown(os.path.join(dirpath, dname), CONTAINER_UID, CONTAINER_GID)
            for fname in filenames:
                os.lchown(os.path.join(dirpath, fname), CONTAINER_UID, CONTAINER_GID)

    def update_container_ref(self, container_name, rev):
        remote = container_name
        branch = container_name
        ref_display = f"{remote}:{branch}"
        try:
            self.logger.info(
                "Updating container remote ref %s to %s in %s",
                ref_display, rev, PATH_REPO_APPS
            )
            try:
                self.repo_containers.set_ref_immediate(
                    remote,
                    branch,
                    rev,
                    None
                )
            except AttributeError:
                self.logger.info(
                    "set_ref_immediate not available, using transaction_set_ref "
                    "for %s -> %s", ref_display, rev
                )
                self.repo_containers.prepare_transaction(None, None)
                self.repo_containers.transaction_set_ref(remote, branch, rev)
                self.repo_containers.commit_transaction(None, None)

            try:
                _, new_csum = self.repo_containers.resolve_rev(ref_display, False)
                self.logger.info(
                    "Container ref %s now points to %s",
                    ref_display, new_csum
                )
            except Exception as e:
                self.logger.warning(
                    "Could not resolve ref %s after update: %s",
                    ref_display, e
                )

        except Exception as e:
            self.logger.error("Failed to update container ref %s: %s", container_name, e)

    def update_os_ref(self, new_rev):
        try:
            booted_dep = self.sysroot.get_booted_deployment()
            if booted_dep is None:
                self.logger.warning("No booted deployment found, cannot update OS ref")
                return

            current_csum = booted_dep.get_csum()
            [_, refs] = self.repo_os.list_refs(None, None)
            os_ref = None

            for ref_name, csum in refs.items():
                if csum == current_csum:
                    os_ref = ref_name
                    break

            if os_ref is None:
                self.logger.warning(
                    "Could not find OS ref matching booted checksum %s; "
                    "not updating OS ref", current_csum
                )
                return

            self.logger.info(
                "Updating OS ref %s from %s to %s",
                os_ref, current_csum, new_rev
            )

            try:
                self.repo_os.set_ref_immediate(
                    None,
                    os_ref,
                    new_rev,
                    None
                )
            except AttributeError:
                self.logger.info(
                    "set_ref_immediate not available, using transaction_set_ref "
                    "for %s -> %s", os_ref, new_rev
                )
                self.repo_os.prepare_transaction(None, None)
                self.repo_os.transaction_set_ref(None, os_ref, new_rev)
                self.repo_os.commit_transaction(None, None)

        except Exception as e:
            self.logger.error("Failed to update OS ref to %s: %s", new_rev, e)

    def handle_container(self, container_name, autostart, autoremove):
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

                if err in (errno.EBUSY, errno.ENOTEMPTY, 39, 16):
                    try:
                        AsyncUpdater._umount_all_under(path, logger)
                        AsyncUpdater._lazy_unmount_container_shm(logger)
                    except Exception as ce:
                        logger.warning("force_rmtree: cleanup helpers failed: %s", ce)

                    time.sleep(1.0 * attempt)
                    continue

                break

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
        res = True
        rootfs_fd = None
        rev = None

        try:
            self.logger.info("Getting rev from repo:{}".format(container_name + ':' + container_name))

            if rev_number is None:
                _, rev = self.repo_containers.resolve_rev(container_name + ':' + container_name, False)[1]
            else:
                rev = rev_number

            self.logger.info("Rev value:{}".format(rev))

            if not self._checkout_required(container_name, rev):
                self.logger.info(
                    "Checkout already valid for %s at rev %s, skipping destructive checkout",
                    container_name, rev
                )
                try:
                    self.set_current_revision(container_name, rev)
                except Exception as e:
                    self.logger.error(
                        "Failed to record current revision %s for %s: %s",
                        rev, container_name, e
                    )
                return

            service = self.systemd.ListUnitsByNames([container_name + '.service'])
            if service[0][2] != 'not-found':
                self.logger.info("Stop the container {}".format(container_name))
                self.stop_unit(container_name)

            self._stop_stack_gracefully(container_name, self.logger, PATH_APPS)

            full_path = os.path.join(PATH_APPS, container_name)
            checkout_tmp = full_path + ".checkout.tmp"

            self._umount_all_under(full_path, self.logger)
            self._umount_all_under(checkout_tmp, self.logger)
            self._lazy_unmount_container_shm(self.logger)

            if os.path.isdir(checkout_tmp):
                if not self._force_rmtree(checkout_tmp, self.logger):
                    raise Exception(f"Failed to cleanup stale {checkout_tmp} before checkout")

            if os.path.isdir(full_path):
                if not self._force_rmtree(full_path, self.logger):
                    raise Exception(f"Failed to cleanup {full_path} before checkout")

            try:
                subprocess.run(
                    [
                        "ostree",
                        "--repo=" + PATH_REPO_APPS,
                        "checkout",
                        "--force-copy",
                        rev,
                        checkout_tmp,
                    ],
                    check=True,
                )

                self._umount_all_under(full_path, self.logger)
                self._umount_all_under(checkout_tmp, self.logger)
                self._lazy_unmount_container_shm(self.logger)

                if os.path.exists(full_path):
                    self.logger.warning(
                        "Destination %s was recreated before rename; cleaning again",
                        full_path
                    )
                    if not self._force_rmtree(full_path, self.logger):
                        raise Exception(f"Failed to cleanup recreated {full_path} before rename")

                os.replace(checkout_tmp, full_path)

            except Exception:
                if os.path.exists(checkout_tmp):
                    self._force_rmtree(checkout_tmp, self.logger)
                raise

            restore_script = os.path.join(full_path, "restore_ostree_special_files.py")
            if os.path.isfile(restore_script):
                subprocess.run(
                    ["python3", restore_script, full_path],
                    check=True,
                )

            with open(os.path.join(full_path, ".podkeeper-ostree-commit"), "w") as f:
                f.write(rev + "\n")

            self._mark_checkout_done(container_name)

            res = True

            try:
                self.set_current_revision(container_name, rev)
            except Exception as e:
                self.logger.error(
                    "Failed to record current revision %s for %s: %s",
                    rev, container_name, e
                )

        except GLib.Error as e:
            self.logger.error("Checking out {} failed ({})".format(container_name, str(e)))
            raise
        finally:
            if rootfs_fd is not None:
                os.close(rootfs_fd)

        if not res:
            raise Exception("Checking out {} failed (returned False)".format(container_name))

    def ostree_stage_tree(self, rev_number):
        try:
            booted_dep = self.sysroot.get_booted_deployment()
            if booted_dep is None:
                raise Exception("Not booted in an OSTree system")

            [res, checksum] = self.repo_os.resolve_rev(rev_number, False)
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
        try:
            self.logger.info("Deleting init_var u-boot environment variable")
            if subprocess.call(["fw_setenv", "init_var"]) != 0:
                self.logger.error("Deleting init_var variable from u-boot environment failed")
            else:
                self.logger.info("Deleting init_var variable from u-boot environment succeeded")
        except subprocess.CalledProcessError as e:
            self.logger.error("Deleting init_var variable from u-boot environment failed ({})".format(e))
            raise
