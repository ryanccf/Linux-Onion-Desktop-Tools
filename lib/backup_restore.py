"""
Backup and restore operations for the Onion OS installer.

Replaces the Windows PowerShell scripts Onion_Save_Backup.ps1 and
Onion_Save_Restore.ps1.  The original scripts relied on robocopy with
progress tracking; this module uses shutil / os / pathlib for portable
file operations on Linux/Debian.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Category definitions
# ---------------------------------------------------------------------------
# Each key is a short machine-readable identifier used by the UI and stored
# in backup_info.json.  ``label`` is the human-readable name, ``path`` is
# the relative path on the SD card root.
BACKUP_CATEGORIES = {
    "roms": {"label": "ROMs", "path": "Roms"},
    "imgs": {"label": "Images (box art)", "path": "Imgs"},
    "saves": {"label": "Saves", "path": "Saves"},
    "ra_config": {"label": "RetroArch config", "path": "RetroArch/.retroarch"},
    "bios": {"label": "BIOS", "path": "BIOS"},
    "onion_config": {"label": "Onion config", "path": ".tmp_update/config"},
}

# Mapping used by ``migrate_stock_to_onion`` to relocate stock Miyoo save
# data into the directory layout expected by Onion OS.
_STOCK_TO_ONION_MAPPINGS: list[dict[str, str]] = [
    {
        "stock": "RetroArch/.retroarch/saves",
        "onion": "Saves/CurrentProfile/saves",
    },
    {
        "stock": "RetroArch/.retroarch/states",
        "onion": "Saves/CurrentProfile/states",
    },
]

# Type alias for the progress callback accepted by several functions.
ProgressCallback = Optional[
    Callable[[str, str, int, int], None]
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detect_sd_state(sd_mount: Path) -> str:
    """Return ``'onion'``, ``'stock'``, or ``'unknown'`` for an SD card."""
    if (sd_mount / ".tmp_update").is_dir():
        return "onion"
    if (sd_mount / "miyoo" / "app").is_dir() or (sd_mount / "miyoo").is_dir():
        return "stock"
    return "unknown"


def _detect_onion_version(sd_mount: Path) -> str:
    """Try to read the Onion version string from the SD card.

    Onion stores its version in ``.tmp_update/onionVersion/version.txt``
    (or similar paths depending on the release).  Returns an empty string
    when the version cannot be determined.
    """
    candidates = [
        sd_mount / ".tmp_update" / "onionVersion" / "version.txt",
        sd_mount / ".tmp_update" / "config" / "version.txt",
        sd_mount / ".tmp_update" / "version.txt",
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                version = candidate.read_text(encoding="utf-8").strip()
                if version:
                    return version
        except OSError:
            continue
    return ""


# ---------------------------------------------------------------------------
# Core public API
# ---------------------------------------------------------------------------

def count_files(directory: Path | str) -> int:
    """Recursively count files inside *directory*.

    Returns ``0`` when *directory* does not exist or is not a directory.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return 0
    total = 0
    for item in directory.rglob("*"):
        if item.is_file():
            total += 1
    return total


def copy_tree_with_progress(
    src: Path | str,
    dst: Path | str,
    progress_callback: ProgressCallback = None,
    *,
    _category: str = "",
    _files_done: int = 0,
    _total_files: int = 0,
) -> int:
    """Copy a directory tree from *src* to *dst* with per-file progress.

    Individual files are copied with :func:`shutil.copy2` so that file
    metadata (timestamps, permissions) is preserved.

    Parameters
    ----------
    src:
        Source directory.
    dst:
        Destination directory.  Created if it does not exist.
    progress_callback:
        ``(category, current_file, files_done, total_files) -> None``
    _category:
        Internal -- passed through to the progress callback.
    _files_done:
        Internal -- running count of files already copied before this call.
    _total_files:
        Internal -- total file count for progress reporting.

    Returns
    -------
    int
        Number of files copied during this call.
    """
    src = Path(src)
    dst = Path(dst)

    if not src.is_dir():
        return 0

    dst.mkdir(parents=True, exist_ok=True)

    copied = 0
    for item in sorted(src.rglob("*")):
        if not item.is_file():
            continue

        relative = item.relative_to(src)
        dest_file = dst / relative
        dest_file.parent.mkdir(parents=True, exist_ok=True)

        shutil.copy2(item, dest_file)
        copied += 1

        if progress_callback is not None:
            progress_callback(
                _category,
                str(relative),
                _files_done + copied,
                _total_files,
            )

    return copied


