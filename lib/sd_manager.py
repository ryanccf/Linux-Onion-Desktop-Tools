"""
sd_manager.py - SD card operations for the Onion OS Linux/Debian installer.

Replaces the Windows-specific tools (RMPARTUSB, LockHunter, RemoveDrive,
fat32format) with native Linux equivalents using lsblk, parted, mkfs.vfat,
udisksctl, and related utilities.
"""

import json
import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool paths -- on Debian, sbin tools aren't on the normal user PATH.
# Using absolute paths ensures they're found regardless of PATH config.
# ---------------------------------------------------------------------------
_TOOL_PATHS = {
    "parted": "/sbin/parted",
    "mkfs.vfat": "/sbin/mkfs.vfat",
    "fsck.vfat": "/sbin/fsck.vfat",
    "partprobe": "/sbin/partprobe",
}


def _tool(name: str) -> str:
    """Return the absolute path for a tool, falling back to bare name."""
    path = _TOOL_PATHS.get(name, name)
    if os.path.isfile(path):
        return path
    return shutil.which(name) or name


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_root() -> bool:
    """Return True if the current process is running as root."""
    return os.geteuid() == 0


def _run(cmd: list[str], *, check: bool = False,
         timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a command via subprocess with standard options.

    Parameters
    ----------
    cmd : list[str]
        Command and arguments.
    check : bool
        If True, raise CalledProcessError on non-zero exit.
    timeout : int
        Maximum seconds to wait for the process to finish.

    Returns
    -------
    subprocess.CompletedProcess
    """
    logger.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        logger.debug("stderr: %s", result.stderr.strip())
    if check:
        result.check_returncode()
    return result


def _privileged_run(cmd: list[str], *, check: bool = False,
                    timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a command that requires root privileges.

    If the current user is already root the command is executed directly.
    Otherwise ``pkexec`` is prepended so that the user is prompted for
    authorisation via polkit.
    """
    if _is_root():
        return _run(cmd, check=check, timeout=timeout)
    return _run(["pkexec"] + cmd, check=check, timeout=timeout)


def _device_basename(device: str) -> str:
    """Return the base device name, e.g. 'sdb' from '/dev/sdb' or '/dev/sdb1'."""
    return os.path.basename(device)


def _ensure_block_device(device: str) -> str:
    """Normalise *device* to an absolute ``/dev/...`` path."""
    if not device.startswith("/dev/"):
        device = f"/dev/{device}"
    return device


def _card_size_bytes(device: str) -> int:
    """Return the size of *device* in bytes by reading sysfs."""
    name = _device_basename(device)
    try:
        with open(f"/sys/block/{name}/size") as fh:
            sectors = int(fh.read().strip())
        return sectors * 512
    except (FileNotFoundError, ValueError, OSError):
        return 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_removable_drives() -> list[dict]:
    """Enumerate removable drives visible to the system.

    Uses ``lsblk`` in JSON mode.  Only drives where ``RM=1`` (removable)
    and ``TYPE="disk"`` are included in the result.

    Returns
    -------
    list[dict]
        Each dict contains the keys: ``name``, ``size``, ``type``,
        ``mountpoint``, ``fstype``, ``rm``, ``model``, ``tran``, ``label``,
        and the synthetic ``device`` key (e.g. ``/dev/sdb``).
    """
    result = _run([
        "lsblk", "-J", "-o",
        "NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE,RM,MODEL,TRAN,LABEL",
    ])
    if result.returncode != 0:
        logger.error("lsblk failed: %s", result.stderr.strip())
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.error("Failed to parse lsblk JSON output")
        return []

    drives: list[dict] = []
    for dev in data.get("blockdevices", []):
        # lsblk may report rm as bool or "1"/"0" depending on version.
        rm = dev.get("rm")
        if isinstance(rm, str):
            rm = rm.strip() == "1"
        elif isinstance(rm, (int, float)):
            rm = bool(rm)
        else:
            rm = False

        if not rm:
            continue
        if dev.get("type") != "disk":
            continue

        drive_info = {
            "name": dev.get("name", ""),
            "device": f"/dev/{dev.get('name', '')}",
            "size": dev.get("size", ""),
            "type": dev.get("type", ""),
            "mountpoint": dev.get("mountpoint"),
            "fstype": dev.get("fstype"),
            "rm": True,
            "model": (dev.get("model") or "").strip(),
            "tran": dev.get("tran"),
            "label": dev.get("label"),
            "children": dev.get("children", []),
        }
        drives.append(drive_info)

    return drives


def get_drive_partitions(device: str) -> list[dict]:
    """Return a list of partitions for *device*.

    Parameters
    ----------
    device : str
        Block device path, e.g. ``/dev/sdb``.

    Returns
    -------
    list[dict]
        Each dict contains ``name``, ``device``, ``size``, ``mountpoint``,
        ``fstype``, and ``label``.
    """
    device = _ensure_block_device(device)

    result = _run([
        "lsblk", "-J", "-o",
        "NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE,LABEL",
        device,
    ])
    if result.returncode != 0:
        logger.error("lsblk failed for %s: %s", device, result.stderr.strip())
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.error("Failed to parse lsblk JSON output for %s", device)
        return []

    partitions: list[dict] = []
    for dev in data.get("blockdevices", []):
        for child in dev.get("children", []):
            if child.get("type") == "part":
                partitions.append({
                    "name": child.get("name", ""),
                    "device": f"/dev/{child.get('name', '')}",
                    "size": child.get("size", ""),
                    "mountpoint": child.get("mountpoint"),
                    "fstype": child.get("fstype"),
                    "label": child.get("label"),
                })
    return partitions


def detect_sd_state(mount_point: str) -> str:
    """Determine what is currently on the SD card at *mount_point*.

    Returns
    -------
    str
        ``"onion"``   -- ``.tmp_update`` folder is present (Onion OS).
        ``"stock"``   -- ``miyoo`` folder is present without ``.tmp_update``
                         (stock Miyoo Mini firmware).
        ``"empty"``   -- The mount point exists but contains no files.
        ``"unknown"`` -- Files are present but they don't match a known
                         layout.
    """
    if not os.path.isdir(mount_point):
        return "unknown"

    try:
        entries = os.listdir(mount_point)
    except OSError:
        return "unknown"

    # Filter out common hidden/system artefacts that don't count as real
    # content (e.g. ``System Volume Information``, ``.Trash-1000``).
    meaningful = [
        e for e in entries
        if e not in {"System Volume Information", ".Trash-1000",
                     "$RECYCLE.BIN", ".fseventsd", ".Spotlight-V100"}
    ]

    if not meaningful:
        return "empty"

    if ".tmp_update" in entries:
        return "onion"

    if "miyoo" in entries and ".tmp_update" not in entries:
        return "stock"

    return "unknown"


def get_onion_version(mount_point: str) -> str | None:
    """Read the installed Onion version from the SD card.

    Looks for ``.tmp_update/onionVersion/version.txt`` under *mount_point*.

    Returns
    -------
    str or None
        The version string, or ``None`` if the file does not exist.
    """
    version_file = os.path.join(
        mount_point, ".tmp_update", "onionVersion", "version.txt"
    )
    try:
        with open(version_file) as fh:
            return fh.read().strip()
    except (FileNotFoundError, OSError):
        return None


def _partition_device_for(device: str) -> str:
    """Return the first-partition device node for a whole-disk *device*.

    For ``/dev/sdb`` this returns ``/dev/sdb1``;
    for ``/dev/mmcblk0`` it returns ``/dev/mmcblk0p1``.
    """
    base = _device_basename(device)
    if base[-1].isdigit():
        return f"{device}p1"
    return f"{device}1"


def format_sd_card(device: str, label: str = "Onion") -> tuple[bool, str]:
    """Format *device* as FAT32 with an MBR partition table.

    All privileged operations are batched into a single shell script so that
    the user is only prompted for authentication **once** via pkexec.

    Parameters
    ----------
    device : str
        Whole-disk device path, e.g. ``/dev/sdb``.
    label : str
        Volume label (max 11 ASCII characters for FAT32).

    Returns
    -------
    tuple[bool, str]
        ``(True, message)`` on success, ``(False, error_description)`` on
        failure.
    """
    device = _ensure_block_device(device)
    label = label[:11].upper()

    partition_device = _partition_device_for(device)

    size_bytes = _card_size_bytes(device)
    cluster_sectors = "128" if size_bytes > 137_438_953_472 else "64"

    # Unmount via udisksctl first (no root needed) -------------------------
    partitions = get_drive_partitions(device)
    for part in partitions:
        if part.get("mountpoint"):
            _run(["udisksctl", "unmount", "-b", part["device"]])

    # Build a single script with all privileged commands --------------------
    script = f"""#!/bin/sh
set -e

# Unmount anything still mounted (belt and suspenders)
for p in {device}*; do
    umount "$p" 2>/dev/null || true
done

# Create MBR partition table
{_tool("parted")} -s {device} mklabel msdos

# Create single FAT32 partition
{_tool("parted")} -s -a optimal {device} mkpart primary fat32 1MiB 100%

# Tell the kernel about the new partition table
{_tool("partprobe")} {device}
udevadm settle --timeout=5
sleep 1

# Format as FAT32
{_tool("mkfs.vfat")} -F32 -s {cluster_sectors} -n {label} {partition_device}

# Final settle so the new filesystem is recognized
udevadm settle --timeout=5
"""

    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write(script)
        script_path = f.name

    try:
        os.chmod(script_path, 0o755)
        res = _privileged_run([script_path], timeout=300)
        if res.returncode != 0:
            error = res.stderr.strip() or res.stdout.strip()
            return False, f"Format failed: {error}"
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass

    return True, f"Successfully formatted {device} as FAT32 (label={label})"


def check_disk(device: str) -> str:
    """Run a non-destructive filesystem check on the first partition of *device*.

    Uses ``fsck.vfat -n`` (no-write / read-only check).

    Parameters
    ----------
    device : str
        Whole-disk device path (e.g. ``/dev/sdb``).  The first partition
        (``/dev/sdb1`` or ``/dev/sdb0p1``) is checked automatically.

    Returns
    -------
    str
        Combined stdout and stderr from ``fsck.vfat``.
    """
    device = _ensure_block_device(device)
    partition_device = _partition_device_for(device)

    # Unmount first to avoid "filesystem is mounted" warnings.
    partitions = get_drive_partitions(device)
    for part in partitions:
        if part.get("mountpoint") and part["device"] == partition_device:
            _run(["udisksctl", "unmount", "-b", partition_device])

    res = _privileged_run([_tool("fsck.vfat"), "-n", partition_device], timeout=300)
    output = (res.stdout + "\n" + res.stderr).strip()
    return output


def eject_drive(device: str) -> tuple[bool, str]:
    """Safely eject *device* (unmount + power-off).

    First all mounted partitions are unmounted, then ``udisksctl power-off``
    is attempted.  If that fails, ``eject`` is used as a fallback.

    Parameters
    ----------
    device : str
        Whole-disk device path, e.g. ``/dev/sdb``.

    Returns
    -------
    tuple[bool, str]
        ``(True, message)`` on success, ``(False, error_description)`` on
        failure.
    """
    device = _ensure_block_device(device)

    # Unmount every partition.
    partitions = get_drive_partitions(device)
    for part in partitions:
        if part.get("mountpoint"):
            res = _run(["udisksctl", "unmount", "-b", part["device"]])
            if res.returncode != 0:
                # Fallback to plain umount.
                res = _privileged_run(["umount", part["device"]])
                if res.returncode != 0:
                    return False, f"Failed to unmount {part['device']}: {res.stderr.strip()}"

    # Power-off via udisksctl (preferred -- does not require root).
    res = _run(["udisksctl", "power-off", "-b", device])
    if res.returncode == 0:
        return True, f"Drive {device} has been safely ejected."

    # Fallback: eject.
    if shutil.which("eject"):
        res = _privileged_run(["eject", device])
        if res.returncode == 0:
            return True, f"Drive {device} has been ejected (via eject)."
        return False, f"Failed to eject {device}: {res.stderr.strip()}"

    return False, f"Failed to power-off {device}: {res.stderr.strip()}"


def mount_partition(partition: str) -> str | None:
    """Mount *partition* via ``udisksctl`` and return the mount point.

    ``udisksctl`` mounts the filesystem under ``/media/<user>/...``
    automatically.

    Parameters
    ----------
    partition : str
        Partition device path, e.g. ``/dev/sdb1``.

    Returns
    -------
    str or None
        The mount point path, or ``None`` if mounting failed.
    """
    partition = _ensure_block_device(partition)

    res = _run(["udisksctl", "mount", "-b", partition])
    if res.returncode != 0:
        logger.error("mount failed for %s: %s", partition, res.stderr.strip())
        return None

    # udisksctl prints something like: "Mounted /dev/sdb1 at /media/user/Onion"
    stdout = res.stdout.strip()
    if " at " in stdout:
        mount_point = stdout.split(" at ", 1)[1].rstrip(".")
        return mount_point

    # If parsing fails, query lsblk for the mount point.
    info = _run(["lsblk", "-n", "-o", "MOUNTPOINT", partition])
    mp = info.stdout.strip()
    return mp if mp else None


def unmount_partition(partition: str) -> tuple[bool, str]:
    """Unmount *partition* via ``udisksctl``.

    Parameters
    ----------
    partition : str
        Partition device path, e.g. ``/dev/sdb1``.

    Returns
    -------
    tuple[bool, str]
        ``(True, message)`` on success, ``(False, error_description)`` on
        failure.
    """
    partition = _ensure_block_device(partition)

    res = _run(["udisksctl", "unmount", "-b", partition])
    if res.returncode == 0:
        return True, f"Unmounted {partition}."

    # Fallback to umount.
    res = _privileged_run(["umount", partition])
    if res.returncode == 0:
        return True, f"Unmounted {partition} (via umount)."

    return False, f"Failed to unmount {partition}: {res.stderr.strip()}"


def get_free_space(path: str) -> int:
    """Return the free space in bytes available at *path*.

    Uses ``os.statvfs`` -- no subprocess required.

    Parameters
    ----------
    path : str
        Any path on the mounted filesystem (e.g. the mount point).

    Returns
    -------
    int
        Free space in bytes available to a non-privileged user.
        Returns ``0`` if the path is invalid or an error occurs.
    """
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize
    except OSError:
        return 0
