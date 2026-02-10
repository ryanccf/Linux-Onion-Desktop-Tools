"""
Onion OS Emulator / Package Manager

Manages emulator and application packages on the SD card. Replaces the
Windows PowerShell script Onion_Config_01_Emulators.ps1.

Packages are staged under App/PackageManager/data/{Emu,RApp,App}/ on
the SD card and are "installed" by copying their directory tree to the
SD card root. ROM directories are never removed during uninstallation.
"""

import logging
import shutil
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

PACKAGE_DATA_DIR = "App/PackageManager/data"
PACKAGE_TYPES = ("Emu", "RApp", "App")

# Maps package type subdirectory names to canonical short names used in
# return values and external APIs.
_TYPE_MAP: dict[str, str] = {
    "Emu": "emu",
    "RApp": "rapp",
    "App": "app",
}


def _rom_dir_for_package(sd_mount: Path, package_name: str) -> Path | None:
    """Return the ROM directory path that corresponds to a package name.

    By Onion convention the ROM folder lives at {sd_mount}/Roms/{package_name}.
    Returns the path (which may or may not exist on disk).

    Args:
        sd_mount: Mount point of the SD card.
        package_name: Name of the emulator package (e.g. "GBA", "SFC").

    Returns:
        Path to the ROM directory, or None if the mapping cannot be
        determined.
    """
    return sd_mount / "Roms" / package_name


def _has_roms(sd_mount: Path, package_name: str) -> bool:
    """Check whether the ROM folder for a given package contains any files.

    Only regular files are counted (not subdirectories or symlinks to
    directories), and hidden files (starting with '.') are skipped since
    they are typically metadata.

    Args:
        sd_mount: Mount point of the SD card.
        package_name: Name of the package.

    Returns:
        True if at least one non-hidden file exists in the ROM directory.
    """
    rom_dir = _rom_dir_for_package(sd_mount, package_name)
    if rom_dir is None or not rom_dir.is_dir():
        return False

    try:
        for entry in rom_dir.iterdir():
            if entry.is_file() and not entry.name.startswith("."):
                return True
    except PermissionError:
        logger.warning(
            "Permission denied reading ROM directory: %s", rom_dir
        )

    return False


def _is_installed(sd_mount: Path, package_name: str, type_dir: str) -> bool:
    """Check whether a package is installed on the SD card root.

    An installed package has its directory present at
    {sd_mount}/{type_dir}/{package_name}/.

    Args:
        sd_mount: Mount point of the SD card.
        package_name: Name of the package.
        type_dir: Top-level type directory on the SD card (e.g. "Emu",
            "RApp", "App").

    Returns:
        True if the package directory exists on the SD root.
    """
    installed_path = sd_mount / type_dir / package_name
    return installed_path.is_dir()


def scan_packages(
    sd_mount: Path,
) -> list[dict[str, str | bool]]:
    """Scan App/PackageManager/data/ for available packages.

    Looks in Emu/, RApp/, and App/ subdirectories within the package data
    directory on the SD card.

    Args:
        sd_mount: Mount point of the SD card.

    Returns:
        List of dicts, each containing:
            - name (str): Package directory name.
            - type (str): One of "emu", "rapp", "app".
            - available (bool): True if the package exists in staging.
            - installed (bool): True if installed at the SD root.
            - has_roms (bool): True if a matching ROM folder has files.
    """
    sd_mount = Path(sd_mount)
    data_root = sd_mount / PACKAGE_DATA_DIR
    packages: list[dict[str, str | bool]] = []

    for type_dir in PACKAGE_TYPES:
        type_path = data_root / type_dir
        short_type = _TYPE_MAP[type_dir]

        if not type_path.is_dir():
            logger.debug(
                "Package type directory does not exist: %s", type_path
            )
            continue

        try:
            entries = sorted(type_path.iterdir())
        except PermissionError:
            logger.warning(
                "Permission denied reading package directory: %s", type_path
            )
            continue

        for entry in entries:
            if not entry.is_dir():
                continue

            package_name = entry.name
            installed = _is_installed(sd_mount, package_name, type_dir)
            roms = _has_roms(sd_mount, package_name)

            packages.append(
                {
                    "name": package_name,
                    "type": short_type,
                    "available": True,
                    "installed": installed,
                    "has_roms": roms,
                }
            )
            logger.debug(
                "Found package: %s (type=%s, installed=%s, has_roms=%s)",
                package_name,
                short_type,
                installed,
                roms,
            )

    logger.info(
        "Scanned %d packages across %d type directories",
        len(packages),
        len(PACKAGE_TYPES),
    )
    return packages