def create_backup(
    sd_mount: str | Path,
    backup_dir: str | Path,
    categories: list[str],
    description: str = "",
    progress_callback: ProgressCallback = None,
) -> tuple[bool, str, str]:
    """Back up selected categories from the SD card.

    A timestamped sub-directory is created under *backup_dir* with the
    format ``YYYYMMDD_HHMMSS_{state}_{version}`` (version is omitted when
    it cannot be determined).

    Parameters
    ----------
    sd_mount:
        Mount point of the SD card.
    backup_dir:
        Parent directory under which the backup sub-directory is created.
    categories:
        List of category keys (must be present in :data:`BACKUP_CATEGORIES`).
    description:
        Optional human-readable description stored in metadata.
    progress_callback:
        ``(category, current_file, files_done, total_files) -> None``

    Returns
    -------
    tuple[bool, str, str]
        ``(success, backup_path, message)``
    """
    sd_mount = Path(sd_mount)
    backup_dir = Path(backup_dir)

    if not sd_mount.is_dir():
        return False, "", f"SD card mount point does not exist: {sd_mount}"

    # Validate categories -------------------------------------------------
    invalid = [c for c in categories if c not in BACKUP_CATEGORIES]
    if invalid:
        return False, "", f"Unknown backup categories: {', '.join(invalid)}"

    if not categories:
        return False, "", "No categories selected for backup."

    # Detect state / version ----------------------------------------------
    state = _detect_sd_state(sd_mount)
    version = _detect_onion_version(sd_mount)

    # Build directory name ------------------------------------------------
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dir_name = f"{timestamp}_{state}"
    if version:
        # Sanitise version so it is file-system safe.
        safe_version = version.replace("/", "_").replace("\\", "_").replace(" ", "_")
        dir_name = f"{dir_name}_{safe_version}"

    backup_path = backup_dir / dir_name
    try:
        backup_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, "", f"Failed to create backup directory: {exc}"

    # Count total files for progress reporting ----------------------------
    total_files = 0
    for cat_key in categories:
        src_dir = sd_mount / BACKUP_CATEGORIES[cat_key]["path"]
        total_files += count_files(src_dir)

    # Copy each category --------------------------------------------------
    files_done = 0
    backed_up_categories: list[str] = []

    try:
        for cat_key in categories:
            cat_info = BACKUP_CATEGORIES[cat_key]
            src_dir = sd_mount / cat_info["path"]

            if not src_dir.is_dir():
                logger.info(
                    "Skipping category '%s': source directory does not exist (%s)",
                    cat_key,
                    src_dir,
                )
                continue

            dst_dir = backup_path / cat_info["path"]

            copied = copy_tree_with_progress(
                src_dir,
                dst_dir,
                progress_callback,
                _category=cat_key,
                _files_done=files_done,
                _total_files=total_files,
            )
            files_done += copied
            backed_up_categories.append(cat_key)
    except Exception as exc:
        logger.exception("Backup failed during file copy.")
        return False, str(backup_path), f"Backup failed: {exc}"

    # Write metadata ------------------------------------------------------
    metadata = {
        "date": datetime.now().isoformat(),
        "categories": backed_up_categories,
        "description": description,
        "state": state,
        "version": version,
        "total_files": files_done,
    }

    info_path = backup_path / "backup_info.json"
    try:
        info_path.write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Could not write backup_info.json: %s", exc)

    return (
        True,
        str(backup_path),
        f"Backup completed: {files_done} files in {len(backed_up_categories)} categories.",
    )


