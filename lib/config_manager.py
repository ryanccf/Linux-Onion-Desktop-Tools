"""
Onion OS Configuration Manager

Manages Onion OS configuration by toggling dotfiles in .tmp_update/config/
on the SD card. Replaces the Windows PowerShell script
Onion_Config_00_settings.ps1.

Each configuration option is represented by a flag file (empty dotfile)
in the .tmp_update/config/ directory. If the file exists, the setting
is enabled; if absent, the setting is disabled.
"""

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_DIR = ".tmp_update/config"


def load_config_definitions(config_path: Path) -> dict[str, list[dict[str, Any]]]:
    """Load config.json and return the Onion_Configuration dict.

    Args:
        config_path: Path to the config.json file (typically at the
            application's root directory).

    Returns:
        The "Onion_Configuration" dictionary from config.json, mapping
        category names (e.g. "System", "Time") to lists of option dicts.

    Raises:
        FileNotFoundError: If config.json does not exist at the given path.
        KeyError: If "Onion_Configuration" key is missing from the JSON.
        json.JSONDecodeError: If the file contains invalid JSON.
    """
    config_path = Path(config_path)
    logger.debug("Loading config definitions from %s", config_path)

    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "Onion_Configuration" not in data:
        raise KeyError(
            f"'Onion_Configuration' key not found in {config_path}. "
            f"Available keys: {list(data.keys())}"
        )

    config = data["Onion_Configuration"]
    total_options = sum(len(opts) for opts in config.values())
    logger.info(
        "Loaded %d configuration categories with %d total options",
        len(config),
        total_options,
    )
    return config


def _get_all_filenames(config: dict[str, list[dict[str, Any]]]) -> list[str]:
    """Extract all filenames from the config definitions.

    Args:
        config: The Onion_Configuration dict as returned by
            load_config_definitions.

    Returns:
        A flat list of all dotfile filenames across all categories.
    """
    filenames: list[str] = []
    for category_options in config.values():
        for option in category_options:
            filenames.append(option["filename"])
    return filenames


def get_current_settings(
    sd_mount: Path,
    config: dict[str, list[dict[str, Any]]] | None = None,
    config_path: Path | None = None,
) -> dict[str, bool]:
    """Read the .tmp_update/config/ directory on the SD card and determine
    which settings are currently enabled or disabled.

    For each config option defined in config.json, the setting is considered
    enabled if the corresponding file exists in .tmp_update/config/ and
    disabled if it does not.

    Either ``config`` (a pre-loaded config dict) or ``config_path`` (path to
    config.json) must be provided so we know which filenames to check.

    Args:
        sd_mount: Mount point of the SD card (e.g. /media/user/sdcard).
        config: Pre-loaded Onion_Configuration dict. If None, config_path
            must be provided.
        config_path: Path to config.json. Used only when config is None.

    Returns:
        Dict mapping each config filename to a bool (True = enabled,
        False = disabled).

    Raises:
        ValueError: If neither config nor config_path is provided.
    """
    sd_mount = Path(sd_mount)

    if config is None:
        if config_path is None:
            raise ValueError(
                "Either 'config' or 'config_path' must be provided"
            )
        config = load_config_definitions(config_path)

    config_dir = sd_mount / CONFIG_DIR
    logger.debug("Scanning config directory: %s", config_dir)

    filenames = _get_all_filenames(config)
    settings: dict[str, bool] = {}

    for filename in filenames:
        file_path = config_dir / filename
        enabled = file_path.exists()
        settings[filename] = enabled
        logger.debug(
            "  %s: %s", filename, "enabled" if enabled else "disabled"
        )

    logger.info(
        "Read %d settings (%d enabled, %d disabled)",
        len(settings),
        sum(1 for v in settings.values() if v),
        sum(1 for v in settings.values() if not v),
    )
    return settings


def toggle_setting(sd_mount: Path, filename: str, enabled: bool) -> None:
    """Enable or disable a single configuration setting on the SD card.

    Enabling creates the empty flag file at .tmp_update/config/{filename}.
    Disabling removes the file if it exists.

    Args:
        sd_mount: Mount point of the SD card.
        filename: The dotfile name (e.g. ".noAutoStart").
        enabled: True to enable (create the file), False to disable
            (remove the file).

    Raises:
        OSError: If creating or removing the file fails due to filesystem
            errors (e.g. read-only, permission denied).
    """
    sd_mount = Path(sd_mount)
    config_dir = sd_mount / CONFIG_DIR
    file_path = config_dir / filename

    if enabled:
        # Ensure the config directory exists
        config_dir.mkdir(parents=True, exist_ok=True)
        # Create the empty flag file
        file_path.touch()
        logger.info("Enabled setting: %s (created %s)", filename, file_path)
    else:
        if file_path.exists():
            file_path.unlink()
            logger.info(
                "Disabled setting: %s (removed %s)", filename, file_path
            )
        else:
            logger.debug(
                "Setting %s already disabled (file does not exist: %s)",
                filename,
                file_path,
            )


def apply_settings(sd_mount: Path, settings_dict: dict[str, bool]) -> None:
    """Apply a full set of configuration settings at once.

    Args:
        sd_mount: Mount point of the SD card.
        settings_dict: Dict mapping each filename to a bool indicating
            whether the setting should be enabled (True) or disabled (False).

    Raises:
        OSError: If any file operation fails.
    """
    sd_mount = Path(sd_mount)
    logger.info("Applying %d settings to %s", len(settings_dict), sd_mount)

    for filename, enabled in settings_dict.items():
        toggle_setting(sd_mount, filename, enabled)

    enabled_count = sum(1 for v in settings_dict.values() if v)
    disabled_count = len(settings_dict) - enabled_count
    logger.info(
        "Applied settings: %d enabled, %d disabled",
        enabled_count,
        disabled_count,
    )
