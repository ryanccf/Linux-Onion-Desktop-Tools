"""
Onion OS WiFi Configuration Manager

Manages WiFi configuration on the SD card for Onion OS. Replaces the
Windows PowerShell scripts Onion_Config_02_wifi.ps1 and PC_WifiInfo.ps1.

Can read saved WiFi networks from the host system (via NetworkManager /
nmcli) and write WPA supplicant configuration files to the SD card.
"""

import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

WPA_SUPPLICANT_PATH = "appconfigs/wpa_supplicant.conf"

_WPA_SUPPLICANT_TEMPLATE = """\
ctrl_interface=/var/run/wpa_supplicant
update_config=1
network={{
    ssid="{ssid}"
    psk="{password}"
}}
"""


def get_host_wifi_networks() -> list[dict[str, str]]:
    """Retrieve saved WiFi connections from the host system using nmcli.

    Uses NetworkManager's CLI tool to list saved connections and then
    queries each one for its SSID and pre-shared key (PSK).

    Returns:
        List of dicts, each containing:
            - ssid (str): The network SSID.
            - password (str): The pre-shared key (may be empty if none
              is stored).

    Raises:
        FileNotFoundError: If nmcli is not installed on the system.
        subprocess.SubprocessError: If nmcli commands fail unexpectedly.
    """
    networks: list[dict[str, str]] = []

    # List all saved connections (name and UUID)
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,UUID", "connection", "show"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except FileNotFoundError:
        logger.error(
            "nmcli not found. Is NetworkManager installed?"
        )
        raise
    except subprocess.CalledProcessError as e:
        logger.error("Failed to list connections: %s", e.stderr)
        raise

    lines = result.stdout.strip().splitlines()
    if not lines:
        logger.info("No saved connections found")
        return networks

    for line in lines:
        # nmcli -t output uses ':' as separator
        # Connection names may contain colons, so split from the right
        # since UUIDs have a fixed format (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)
        parts = line.rsplit(":", 1)
        if len(parts) != 2:
            logger.debug("Skipping malformed connection line: %s", line)
            continue

        conn_name, uuid = parts[0], parts[1]
        uuid = uuid.strip()

        # Get details for this connection
        ssid, password = _get_connection_details(uuid)
        if ssid is None:
            logger.debug(
                "Skipping connection %r (UUID %s): not a WiFi connection "
                "or no SSID found",
                conn_name,
                uuid,
            )
            continue

        networks.append({"ssid": ssid, "password": password or ""})
        logger.debug("Found WiFi network: %s", ssid)

    logger.info("Found %d saved WiFi networks", len(networks))
    return networks


def _get_connection_details(uuid: str) -> tuple[str | None, str | None]:
    """Query nmcli for the SSID and PSK of a specific connection.

    Args:
        uuid: The UUID of the NetworkManager connection.

    Returns:
        Tuple of (ssid, password). Both may be None if the connection
        is not a WiFi connection or if details cannot be retrieved.
    """
    try:
        result = subprocess.run(
            ["nmcli", "-s", "connection", "show", uuid],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except subprocess.CalledProcessError as e:
        logger.debug("Failed to get details for UUID %s: %s", uuid, e.stderr)
        return None, None
    except subprocess.TimeoutExpired:
        logger.debug("Timeout getting details for UUID %s", uuid)
        return None, None

    output = result.stdout
    ssid: str | None = None
    psk: str | None = None

    for line in output.splitlines():
        line = line.strip()
        # Match 802-11-wireless.ssid
        if line.startswith("802-11-wireless.ssid:"):
            value = line.split(":", 1)[1].strip()
            if value and value != "--":
                ssid = value
        # Match 802-11-wireless-security.psk
        elif line.startswith("802-11-wireless-security.psk:"):
            value = line.split(":", 1)[1].strip()
            if value and value != "--":
                psk = value

    return ssid, psk


def write_wifi_config(
    sd_mount: Path, ssid: str, password: str
) -> tuple[bool, str]:
    """Write WPA supplicant configuration to the SD card.

    Creates the wpa_supplicant.conf file at
    {sd_mount}/appconfigs/wpa_supplicant.conf with LF line endings.

    Args:
        sd_mount: Mount point of the SD card.
        ssid: The WiFi network SSID.
        password: The WiFi network password (pre-shared key).

    Returns:
        Tuple of (success: bool, message: str).
    """
    sd_mount = Path(sd_mount)

    if not ssid:
        return False, "SSID cannot be empty"

    wpa_conf_path = sd_mount / WPA_SUPPLICANT_PATH
    content = _WPA_SUPPLICANT_TEMPLATE.format(ssid=ssid, password=password)

    try:
        # Ensure parent directory exists
        wpa_conf_path.parent.mkdir(parents=True, exist_ok=True)

        # Write with explicit LF line endings (newline="")
        # The template already uses \n which is LF
        with open(wpa_conf_path, "w", encoding="utf-8", newline="") as f:
            f.write(content)

        logger.info(
            "Wrote WiFi config for SSID %r to %s", ssid, wpa_conf_path
        )
        return True, f"WiFi configuration written for '{ssid}'"

    except PermissionError as e:
        msg = f"Permission denied writing WiFi config: {e}"
        logger.error(msg)
        return False, msg
    except OSError as e:
        msg = f"Failed to write WiFi config: {e}"
        logger.error(msg)
        return False, msg


def read_wifi_config(sd_mount: Path) -> tuple[str | None, str | None]:
    """Read existing WiFi configuration from the SD card.

    Parses the wpa_supplicant.conf file at
    {sd_mount}/appconfigs/wpa_supplicant.conf to extract the SSID and
    pre-shared key.

    Args:
        sd_mount: Mount point of the SD card.

    Returns:
        Tuple of (ssid, password). Returns (None, None) if no config
        file exists or if parsing fails.
    """
    sd_mount = Path(sd_mount)
    wpa_conf_path = sd_mount / WPA_SUPPLICANT_PATH

    if not wpa_conf_path.is_file():
        logger.debug("No WiFi config found at %s", wpa_conf_path)
        return None, None

    try:
        content = wpa_conf_path.read_text(encoding="utf-8")
    except (PermissionError, OSError) as e:
        logger.error("Failed to read WiFi config at %s: %s", wpa_conf_path, e)
        return None, None

    ssid = _extract_wpa_field(content, "ssid")
    psk = _extract_wpa_field(content, "psk")

    if ssid is not None:
        logger.info("Read WiFi config: SSID=%r", ssid)
    else:
        logger.warning(
            "WiFi config exists at %s but no SSID could be parsed",
            wpa_conf_path,
        )

    return ssid, psk


def _extract_wpa_field(content: str, field: str) -> str | None:
    """Extract a field value from wpa_supplicant.conf content.

    Handles both quoted and unquoted values. For example:
        ssid="MyNetwork"  -> "MyNetwork"
        psk="secret123"   -> "secret123"

    Args:
        content: The full text content of the wpa_supplicant.conf file.
        field: The field name to extract (e.g. "ssid", "psk").

    Returns:
        The field value as a string, or None if not found.
    """
    # Match field="value" or field=value (with optional surrounding whitespace)
    pattern = rf'^\s*{re.escape(field)}\s*=\s*"([^"]*)"'
    match = re.search(pattern, content, re.MULTILINE)
    if match:
        return match.group(1)

    # Try unquoted value
    pattern = rf"^\s*{re.escape(field)}\s*=\s*(\S+)"
    match = re.search(pattern, content, re.MULTILINE)
    if match:
        return match.group(1)

    return None