def install_package(
    sd_mount: Path, package_name: str, package_type: str
) -> tuple[bool, str]:
    """Install a package by copying its directory from the staging area
    to the SD card root.

    The source is App/PackageManager/data/{TypeDir}/{package_name}/ and
    the destination is {sd_mount}/{TypeDir}/{package_name}/.

    Args:
        sd_mount: Mount point of the SD card.
        package_name: Name of the package (directory name).
        package_type: Package type, one of "emu", "rapp", "app".

    Returns:
        Tuple of (success: bool, message: str).
    """
    sd_mount = Path(sd_mount)

    # Resolve the type directory name from the short type
    type_dir = _resolve_type_dir(package_type)
    if type_dir is None:
        return False, f"Unknown package type: {package_type!r}"

    source = sd_mount / PACKAGE_DATA_DIR / type_dir / package_name
    destination = sd_mount / type_dir / package_name

    if not source.is_dir():
        return False, (
            f"Package source not found: {source}. "
            f"Ensure the package data is present on the SD card."
        )

    if destination.is_dir():
        return False, (
            f"Package already installed at {destination}. "
            f"Uninstall first if you want to reinstall."
        )

    try:
        # Ensure the parent type directory exists
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
        logger.info(
            "Installed package %s (%s): %s -> %s",
            package_name,
            package_type,
            source,
            destination,
        )
        return True, f"Successfully installed {package_name}"
    except PermissionError as e:
        msg = f"Permission denied installing {package_name}: {e}"
        logger.error(msg)
        return False, msg
    except OSError as e:
        msg = f"Failed to install {package_name}: {e}"
        logger.error(msg)
        return False, msg


def uninstall_package(
    sd_mount: Path, package_name: str, package_type: str
) -> tuple[bool, str]:
    """Remove a package directory from the SD card root.

    ROM files are NOT removed -- only the package directory under the
    type directory (e.g. Emu/GBA/) is deleted.

    Args:
        sd_mount: Mount point of the SD card.
        package_name: Name of the package (directory name).
        package_type: Package type, one of "emu", "rapp", "app".

    Returns:
        Tuple of (success: bool, message: str).
    """
    sd_mount = Path(sd_mount)

    type_dir = _resolve_type_dir(package_type)
    if type_dir is None:
        return False, f"Unknown package type: {package_type!r}"

    target = sd_mount / type_dir / package_name

    if not target.is_dir():
        return False, (
            f"Package {package_name} is not installed "
            f"(directory not found: {target})"
        )

    try:
        shutil.rmtree(target)
        logger.info(
            "Uninstalled package %s (%s): removed %s",
            package_name,
            package_type,
            target,
        )
        return True, f"Successfully uninstalled {package_name}"
    except PermissionError as e:
        msg = f"Permission denied uninstalling {package_name}: {e}"
        logger.error(msg)
        return False, msg
    except OSError as e:
        msg = f"Failed to uninstall {package_name}: {e}"
        logger.error(msg)
        return False, msg


def auto_install(sd_mount: Path) -> list[str]:
    """Automatically install all emulators whose matching ROM folders
    contain files.

    Only packages of type "emu" are considered for auto-install.

    Args:
        sd_mount: Mount point of the SD card.

    Returns:
        List of package names that were successfully installed.
    """
    sd_mount = Path(sd_mount)
    installed_names: list[str] = []

    packages = scan_packages(sd_mount)

    for pkg in packages:
        if pkg["type"] != "emu":
            continue
        if pkg["installed"]:
            logger.debug(
                "Skipping %s: already installed", pkg["name"]
            )
            continue
        if not pkg["has_roms"]:
            logger.debug(
                "Skipping %s: no ROMs found", pkg["name"]
            )
            continue

        name = str(pkg["name"])
        success, message = install_package(sd_mount, name, "emu")
        if success:
            installed_names.append(name)
            logger.info("Auto-installed: %s", name)
        else:
            logger.warning(
                "Auto-install failed for %s: %s", name, message
            )

    logger.info(
        "Auto-install complete: %d packages installed", len(installed_names)
    )
    return installed_names


def get_package_status_color(
    package: dict[str, str | bool],
) -> Literal["green", "orange", "white"]:
    """Return a UI color string based on a package's status.

    Args:
        package: A package dict as returned by scan_packages().

    Returns:
        "green" if the package is installed, "orange" if ROMs are present
        but the package is not installed, "white" otherwise.
    """
    if package.get("installed"):
        return "green"
    if package.get("has_roms"):
        return "orange"
    return "white"


def _resolve_type_dir(package_type: str) -> str | None:
    """Resolve a short package type name to its directory name.

    Args:
        package_type: One of "emu", "rapp", "app" (case-insensitive).

    Returns:
        The directory name (e.g. "Emu", "RApp", "App"), or None if the
        type is unrecognized.
    """
    reverse_map = {v: k for k, v in _TYPE_MAP.items()}
    return reverse_map.get(package_type.lower())