def list_backups(backup_dir: str | Path) -> list[dict]:
    """List all backups found under *backup_dir*.

    Each backup is expected to contain a ``backup_info.json`` file.
    Directories without that file are silently ignored.

    Returns a list of dicts sorted newest-first with keys:
    ``path``, ``date``, ``categories``, ``description``, ``state``,
    ``version``.
    """
    backup_dir = Path(backup_dir)
    results: list[dict] = []

    if not backup_dir.is_dir():
        return results

    for entry in sorted(backup_dir.iterdir(), reverse=True):
        if not entry.is_dir():
            continue

        info_file = entry / "backup_info.json"
        if not info_file.is_file():
            continue

        try:
            data = json.loads(info_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Skipping backup '%s': %s", entry.name, exc)
            continue

        results.append(
            {
                "path": str(entry),
                "date": data.get("date", ""),
                "categories": data.get("categories", []),
                "description": data.get("description", ""),
                "state": data.get("state", "unknown"),
                "version": data.get("version", ""),
            }
        )

    return results


def restore_backup(
    backup_path: str | Path,
    sd_mount: str | Path,
    categories: list[str],
    progress_callback: ProgressCallback = None,
) -> tuple[bool, str]:
    """Restore selected categories from a backup to the SD card.

    Parameters
    ----------
    backup_path:
        Path to the backup directory (one produced by :func:`create_backup`).
    sd_mount:
        Mount point of the target SD card.
    categories:
        List of category keys to restore.
    progress_callback:
        ``(category, current_file, files_done, total_files) -> None``

    Returns
    -------
    tuple[bool, str]
        ``(success, message)``
    """
    backup_path = Path(backup_path)
    sd_mount = Path(sd_mount)

    if not backup_path.is_dir():
        return False, f"Backup path does not exist: {backup_path}"

    if not sd_mount.is_dir():
        return False, f"SD card mount point does not exist: {sd_mount}"

    # Validate categories -------------------------------------------------
    invalid = [c for c in categories if c not in BACKUP_CATEGORIES]
    if invalid:
        return False, f"Unknown restore categories: {', '.join(invalid)}"

    if not categories:
        return False, "No categories selected for restore."

    # Count total files for progress reporting ----------------------------
    total_files = 0
    for cat_key in categories:
        src_dir = backup_path / BACKUP_CATEGORIES[cat_key]["path"]
        total_files += count_files(src_dir)

    # Copy each category --------------------------------------------------
    files_done = 0
    restored_categories: list[str] = []

    try:
        for cat_key in categories:
            cat_info = BACKUP_CATEGORIES[cat_key]
            src_dir = backup_path / cat_info["path"]

            if not src_dir.is_dir():
                logger.info(
                    "Skipping category '%s': not present in backup (%s)",
                    cat_key,
                    src_dir,
                )
                continue

            dst_dir = sd_mount / cat_info["path"]

            copied = copy_tree_with_progress(
                src_dir,
                dst_dir,
                progress_callback,
                _category=cat_key,
                _files_done=files_done,
                _total_files=total_files,
            )
            files_done += copied
            restored_categories.append(cat_key)
    except Exception as exc:
        logger.exception("Restore failed during file copy.")
        return False, f"Restore failed: {exc}"

    return (
        True,
        f"Restore completed: {files_done} files in {len(restored_categories)} categories.",
    )


def get_backup_size(
    backup_path: str | Path,
    categories: list[str],
) -> int:
    """Return the total size in bytes of the given categories in a backup.

    Categories whose directories do not exist in the backup are silently
    skipped (contribute 0 bytes).
    """
    backup_path = Path(backup_path)
    total = 0

    for cat_key in categories:
        if cat_key not in BACKUP_CATEGORIES:
            continue
        cat_dir = backup_path / BACKUP_CATEGORIES[cat_key]["path"]
        if not cat_dir.is_dir():
            continue
        for item in cat_dir.rglob("*"):
            if item.is_file():
                try:
                    total += item.stat().st_size
                except OSError:
                    pass

    return total


def migrate_stock_to_onion(
    stock_mount: str | Path,
    onion_mount: str | Path,
    progress_callback: ProgressCallback = None,
) -> tuple[bool, str]:
    """Migrate data from a stock Miyoo SD card to an Onion OS SD card.

    The primary job is to remap save data from the stock directory layout
    (``RetroArch/.retroarch/saves``, ``RetroArch/.retroarch/states``) to
    the Onion directory layout (``Saves/CurrentProfile/saves``,
    ``Saves/CurrentProfile/states``).

    In addition, ROM and BIOS directories are copied across if they exist,
    since the directory names are the same on both layouts.

    Parameters
    ----------
    stock_mount:
        Mount point of the stock Miyoo SD card.
    onion_mount:
        Mount point of the target Onion SD card.
    progress_callback:
        ``(category, current_file, files_done, total_files) -> None``

    Returns
    -------
    tuple[bool, str]
        ``(success, message)``
    """
    stock_mount = Path(stock_mount)
    onion_mount = Path(onion_mount)

    if not stock_mount.is_dir():
        return False, f"Stock SD mount point does not exist: {stock_mount}"
    if not onion_mount.is_dir():
        return False, f"Onion SD mount point does not exist: {onion_mount}"

    # Build a complete list of (src, dst, label) pairs --------------------
    copy_jobs: list[tuple[Path, Path, str]] = []

    # Stock-to-Onion save remapping
    for mapping in _STOCK_TO_ONION_MAPPINGS:
        src = stock_mount / mapping["stock"]
        dst = onion_mount / mapping["onion"]
        if src.is_dir():
            copy_jobs.append((src, dst, f"saves ({mapping['stock']})"))

    # Shared directories that keep the same relative path.
    shared_dirs = ["Roms", "BIOS", "Imgs"]
    for dirname in shared_dirs:
        src = stock_mount / dirname
        dst = onion_mount / dirname
        if src.is_dir():
            copy_jobs.append((src, dst, dirname))

    if not copy_jobs:
        return True, "Nothing to migrate: no recognised data found on stock SD."

    # Count total files ---------------------------------------------------
    total_files = 0
    for src, _dst, _label in copy_jobs:
        total_files += count_files(src)

    # Copy ----------------------------------------------------------------
    files_done = 0
    try:
        for src, dst, label in copy_jobs:
            copied = copy_tree_with_progress(
                src,
                dst,
                progress_callback,
                _category=label,
                _files_done=files_done,
                _total_files=total_files,
            )
            files_done += copied
    except Exception as exc:
        logger.exception("Migration failed during file copy.")
        return False, f"Migration failed: {exc}"

    return (
        True,
        f"Migration completed: {files_done} files copied.",
    )
