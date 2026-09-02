#!/bin/sh
set -eu
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

source_dir="$(CDPATH= cd "$(dirname "$0")" && pwd)"
release="$source_dir/ylvaos-release"
overlay="$source_dir/rootfs-overlay.tar.gz"

if [ "$(id -u)" -ne 0 ]; then
    echo "YlvaOS update.sh must run as root."
    exit 1
fi

if [ ! -f "$release" ] || [ ! -f "$overlay" ]; then
    echo "YlvaOS update payload is incomplete."
    exit 1
fi

. "$release"
backup="/etc/ylvaos-release.before-update"
cp /etc/ylvaos-release "$backup" 2>/dev/null || true
tar -xzf "$overlay" -C /
fc-cache -f >/tmp/ylva-update-font-cache.log 2>&1 || true
sync
echo "YlvaOS update applied."
echo "Alpine Linux ${ALPINE_VERSION:-unknown} base / YlvaOS ${YLVAOS_VERSION:-unknown}"
echo "Rebooting YlvaOS..."
reboot
