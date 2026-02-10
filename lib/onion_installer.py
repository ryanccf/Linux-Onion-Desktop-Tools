"""
onion_installer.py - Download and install Onion OS for Miyoo Mini Plus.

Replaces the Windows PowerShell scripts:
  - Onion_Install_Download.ps1
  - Onion_Install_Extract.ps1

Provides functions to fetch releases from GitHub, download release zips,
extract them to an SD card mount point, and verify the installation.
"""

import json
import logging
import os
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ONION_RELEASES_URL = (
    "https://api.github.com/repos/OnionUI/Onion/releases"
)

APP_RELEASES_URL = (
    "https://api.github.com/repos/schmurtzm/Onion-Desktop-Tools/releases"
)

NETWORK_TIMEOUT = 30  # seconds

CHUNK_SIZE = 64 * 1024  # 64 KiB per read during chunked downloads

# Directories that must be present on the SD card after a successful extraction.
EXPECTED_DIRS = [".tmp_update", "BIOS", "RetroArch", "miyoo", "Themes"]

# GitHub API requests benefit from an explicit Accept header.
_GITHUB_HEADERS = {"Accept": "application/vnd.github+json"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _github_get(url: str) -> Any:
    """Perform a GET request against the GitHub API and return parsed JSON.

    Raises
    ------
    ConnectionError
        On any network / HTTP error so callers get a single exception type
        for transport-level problems.
    """
    request = Request(url, headers=_GITHUB_HEADERS)
    try:
        with urlopen(request, timeout=NETWORK_TIMEOUT) as response:
            data = response.read()
            return json.loads(data)
    except HTTPError as exc:
        raise ConnectionError(
            f"GitHub API returned HTTP {exc.code} for {url}: {exc.reason}"
        ) from exc
    except URLError as exc:
        raise ConnectionError(
            f"Unable to reach {url}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise ConnectionError(
            f"Request to {url} timed out after {NETWORK_TIMEOUT}s"
        ) from exc


def _find_zip_asset(assets: list[dict]) -> Optional[dict]:
    """Return the first asset whose name ends with ``.zip``, or *None*."""
    for asset in assets:
        if asset.get("name", "").lower().endswith(".zip"):
            return asset
    return None


def _parse_version(tag: str) -> tuple:
    """Extract a comparable version tuple from a tag string like ``v4.3.1``."""
    match = re.search(r"(\d+(?:\.\d+)*)", tag)
    if match:
        return tuple(int(part) for part in match.group(1).split("."))
    return (0,)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_releases() -> dict[str, list[dict[str, Any]]]:
    """Query the Onion OS GitHub releases and return categorised results.

    Returns
    -------
    dict
        ``{"stable": [...], "beta": [...]}`` where each entry is a dict with
        keys: *tag_name*, *name*, *prerelease*, *published_at*,
        *browser_download_url*, *size*.

    Raises
    ------
    ConnectionError
        If the GitHub API cannot be reached or returns an error.
    ValueError
        If the response is not valid JSON or has an unexpected shape.
    """
    raw_releases: list[dict] = _github_get(ONION_RELEASES_URL)

    if not isinstance(raw_releases, list):
        raise ValueError(
            "Unexpected GitHub API response: expected a JSON array of releases"
        )

    stable: list[dict[str, Any]] = []
    beta: list[dict[str, Any]] = []

    for release in raw_releases:
        zip_asset = _find_zip_asset(release.get("assets", []))
        if zip_asset is None:
            # Skip releases that have no downloadable zip.
            continue

        entry: dict[str, Any] = {
            "tag_name": release.get("tag_name", ""),
            "name": release.get("name", ""),
            "prerelease": bool(release.get("prerelease", False)),
            "published_at": release.get("published_at", ""),
            "browser_download_url": zip_asset.get(
                "browser_download_url", ""
            ),
            "size": zip_asset.get("size", 0),
        }

        if entry["prerelease"]:
            beta.append(entry)
        else:
            stable.append(entry)

    return {"stable": stable, "beta": beta}


def download_release(
    url: str,
    dest_dir: str | Path,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> Path:
    """Download a release zip from *url* into *dest_dir*.

    Parameters
    ----------
    url:
        Direct download URL for the ``.zip`` asset (typically from
        ``browser_download_url``).
    dest_dir:
        Directory where the file will be saved.  Created if it does not exist.
    progress_callback:
        Optional callable invoked as ``progress_callback(bytes_downloaded,
        total_bytes)`` after each chunk.  *total_bytes* may be ``0`` when the
        server does not send a ``Content-Length`` header.

    Returns
    -------
    Path
        Absolute path to the downloaded file.

    Raises
    ------
    ConnectionError
        On network / HTTP errors.
    OSError
        If the destination directory cannot be created or the file cannot be
        written.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Derive filename from the URL's last path segment.
    filename = url.rsplit("/", 1)[-1] or "onion_release.zip"
    dest_path = dest_dir / filename

    request = Request(url, headers=_GITHUB_HEADERS)

    try:
        with urlopen(request, timeout=NETWORK_TIMEOUT) as response:
            total_bytes = int(response.headers.get("Content-Length", 0))
            bytes_downloaded = 0

            with open(dest_path, "wb") as fh:
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    fh.write(chunk)
                    bytes_downloaded += len(chunk)
                    if progress_callback is not None:
                        progress_callback(bytes_downloaded, total_bytes)

    except HTTPError as exc:
        raise ConnectionError(
            f"Download failed with HTTP {exc.code}: {exc.reason}"
        ) from exc
    except URLError as exc:
        raise ConnectionError(
            f"Unable to reach download URL {url}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise ConnectionError(
            f"Download from {url} timed out after {NETWORK_TIMEOUT}s"
        ) from exc

    logger.info("Downloaded %s (%d bytes)", dest_path, bytes_downloaded)
    return dest_path.resolve()


def get_downloaded_releases(
    downloads_dir: str | Path,
) -> list[dict[str, Any]]:
    """List already-downloaded Onion OS zip files in *downloads_dir*.

    Parameters
    ----------
    downloads_dir:
        Path to the directory that holds previously downloaded zips.

    Returns
    -------
    list[dict]
        Each dict has keys: *filename* (str), *size* (int, bytes),
        *modified* (str, ISO-8601 UTC timestamp).  The list is sorted by
        modification time, newest first.  Returns an empty list if the
        directory does not exist or contains no zip files.
    """
    downloads_dir = Path(downloads_dir)

    if not downloads_dir.is_dir():
        return []

    results: list[dict[str, Any]] = []
    for entry in downloads_dir.iterdir():
        if entry.is_file() and entry.suffix.lower() == ".zip":
            stat = entry.stat()
            results.append(
                {
                    "filename": entry.name,
                    "path": str(entry.resolve()),
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).isoformat(),
                }
            )

    # Most-recently modified first.
    results.sort(key=lambda r: r["modified"], reverse=True)
    return results


def get_required_space(zip_path: str | Path) -> int:
    """Return the total uncompressed size in bytes of a zip archive.

    Parameters
    ----------
    zip_path:
        Path to a ``.zip`` file on disk.

    Returns
    -------
    int
        Sum of the uncompressed sizes of every entry in the archive.

    Raises
    ------
    FileNotFoundError
        If *zip_path* does not exist.
    zipfile.BadZipFile
        If the file is not a valid zip archive.
    """
    zip_path = Path(zip_path)
    with zipfile.ZipFile(zip_path, "r") as zf:
        return sum(info.file_size for info in zf.infolist())


def extract_to_sd(
    zip_path: str | Path,
    sd_mount_point: str | Path,
    progress_callback: Optional[
        Callable[[str, int, int], None]
    ] = None,
) -> tuple[bool, str]:
    """Extract the Onion OS zip to an SD card mount point.

    Hidden files and directories (e.g. ``.tmp_update``) are preserved.

    Parameters
    ----------
    zip_path:
        Path to the downloaded ``.zip`` file.
    sd_mount_point:
        Root directory of the mounted SD card (e.g. ``/media/user/SDCARD``).
    progress_callback:
        Optional callable invoked as ``progress_callback(current_file,
        file_index, total_files)`` for every member extracted.

    Returns
    -------
    tuple[bool, str]
        ``(success, message)`` -- *success* is ``True`` when extraction
        completes without error.
    """
    zip_path = Path(zip_path)
    sd_mount_point = Path(sd_mount_point)

    if not zip_path.is_file():
        return False, f"Zip file not found: {zip_path}"

    if not sd_mount_point.is_dir():
        return False, f"SD card mount point does not exist: {sd_mount_point}"

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            members = zf.infolist()
            total_files = len(members)

            for index, member in enumerate(members):
                # --- Safety: reject paths that would escape the target dir ---
                target = (sd_mount_point / member.filename).resolve()
                if not str(target).startswith(
                    str(sd_mount_point.resolve())
                ):
                    logger.warning(
                        "Skipping potentially unsafe path: %s",
                        member.filename,
                    )
                    continue

                if progress_callback is not None:
                    progress_callback(member.filename, index, total_files)

                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(target, "wb") as dst:
                        while True:
                            chunk = src.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            dst.write(chunk)

                    # Preserve the original external attributes (permissions).
                    if member.external_attr > 0:
                        unix_mode = member.external_attr >> 16
                        if unix_mode:
                            try:
                                os.chmod(target, unix_mode)
                            except OSError:
                                pass  # Best-effort; FAT32 does not support it.

    except zipfile.BadZipFile as exc:
        return False, f"Invalid zip file: {exc}"
    except OSError as exc:
        return False, f"Extraction error: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"Unexpected error during extraction: {exc}"

    return True, "Extraction completed successfully."


def verify_extraction(
    sd_mount_point: str | Path,
) -> tuple[bool, list[str]]:
    """Check that the expected Onion OS directories exist on the SD card.

    Parameters
    ----------
    sd_mount_point:
        Root of the mounted SD card.

    Returns
    -------
    tuple[bool, list[str]]
        ``(success, missing_dirs)`` -- *success* is ``True`` when every
        expected directory is present; *missing_dirs* lists any that are
        absent.
    """
    sd_mount_point = Path(sd_mount_point)
    missing: list[str] = []

    for dirname in EXPECTED_DIRS:
        if not (sd_mount_point / dirname).is_dir():
            missing.append(dirname)

    return (len(missing) == 0, missing)


def check_for_app_updates(
    current_version: str,
) -> tuple[bool, str, str]:
    """Check whether a newer version of the Onion Desktop Tools app exists.

    Parameters
    ----------
    current_version:
        The running application's version string (e.g. ``"1.0.0"``).

    Returns
    -------
    tuple[bool, str, str]
        ``(has_update, latest_version, download_url)``.
        *has_update* is ``True`` when the remote version is strictly newer.
        If the check fails, returns ``(False, "", "")`` and logs the error
        rather than raising an exception -- an update check should never crash
        the application.
    """
    try:
        releases: list[dict] = _github_get(APP_RELEASES_URL)

        if not isinstance(releases, list) or len(releases) == 0:
            return False, "", ""

        # Use the first (most recent) non-draft release.
        latest = None
        for release in releases:
            if release.get("draft", False):
                continue
            latest = release
            break

        if latest is None:
            return False, "", ""

        latest_tag: str = latest.get("tag_name", "")
        latest_version = _parse_version(latest_tag)
        current_parsed = _parse_version(current_version)

        has_update = latest_version > current_parsed

        # Prefer a zip asset; fall back to the HTML release page.
        download_url = latest.get("html_url", "")
        zip_asset = _find_zip_asset(latest.get("assets", []))
        if zip_asset is not None:
            download_url = zip_asset.get("browser_download_url", download_url)

        return has_update, latest_tag, download_url

    except Exception as exc:  # noqa: BLE001
        logger.warning("App update check failed: %s", exc)
        return False, "", ""
