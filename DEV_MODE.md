# Dev mode

## System libraries

For Ubuntu:

```bash
sudo apt install \
    libcairo2-dev \
    libgirepository1.0-dev \
    gobject-introspection \
    pkg-config \
    gir1.2-ostree-1.0 \
    ostree
```

`PyGObject==3.42.0` expects `girepository.h` from `libgirepository1.0-dev`.
On Ubuntu 24.04, `libgirepository-2.0-dev` alone is not enough for this pinned
version.

## Python 3.10

It's recommended to use Python 3.10 specifically because it's included in our Yocto builds.

For Ubuntu based machines:

```bash
sudo apt update
sudo apt install software-properties-common

sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt update

sudo apt install python3.10 python3.10-venv python3.10-dev
```

This will keep your `python3` command pointing at your latest python3 version (python3.12, for example). But will make available python3.10 so you can work on this project.

## Setup venv & packages

Init python3 venv:

```bash
python3.10 -m venv --system-site-packages .venv
```

Activate venv:

```bash
source ./.venv/bin/activate
```

Install python packages:

```bash
python -m pip install pycairo==1.21.0
python -m pip install --no-build-isolation PyGObject==3.42.0
python -m pip install -r requirements.txt
```

Once finished working, exit venv:

```bash
deactivate
```

## Limited `/apps` partition

For testing app update size checks on a dev machine, mount `/apps` as a limited
`tmpfs` filesystem:

```bash
sudo mkdir -p /apps
sudo mount -t tmpfs -o size=11M tmpfs /apps
df -h /apps
```

Change `128M` to the size limit you need for the test.

Unmount it when finished:

```bash
sudo umount /apps
```

If `/apps` is busy, check which process is using it, stop that process or leave
the directory, then retry:

```bash
sudo lsof +f -- /apps
cd /
sudo umount /apps
```

`tmpfs` contents are temporary and disappear when unmounted or after reboot.

## Limited `/sysroot` partition

For testing OS update size checks on a development machine, mount `/sysroot` as
a limited `tmpfs` filesystem. Do not do this on a target device or a host that
uses `/sysroot` for its real operating system.

First, check whether `/sysroot` is already a mount point:

```bash
findmnt /sysroot
```

If this shows an existing system mount, do not replace it. On a development
machine where `/sysroot` is unused, create and mount the test filesystem:

```bash
sudo mkdir -p /sysroot
sudo mount -t tmpfs -o size=12M tmpfs /sysroot
findmnt /sysroot
df -h /sysroot
```

Change `128M` to the partition size needed for the test. For an OS Distribution
Set, set `type` to `os` and provide a non-zero `totalSizeMB` in its `info`
metadata. The free-space check includes `UPDATE_GAP_PERCENT` (currently 10%), so
an update with `totalSizeMB` set to `120` requires at least `132 MB` free and
should be rejected by a `128M` test filesystem.

Unmount the test filesystem when finished:

```bash
sudo umount /sysroot
```

If `/sysroot` is busy, check which process is using it, stop that process or
leave the directory, then retry:

```bash
sudo lsof +f -- /sysroot
cd /
sudo umount /sysroot
```

Mounting the `tmpfs` temporarily hides existing files under `/sysroot` without
deleting them. Its own contents disappear when it is unmounted or after reboot.

## Debugging

Debug your app by launching `./.test/fullmetalupdate.sh`
