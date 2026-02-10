#!/bin/bash
# setup_deps.sh - Install all system dependencies for Onion Installer.
# Run with: sudo ./setup_deps.sh
#   or let main.py invoke it automatically on first run.

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Every dependency: command -> Debian package
declare -A DEPS=(
    [parted]="parted"
    [mkfs.vfat]="dosfstools"
    [fsck.vfat]="dosfstools"
    [partprobe]="parted"
    [udisksctl]="udisks2"
    [eject]="eject"
    [udevadm]="udev"
    [nmcli]="network-manager"
    [unzip]="unzip"
    [lsblk]="util-linux"
)

# Python packages (checked via import)
PYTHON_PKGS=(
    "python3"
    "python3-gi"
    "gir1.2-gtk-3.0"
)

missing_pkgs=()

echo -e "${YELLOW}Onion Installer - Dependency Check${NC}"
echo "==========================================="

# Check CLI tools
for cmd in "${!DEPS[@]}"; do
    pkg="${DEPS[$cmd]}"
    if command -v "$cmd" &>/dev/null || [ -x "/sbin/$cmd" ] || [ -x "/usr/sbin/$cmd" ]; then
        echo -e "  ${GREEN}✓${NC} $cmd ($pkg)"
    else
        echo -e "  ${RED}✗${NC} $cmd ($pkg)"
        # Only add unique packages
        if [[ ! " ${missing_pkgs[*]} " =~ " ${pkg} " ]]; then
            missing_pkgs+=("$pkg")
        fi
    fi
done

# Check Python packages
for pkg in "${PYTHON_PKGS[@]}"; do
    if dpkg -s "$pkg" &>/dev/null; then
        echo -e "  ${GREEN}✓${NC} $pkg"
    else
        echo -e "  ${RED}✗${NC} $pkg"
        missing_pkgs+=("$pkg")
    fi
done

# Check Python GTK bindings work
if python3 -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk" &>/dev/null; then
    echo -e "  ${GREEN}✓${NC} GTK3 Python bindings"
else
    echo -e "  ${RED}✗${NC} GTK3 Python bindings"
    if [[ ! " ${missing_pkgs[*]} " =~ " python3-gi " ]]; then
        missing_pkgs+=("python3-gi" "gir1.2-gtk-3.0")
    fi
fi

echo ""

if [ ${#missing_pkgs[@]} -eq 0 ]; then
    echo -e "${GREEN}All dependencies are installed!${NC}"
    exit 0
fi

echo -e "${YELLOW}Missing packages: ${missing_pkgs[*]}${NC}"
echo ""

# If running as root, install directly. Otherwise, prompt.
if [ "$EUID" -eq 0 ]; then
    echo "Installing missing packages..."
    apt-get update -qq
    apt-get install -y "${missing_pkgs[@]}"
    echo -e "${GREEN}All dependencies installed successfully!${NC}"
else
    echo "Root privileges required to install packages."
    echo "Running: sudo apt-get install ${missing_pkgs[*]}"
    echo ""
    sudo apt-get update -qq
    sudo apt-get install -y "${missing_pkgs[@]}"
    echo -e "${GREEN}All dependencies installed successfully!${NC}"
fi
