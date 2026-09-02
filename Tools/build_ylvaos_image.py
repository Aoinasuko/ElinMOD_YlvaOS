#!/usr/bin/env python3
"""
Build the bundled YlvaOS Linux boot assets and preinstalled qcow2 root disk.

This script intentionally uses the local QEMU bundled in Mod_YlvaOS/Tools/qemu
and the official Alpine Linux virtual ISO. The generated VM is network-disabled
at runtime by the MOD backend, but this build step uses QEMU user networking to
install packages into the offline root disk.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path


ALPINE_VERSION = "3.24.1"
ALPINE_BRANCH = "v3.24"
ALPINE_ARCH = "x86_64"
ALPINE_ISO = f"alpine-virt-{ALPINE_VERSION}-{ALPINE_ARCH}.iso"
ALPINE_RELEASE_BASE = "https://dl-cdn.alpinelinux.org/alpine/latest-stable/releases/x86_64"
ALPINE_REPO_MAIN = f"https://dl-cdn.alpinelinux.org/alpine/{ALPINE_BRANCH}/main"
ALPINE_REPO_COMMUNITY = f"https://dl-cdn.alpinelinux.org/alpine/{ALPINE_BRANCH}/community"
ALPINE_ISO_URL = f"{ALPINE_RELEASE_BASE}/{ALPINE_ISO}"
ALPINE_ISO_SHA256 = "e73a6241bd5f3c5c2d4d38c02cc52c378c0415a7c888bd292066bf36e0f41a39"
YLVAOS_VERSION = "0.01"

DEFAULT_DISK_MIB = 16384
INSTALL_TIMEOUT_SECONDS = 1800


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return

    temporary = destination.with_suffix(destination.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()

    print(f"Downloading {url}")
    with urllib.request.urlopen(url) as response, temporary.open("wb") as output:
        shutil.copyfileobj(response, output)
    temporary.replace(destination)


def run_checked(args: list[str], cwd: Path | None = None) -> None:
    print(" ".join(args))
    subprocess.run(args, cwd=str(cwd) if cwd else None, check=True)


def ensure_alpine_iso(root: Path) -> Path:
    iso = root / "_work" / "downloads" / ALPINE_ISO
    download(ALPINE_ISO_URL, iso)
    actual = sha256_file(iso)
    if actual != ALPINE_ISO_SHA256:
        raise RuntimeError(f"SHA-256 mismatch for {iso}: expected {ALPINE_ISO_SHA256}, got {actual}")
    return iso


def extract_boot_assets(root: Path, iso: Path) -> tuple[Path, Path]:
    extract_dir = root / "_work" / "alpine-iso"
    assets_dir = root / "Mod_YlvaOS" / "vm" / "assets"
    extract_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)

    run_checked(
        [
            "tar.exe",
            "-xf",
            str(iso),
            "-C",
            str(extract_dir),
            "boot/vmlinuz-virt",
            "boot/initramfs-virt",
        ]
    )

    kernel = assets_dir / "vmlinuz"
    initrd = assets_dir / "initrd.img"
    shutil.copyfile(extract_dir / "boot" / "vmlinuz-virt", kernel)
    shutil.copyfile(extract_dir / "boot" / "initramfs-virt", initrd)
    print(f"Wrote {kernel}")
    print(f"Wrote {initrd}")
    return kernel, initrd


class QemuConsole:
    def __init__(self, args: list[str], cwd: Path):
        self.args = args
        self.cwd = cwd
        self.lock = threading.Lock()
        self.text = ""
        self.process: subprocess.Popen[bytes] | None = None
        self.reader: threading.Thread | None = None

    def start(self) -> None:
        self.process = subprocess.Popen(
            self.args,
            cwd=str(self.cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()

    def _read_loop(self) -> None:
        assert self.process is not None
        assert self.process.stdout is not None
        while True:
            data = self.process.stdout.read(256)
            if not data:
                break
            chunk = data.decode("utf-8", errors="replace")
            sys.stdout.write(chunk)
            sys.stdout.flush()
            with self.lock:
                self.text += chunk
                if len(self.text) > 2_000_000:
                    self.text = self.text[-1_000_000:]

    def send(self, text: str) -> None:
        assert self.process is not None
        assert self.process.stdin is not None
        self.process.stdin.write(text.encode("utf-8"))
        self.process.stdin.flush()

    def send_slow(self, text: str, line_delay: float = 0.35) -> None:
        assert self.process is not None
        assert self.process.stdin is not None
        for line in text.splitlines(keepends=True):
            self.process.stdin.write(line.encode("utf-8"))
            self.process.stdin.flush()
            time.sleep(line_delay)

    def send_chars(self, text: str, char_delay: float = 0.01) -> None:
        assert self.process is not None
        assert self.process.stdin is not None
        for char in text:
            self.process.stdin.write(char.encode("utf-8"))
            self.process.stdin.flush()
            time.sleep(char_delay)

    def wait_for_new(self, needle: str, start_index: int, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if self.text.find(needle, start_index) >= 0:
                    return
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError(f"QEMU exited before seeing new {needle!r}")
            time.sleep(0.2)
        raise TimeoutError(f"Timed out waiting for new {needle!r}")

    def length(self) -> int:
        with self.lock:
            return len(self.text)

    def run_interactive_command(self, command: str, prompt: str, timeout: int = 60) -> None:
        start_index = self.length()
        time.sleep(0.8)
        self.send_chars(command + "\n", char_delay=0.003)
        self.wait_for_new(prompt, start_index, timeout)

    def wait_for(self, needle: str, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if needle in self.text:
                    return
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError(f"QEMU exited before seeing {needle!r}")
            time.sleep(0.2)
        raise TimeoutError(f"Timed out waiting for {needle!r}")

    def snapshot(self) -> str:
        with self.lock:
            return self.text

    def terminate(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)


def install_script() -> str:
    packages = " ".join(
        [
            "alpine-base",
            "e2fsprogs",
            "util-linux",
            "util-linux-login",
            "vim",
            "doas",
            "less",
            "shadow",
            "ca-certificates",
            "musl-locales",
            "musl-locales-lang",
            "dbus",
            "dbus-x11",
            "eudev",
            "dialog",
            "alsa-lib",
            "alsa-utils",
            "alsa-plugins",
            "alsa-plugins-pulse",
            "pulseaudio",
            "pulseaudio-alsa",
            "pulseaudio-utils",
            "fluidsynth",
            "soundfont-timgm",
            "wine",
            "xorg-server",
            "xinit",
            "xterm",
            "openbox",
            "tint2",
            "pcmanfm",
            "mc",
            "xdotool",
            "xdg-utils",
            "xrandr",
            "xsetroot",
            "font-dejavu",
            "font-noto-cjk",
            "xf86-input-libinput",
            "xf86-video-fbdev",
            "xf86-video-vesa",
            "mesa-dri-gallium",
        ]
    )

    script = """set -eux
trap 'rc=$?; if [ "$rc" -ne 0 ]; then echo __YLVA_INSTALL_FAILED__:$rc; fi' EXIT

export PATH=/sbin:/bin:/usr/sbin:/usr/bin
echo __YLVA_INSTALL_BEGIN__

ip link set eth0 up || ifconfig eth0 up || true
udhcpc -i eth0 -n -q -t 10 || true

apk add e2fsprogs || apk add --repository {ALPINE_REPO_MAIN} e2fsprogs
mkfs.ext4 -F -L YLVAOS /dev/vda
mkdir -p /mnt
mount -t ext4 /dev/vda /mnt

mkdir -p /mnt/etc/apk
cat >/tmp/ylva-repositories <<'EOF'
{ALPINE_REPO_MAIN}
{ALPINE_REPO_COMMUNITY}
EOF
cp /tmp/ylva-repositories /mnt/etc/apk/repositories

apk --root /mnt --initdb --update-cache --keys-dir /etc/apk/keys --repositories-file /tmp/ylva-repositories add {packages}

kernel_release="$(uname -r)"
if [ -d "/lib/modules/$kernel_release" ]; then
    mkdir -p /mnt/lib/modules
    rm -rf "/mnt/lib/modules/$kernel_release"
    cp -a "/lib/modules/$kernel_release" /mnt/lib/modules/
    chroot /mnt /sbin/depmod "$kernel_release" || true
fi

printf 'YlvaOS\\n' >/mnt/etc/hostname
cat >/mnt/etc/ylvaos-release <<'EOF'
YLVAOS_VERSION="{YLVAOS_VERSION}"
ALPINE_VERSION="{ALPINE_VERSION}"
EOF

cat >/mnt/etc/os-release <<'EOF'
NAME="YlvaOS"
ID=ylvaos
ID_LIKE=alpine
VERSION_ID="{ALPINE_VERSION}"
PRETTY_NAME="Alpine Linux {ALPINE_VERSION} base / YlvaOS {YLVAOS_VERSION}"
HOME_URL="https://ylva.local/"
SUPPORT_URL="https://alpinelinux.org/"
EOF

cat >/mnt/etc/issue <<'EOF'
Alpine Linux {ALPINE_VERSION} base / YlvaOS {YLVAOS_VERSION} \\n \\l

EOF

cat >/mnt/etc/motd <<'EOF'
Welcome to YlvaOS.

This is a real Alpine Linux based userspace running inside the Elin MOD QEMU sandbox.
Runtime networking is disabled by default from the MOD side.
EOF

mkdir -p /mnt/etc/profile.d
cat >/mnt/etc/profile.d/ylvaos-locale.sh <<'EOF'
export MUSL_LOCPATH=/usr/share/i18n/locales/musl
export LANG=ja_JP.UTF-8
export LC_CTYPE=ja_JP.UTF-8
export LC_MESSAGES=C.UTF-8
EOF

cat >/mnt/etc/fstab <<'EOF'
/dev/vda / ext4 rw,noatime 0 1
proc /proc proc defaults 0 0
sysfs /sys sysfs defaults 0 0
devpts /dev/pts devpts mode=0620,gid=5 0 0
tmpfs /run tmpfs defaults,nosuid,nodev 0 0
tmpfs /tmp tmpfs defaults,nosuid,nodev 0 0
EOF

mkdir -p /mnt/etc/network
cat >/mnt/etc/network/interfaces <<'EOF'
auto lo
iface lo inet loopback
EOF

mkdir -p /mnt/home/ylva /mnt/etc/doas.d /mnt/etc/profile.d
sed -i 's/^root:[^:]*:/root::/' /mnt/etc/shadow
if grep -q '^wheel:' /mnt/etc/group; then
    sed -i 's/^wheel:.*/wheel:x:10:root/' /mnt/etc/group
else
    echo 'wheel:x:10:root' >>/mnt/etc/group
fi
echo 'permit nopass :wheel' >/mnt/etc/doas.d/doas.conf
chmod 0600 /mnt/etc/doas.d/doas.conf

cat >/mnt/home/ylva/README.txt <<'EOF'
YlvaOS quick notes
==================

- vim is preinstalled.
- Wine, PulseAudio, ALSA tools, FluidSynth, and a GM soundfont are preinstalled for lightweight desktop apps.
- The root disk lives under LocalLow/Lafrontier/Elin/YlvaOS/vm/disk.qcow2 after the MOD provisions it.
- Put host files in LocalLow/Lafrontier/Elin/YlvaOS/Import to expose them as a read-only guest drive.
- QEMU runtime networking is disabled by default in the MOD backend.
- Run ConnectNetwork and type yes to enable QEMU user-mode networking for this VM session.
- After connecting, use doas apk update and doas apk add <package> to install packages.
- Run YlvaOS update after installing a newer MOD package to update YlvaOS-managed OS files from the bundled update drive.
- Use poweroff to shut down the VM.
EOF
chown 1000:1000 /mnt/home/ylva/README.txt

cat >/mnt/sbin/ylva-getty <<'EOF'
#!/bin/sh
set -u
export PATH=/sbin:/bin:/usr/sbin:/usr/bin

get_arg() {
    name="$1"
    for arg in $(cat /proc/cmdline); do
        case "$arg" in
            "$name="*) printf '%s' "$(printf '%s' "$arg" | sed "s/^$name=//")"; return ;;
        esac
    done
}

user="$(get_arg ylva_user)"
case "$user" in ''|*[!a-z0-9_-]* ) user=ylva ;; esac
first="$(printf '%.1s' "$user")"
case "$first" in [a-z]|_) ;; *) user=ylva ;; esac

password_b64="$(get_arg ylva_password_b64)"
password=""
if [ -n "$password_b64" ]; then
    password="$(printf '%s' "$password_b64" | base64 -d 2>/dev/null || true)"
fi

rows="$(get_arg ylva_rows)"
cols="$(get_arg ylva_cols)"
case "$rows" in ''|*[!0-9]* ) rows=32 ;; esac
case "$cols" in ''|*[!0-9]* ) cols=140 ;; esac

resize2fs /dev/vda >/dev/null 2>&1 || true
for _ in 1 2 3 4 5; do
    found_port=0
    for port in /dev/virtio-ports/org.ylvaos.control /dev/virtio-ports/org.ylvaos.audio /dev/virtio-ports/org.ylvaos.hostinput; do
        if [ -e "$port" ]; then
            chmod 0666 "$port" >/dev/null 2>&1 || true
            found_port=1
        fi
    done
    if [ "$found_port" -eq 1 ]; then
        break
    fi
    sleep 1
done

if ! id "$user" >/dev/null 2>&1; then
    adduser -D -h "/home/$user" -s /bin/ash "$user" >/dev/null 2>&1 || user=ylva
fi

if ! id "$user" >/dev/null 2>&1; then
    adduser -D -h /home/ylva -s /bin/ash ylva >/dev/null 2>&1 || true
    user=ylva
fi

addgroup "$user" wheel >/dev/null 2>&1 || true
mkdir -p "/home/$user"
chown "$user:$user" "/home/$user" >/dev/null 2>&1 || true

if [ -n "$password" ]; then
    printf '%s:%s\n' "$user" "$password" | chpasswd >/dev/null 2>&1 || true
else
    passwd -d "$user" >/dev/null 2>&1 || true
fi

cat >"/home/$user/.profile" <<EOF_PROFILE
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
export MUSL_LOCPATH=/usr/share/i18n/locales/musl
export LANG=ja_JP.UTF-8
export LC_CTYPE=ja_JP.UTF-8
export LC_MESSAGES=C.UTF-8
export TERM=vt100
stty rows $rows cols $cols -ixon 2>/dev/null || true
export XDG_RUNTIME_DIR="/tmp/ylva-runtime-$user"
mkdir -p "/tmp/ylva-runtime-$user" "/tmp/ylva-runtime-$user/pulse" 2>/dev/null || true
chmod 700 "/tmp/ylva-runtime-$user" 2>/dev/null || true
export PULSE_SERVER="unix:/tmp/ylva-runtime-$user/pulse/native"
ylva-start-audio >/tmp/ylva-audio.log 2>&1 || true
if command -v ylva-host-agent >/dev/null 2>&1 && ! pgrep -u "\$(id -u)" -f '[y]lva-host-agent' >/dev/null 2>&1; then
    ylva-host-agent >/tmp/ylva-host-agent.log 2>&1 &
fi
if [ "\${YLVA_SPLASH_SHOWN:-0}" != 1 ]; then
    export YLVA_SPLASH_SHOWN=1
    ylva-splash 2>/dev/null || true
fi
export PS1='YlvaOS:\w\$ '
alias poweroff='doas poweroff'
alias reboot='doas reboot'
alias shutdown='doas poweroff'
EOF_PROFILE
chown "$user:$user" "/home/$user/.profile" >/dev/null 2>&1 || true

exec /sbin/agetty --autologin "$user" -L 115200 ttyS0 vt100
EOF
chmod 0755 /mnt/sbin/ylva-getty

cat >/mnt/etc/profile.d/ylvaos-terminal.sh <<'EOF'
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
export MUSL_LOCPATH=/usr/share/i18n/locales/musl
export LANG=ja_JP.UTF-8
export LC_CTYPE=ja_JP.UTF-8
export LC_MESSAGES=C.UTF-8
export TERM=vt100
stty rows 32 cols 140 -ixon 2>/dev/null || true
EOF

cat >/mnt/etc/profile.d/ylvaos-audio.sh <<'EOF'
if [ -z "${XDG_RUNTIME_DIR:-}" ]; then
    ylva_audio_user="$(id -un 2>/dev/null || printf ylva)"
    export XDG_RUNTIME_DIR="/tmp/ylva-runtime-$ylva_audio_user"
fi
mkdir -p "$XDG_RUNTIME_DIR" "$XDG_RUNTIME_DIR/pulse" 2>/dev/null || true
chmod 700 "$XDG_RUNTIME_DIR" 2>/dev/null || true
export PULSE_SERVER="${PULSE_SERVER:-unix:$XDG_RUNTIME_DIR/pulse/native}"
export ALSA_CONFIG_PATH=/etc/asound.conf
export PULSE_PROP="media.role=game"
EOF

cat >/mnt/usr/bin/ylva-control <<'EOF'
#!/bin/sh
set -u

get_arg() {
    name="$1"
    for arg in $(cat /proc/cmdline); do
        case "$arg" in
            "$name="*) printf '%s' "$(printf '%s' "$arg" | sed "s/^$name=//")"; return ;;
        esac
    done
}

message="$*"
token="$(get_arg ylva_control_token)"
port=/dev/virtio-ports/org.ylvaos.control

if [ -n "$message" ] && [ -n "$token" ] && [ -e "$port" ]; then
    if [ ! -w "$port" ] && [ "$(id -u)" -ne 0 ]; then
        doas chmod 0666 "$port" >/dev/null 2>&1 || true
    fi

    if printf 'YLVAOS %s %s\n' "$token" "$message" >"$port" 2>/dev/null; then
        exit 0
    fi
fi

if [ -n "$message" ]; then
    printf '\033]777;ylvaos;%s\a' "$message" >/dev/ttyS0 2>/dev/null || true
fi
EOF
chmod 0755 /mnt/usr/bin/ylva-control

cat >/mnt/usr/bin/ylva-splash <<'EOF'
#!/bin/sh
set -u

version="{YLVAOS_VERSION}"
base="Alpine Linux {ALPINE_VERSION} base / YlvaOS $version"
if [ -t 1 ]; then
    cyan="$(printf '\\033[96m')"
    reset="$(printf '\\033[0m')"
else
    cyan=""
    reset=""
fi

printf '%s-----------------------------\\n' "$cyan"
printf ' ^           Ylva OS\\n'
printf '(  * *)   by aoi_nasuko\\n'
printf -- '-----------------------------\\n'
printf '%s%s\\n' "$base" "$reset"
EOF
chmod 0755 /mnt/usr/bin/ylva-splash

cat >/mnt/usr/bin/ylva-host-agent <<'EOF'
#!/bin/sh
set -u
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
export MUSL_LOCPATH="${MUSL_LOCPATH:-/usr/share/i18n/locales/musl}"
export LANG="${LANG:-ja_JP.UTF-8}"
export LC_CTYPE="${LC_CTYPE:-ja_JP.UTF-8}"
export LC_MESSAGES="${LC_MESSAGES:-C.UTF-8}"

get_arg() {
    name="$1"
    for arg in $(cat /proc/cmdline); do
        case "$arg" in
            "$name="*) printf '%s' "$(printf '%s' "$arg" | sed "s/^$name=//")"; return ;;
        esac
    done
}

paste_base64() {
    payload="$1"
    tmp="${TMPDIR:-/tmp}/ylva-paste.$$"
    rm -f "$tmp"
    if ! printf '%s' "$payload" | base64 -d >"$tmp" 2>/dev/null; then
        rm -f "$tmp"
        return 1
    fi

    if [ -S /tmp/.X11-unix/X0 ] && command -v xdotool >/dev/null 2>&1; then
        display="${DISPLAY:-:0}"
        xauthority="${XAUTHORITY:-$HOME/.Xauthority}"
        DISPLAY="$display" XAUTHORITY="$xauthority" xdotool type --clearmodifiers --delay 1 --file "$tmp" >/dev/null 2>&1 ||
            DISPLAY=:0 xdotool type --clearmodifiers --delay 1 --file "$tmp" >/dev/null 2>&1 ||
            true
    fi

    rm -f "$tmp"
}

handle_line() {
    line="$1"
    prefix="YLVAOS_HOST $token "
    case "$line" in
        "${prefix}"*) body="${line#"$prefix"}" ;;
        *) return ;;
    esac

    case "$body" in
        paste-b64\ *)
            paste_base64 "${body#paste-b64 }"
            ;;
    esac
}

token="$(get_arg ylva_control_token)"
port=/dev/virtio-ports/org.ylvaos.hostinput
ready_sent=0

while :; do
    if [ -z "$token" ] || [ ! -e "$port" ]; then
        ready_sent=0
        sleep 1
        continue
    fi

    if [ ! -r "$port" ] && [ "$(id -u)" -ne 0 ]; then
        doas chmod 0666 "$port" >/dev/null 2>&1 || true
    fi

    if [ "$ready_sent" != 1 ]; then
        printf 'YLVAOS_HOST %s ready\n' "$token" >"$port" 2>/dev/null && ready_sent=1 || true
    fi

    if IFS= read -r line <"$port"; then
        handle_line "$line"
    else
        ready_sent=0
        sleep 1
    fi
done
EOF
chmod 0755 /mnt/usr/bin/ylva-host-agent

cat >/mnt/usr/bin/ConnectNetwork <<'EOF'
#!/bin/sh
set -u
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        doas "$@"
    fi
}

find_iface() {
    for path in /sys/class/net/*; do
        name="${path##*/}"
        if [ "$name" != lo ]; then
            printf '%s\n' "$name"
            return 0
        fi
    done

    return 1
}

cat <<'WARN'
WARNING: ConnectNetwork enables Internet access for the YlvaOS guest through QEMU user-mode networking.

Guest programs may contact remote servers, download and run code, expose information typed inside YlvaOS, and increase the attack surface of this MOD sandbox.
Host filesystem access is still limited to the YlvaOS virtual disk and the read-only Import drive, but malicious guest software can damage or exfiltrate data inside the VM.
Only continue if you understand Linux networking and trust the commands you will run.

Type "yes" to enable networking:
WARN
printf '> '
IFS= read -r answer || answer=
if [ "$answer" != yes ]; then
    echo "ConnectNetwork cancelled."
    exit 1
fi

echo "Requesting a network adapter from the Elin MOD host..."
if ! ylva-control network connect; then
    echo "Failed to request networking from the MOD host."
    exit 1
fi

as_root modprobe virtio_net >/dev/null 2>&1 || true

iface=
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
    iface="$(find_iface 2>/dev/null || true)"
    if [ -n "$iface" ]; then
        break
    fi
    sleep 1
done

if [ -z "$iface" ]; then
    echo "No YlvaOS network interface appeared."
    exit 1
fi

echo "Configuring $iface with DHCP..."
as_root ip link set "$iface" up >/dev/null 2>&1 || as_root ifconfig "$iface" up >/dev/null 2>&1 || true
if as_root udhcpc -i "$iface" -n -q -t 10; then
    echo "YlvaOS networking is connected through QEMU user-mode NAT."
    echo "You can now run commands such as: doas apk update"
    exit 0
fi

echo "DHCP failed on $iface."
exit 1
EOF
chmod 0755 /mnt/usr/bin/ConnectNetwork
ln -sf /usr/bin/ConnectNetwork /mnt/usr/bin/connectnetwork

cat >/mnt/etc/asound.conf <<'EOF'
pcm.!default {
    type pulse
}

ctl.!default {
    type pulse
}
EOF

mkdir -p /mnt/etc/fonts
cat >/mnt/etc/fonts/local.conf <<'EOF'
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
<fontconfig>
  <alias>
    <family>MS Gothic</family>
    <prefer><family>Noto Sans CJK JP</family></prefer>
  </alias>
  <alias>
    <family>MS PGothic</family>
    <prefer><family>Noto Sans CJK JP</family></prefer>
  </alias>
  <alias>
    <family>MS UI Gothic</family>
    <prefer><family>Noto Sans CJK JP</family></prefer>
  </alias>
  <alias>
    <family>MS Mincho</family>
    <prefer><family>Noto Serif CJK JP</family><family>Noto Sans CJK JP</family></prefer>
  </alias>
  <alias>
    <family>Meiryo</family>
    <prefer><family>Noto Sans CJK JP</family></prefer>
  </alias>
</fontconfig>
EOF
chroot /mnt /usr/bin/fc-cache -f >/dev/null 2>&1 || true

cat >/mnt/usr/bin/ylva-audio-bridge <<'EOF'
#!/bin/sh
set -u
export PATH=/sbin:/bin:/usr/sbin:/usr/bin

user="$(id -un 2>/dev/null || printf ylva)"
runtime="${XDG_RUNTIME_DIR:-/tmp/ylva-runtime-$user}"
port=/dev/virtio-ports/org.ylvaos.audio
export XDG_RUNTIME_DIR="$runtime"
export PULSE_SERVER="${PULSE_SERVER:-unix:$runtime/pulse/native}"
export ALSA_CONFIG_PATH="${ALSA_CONFIG_PATH:-/etc/asound.conf}"

mkdir -p "$runtime" "$runtime/pulse" 2>/dev/null || true
chmod 700 "$runtime" 2>/dev/null || true
while :; do
    if [ ! -e "$port" ]; then
        sleep 1
        continue
    fi

    chmod 0666 "$port" >/dev/null 2>&1 || true
    if ! pactl info >/dev/null 2>&1; then
        sleep 1
        continue
    fi

    if ! pactl list short sources 2>/dev/null | awk '{print $2}' | grep -qx ylva.monitor; then
        sleep 1
        continue
    fi

    parec --device=ylva.monitor --format=s16le --rate=44100 --channels=2 --latency-msec=120 >"$port" 2>/tmp/ylva-audio-bridge.log || sleep 1
done
EOF
chmod 0755 /mnt/usr/bin/ylva-audio-bridge

cat >/mnt/usr/bin/ylva-start-audio <<'EOF'
#!/bin/sh
set -u
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

user="$(id -un 2>/dev/null || printf ylva)"
runtime="${XDG_RUNTIME_DIR:-/tmp/ylva-runtime-$user}"
export XDG_RUNTIME_DIR="$runtime"
export PULSE_SERVER="unix:$runtime/pulse/native"
export ALSA_CONFIG_PATH=/etc/asound.conf

mkdir -p "$runtime" "$runtime/pulse"
chmod 700 "$runtime" 2>/dev/null || true

if ! pulseaudio --check >/dev/null 2>&1; then
    pulseaudio --daemonize=yes --exit-idle-time=-1 --log-target=file:/tmp/ylva-pulseaudio.log >/dev/null 2>&1 || true
fi

for _ in 1 2 3 4 5 6 7 8 9 10; do
    if pactl info >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

for _ in 1 2 3; do
    if pactl list short sinks 2>/dev/null | awk '{print $2}' | grep -qx ylva; then
        break
    fi
    pactl load-module module-null-sink sink_name=ylva format=s16le rate=44100 channels=2 sink_properties=device.description=YlvaOS >/tmp/ylva-null-sink.id 2>/tmp/ylva-null-sink.log || true
    sleep 1
done

pactl set-default-sink ylva >/dev/null 2>&1 || true

if command -v ylva-audio-bridge >/dev/null 2>&1; then
    if pgrep -f '/usr/bin/ylva-audio-bridge' >/dev/null 2>&1; then
        if [ "$(id -u)" -eq 0 ]; then
            pkill -f '/usr/bin/ylva-audio-bridge' >/dev/null 2>&1 || true
        else
            doas pkill -f '/usr/bin/ylva-audio-bridge' >/dev/null 2>&1 || pkill -f '/usr/bin/ylva-audio-bridge' >/dev/null 2>&1 || true
        fi
        sleep 1
    fi
    ylva-audio-bridge >/tmp/ylva-audio-bridge.log 2>&1 &
fi

soundfont=/usr/share/sounds/sf2/TimGM6mb.sf2
if [ ! -f "$soundfont" ]; then
    soundfont="$(find /usr/share -iname '*.sf2' 2>/dev/null | head -n 1)"
fi

if [ -n "$soundfont" ] && [ -f "$soundfont" ] && ! pgrep -u "$(id -u)" fluidsynth >/dev/null 2>&1; then
    modprobe snd-seq >/dev/null 2>&1 || true
    modprobe snd-seq-midi >/dev/null 2>&1 || true
    modprobe snd-rawmidi >/dev/null 2>&1 || true
    fluidsynth -i -a pulseaudio -m alsa_seq -o audio.period-size=1024 -o audio.periods=3 "$soundfont" >/tmp/ylva-fluidsynth.log 2>&1 &
fi
EOF
chmod 0755 /mnt/usr/bin/ylva-start-audio

mkdir -p /mnt/etc/local.d
cat >/mnt/etc/local.d/ylva-audio.start <<'EOF'
#!/bin/sh
# The audio bridge is started from the logged-in user session by ylva-start-audio,
# so it can use that user's PulseAudio runtime directory.
exit 0
EOF
chmod 0755 /mnt/etc/local.d/ylva-audio.start

mkdir -p /mnt/usr/lib/ylvaos
cat >/mnt/usr/lib/ylvaos/wine-env <<'EOF'
export MUSL_LOCPATH=/usr/share/i18n/locales/musl
export LANG=ja_JP.UTF-8
export LC_CTYPE=ja_JP.UTF-8
export LC_MESSAGES=C.UTF-8
export WINEPREFIX="${WINEPREFIX:-$HOME/.wine}"
export WINEDLLOVERRIDES="${WINEDLLOVERRIDES:-mscoree,mshtml=}"
export PULSE_LATENCY_MSEC="${PULSE_LATENCY_MSEC:-120}"
if [ -z "${XDG_RUNTIME_DIR:-}" ]; then
    ylva_audio_user="$(id -un 2>/dev/null || printf ylva)"
    export XDG_RUNTIME_DIR="/tmp/ylva-runtime-$ylva_audio_user"
fi
mkdir -p "$XDG_RUNTIME_DIR" "$XDG_RUNTIME_DIR/pulse" 2>/dev/null || true
chmod 700 "$XDG_RUNTIME_DIR" 2>/dev/null || true
export PULSE_SERVER="${PULSE_SERVER:-unix:$XDG_RUNTIME_DIR/pulse/native}"
export ALSA_CONFIG_PATH="${ALSA_CONFIG_PATH:-/etc/asound.conf}"
EOF

cat >/mnt/usr/lib/ylvaos/setup-audio <<'EOF'
#!/bin/sh
set -u
. /usr/lib/ylvaos/wine-env

ylva-start-audio >/tmp/ylva-audio.log 2>&1 || true
if pactl list short sinks 2>/dev/null | awk '{print $2}' | grep -qx ylva; then
    echo "YlvaOS audio sink is ready."
    exit 0
fi

echo "YlvaOS audio sink did not become ready. See /tmp/ylva-audio.log and /tmp/ylva-null-sink.log."
exit 1
EOF
chmod 0755 /mnt/usr/lib/ylvaos/setup-audio

cat >/mnt/usr/lib/ylvaos/setup-font <<'EOF'
#!/bin/sh
set -u
. /usr/lib/ylvaos/wine-env

fc-cache -f >/tmp/ylva-font-cache.log 2>&1 || true
wine reg add 'HKLM\\Software\\Microsoft\\Windows NT\\CurrentVersion\\FontSubstitutes' /v 'MS Gothic' /d 'Noto Sans CJK JP' /f >/dev/null 2>&1 || true
wine reg add 'HKLM\\Software\\Microsoft\\Windows NT\\CurrentVersion\\FontSubstitutes' /v 'MS PGothic' /d 'Noto Sans CJK JP' /f >/dev/null 2>&1 || true
wine reg add 'HKLM\\Software\\Microsoft\\Windows NT\\CurrentVersion\\FontSubstitutes' /v 'MS UI Gothic' /d 'Noto Sans CJK JP' /f >/dev/null 2>&1 || true
wine reg add 'HKLM\\Software\\Microsoft\\Windows NT\\CurrentVersion\\FontSubstitutes' /v 'MS Mincho' /d 'Noto Serif CJK JP' /f >/dev/null 2>&1 || true
wine reg add 'HKCU\\Software\\Wine\\Fonts\\Replacements' /v 'MS Gothic' /d 'Noto Sans CJK JP' /f >/dev/null 2>&1 || true
wine reg add 'HKCU\\Software\\Wine\\Fonts\\Replacements' /v 'MS PGothic' /d 'Noto Sans CJK JP' /f >/dev/null 2>&1 || true
wine reg add 'HKCU\\Software\\Wine\\Fonts\\Replacements' /v 'MS UI Gothic' /d 'Noto Sans CJK JP' /f >/dev/null 2>&1 || true
wine reg add 'HKCU\\Software\\Wine\\Fonts\\Replacements' /v 'MS Mincho' /d 'Noto Serif CJK JP' /f >/dev/null 2>&1 || true
echo "YlvaOS fonts are ready."
EOF
chmod 0755 /mnt/usr/lib/ylvaos/setup-font

cat >/mnt/usr/lib/ylvaos/setup-wine <<'EOF'
#!/bin/sh
set -u
. /usr/lib/ylvaos/wine-env

marker="$WINEPREFIX/.ylvaos-jp-wine-v4"
if [ -f "$marker" ]; then
    echo "Wine prefix is ready at $WINEPREFIX."
    exit 0
fi

mkdir -p "$WINEPREFIX"
/usr/lib/ylvaos/setup-audio >/tmp/ylva-audio.log 2>&1 || true
wineboot -u >/tmp/ylva-wineboot.log 2>&1 || true

wine reg add 'HKCU\\Software\\Wine\\Drivers' /v Audio /d pulse /f >/dev/null 2>&1 || true
wine reg add 'HKCU\\Control Panel\\International' /v LocaleName /d ja-JP /f >/dev/null 2>&1 || true
wine reg add 'HKCU\\Control Panel\\International' /v sCountry /d Japan /f >/dev/null 2>&1 || true
wine reg add 'HKCU\\Control Panel\\International' /v sLanguage /d JPN /f >/dev/null 2>&1 || true
wine reg add 'HKLM\\System\\CurrentControlSet\\Control\\Nls\\CodePage' /v ACP /d 932 /f >/dev/null 2>&1 || true
wine reg add 'HKLM\\System\\CurrentControlSet\\Control\\Nls\\CodePage' /v OEMCP /d 932 /f >/dev/null 2>&1 || true
wine reg add 'HKLM\\System\\CurrentControlSet\\Control\\Nls\\CodePage' /v MACCP /d 10001 /f >/dev/null 2>&1 || true
wine reg add 'HKLM\\System\\CurrentControlSet\\Control\\Nls\\Language' /v Default /d 0411 /f >/dev/null 2>&1 || true
wine reg add 'HKLM\\System\\CurrentControlSet\\Control\\Nls\\Language' /v InstallLanguage /d 0411 /f >/dev/null 2>&1 || true
/usr/lib/ylvaos/setup-font >/tmp/ylva-font-setup.log 2>&1 || true

touch "$marker"
echo "Wine prefix is ready at $WINEPREFIX."
EOF
chmod 0755 /mnt/usr/lib/ylvaos/setup-wine

cat >/mnt/usr/lib/ylvaos/update-from-mod <<'EOF'
#!/bin/sh
set -u
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        doas "$@"
    fi
}

read_release_value() {
    file="$1"
    key="$2"
    [ -f "$file" ] || return 0
    sed -n "s/^${key}=//p" "$file" | sed 's/^"//;s/"$//' | head -n 1
}

version_part() {
    part="$(printf '%s' "$1" | sed 's/[^0-9].*$//;s/^0*//')"
    if [ -z "$part" ]; then
        part=0
    fi
    printf '%s' "$part"
}

version_gt() {
    old="$1"
    new="$2"
    while :; do
        old_part="$old"
        new_part="$new"
        case "$old" in *.*) old_part="${old%%.*}"; old="${old#*.}" ;; *) old="" ;; esac
        case "$new" in *.*) new_part="${new%%.*}"; new="${new#*.}" ;; *) new="" ;; esac
        old_num="$(version_part "$old_part")"
        new_num="$(version_part "$new_part")"
        if [ "$new_num" -gt "$old_num" ]; then
            return 0
        fi
        if [ "$new_num" -lt "$old_num" ]; then
            return 1
        fi
        [ -n "$old" ] || [ -n "$new" ] || return 1
    done
}

find_update_source() {
    mount_dir="$1"
    mkdir -p "$mount_dir"
    if mountpoint -q "$mount_dir" 2>/dev/null; then
        if [ -f "$mount_dir/YLVAOS_UPDATE_SOURCE" ]; then
            return 0
        fi
        as_root umount "$mount_dir" >/dev/null 2>&1 || true
    fi

    for dev in /dev/vdc1 /dev/vdc /dev/vdd1 /dev/vdd /dev/vdb1 /dev/vdb /dev/sdb1 /dev/sdb /dev/sda1 /dev/sda; do
        [ -b "$dev" ] || continue
        if as_root mount -t vfat -o ro,utf8=1 "$dev" "$mount_dir" >/tmp/ylva-update-mount.log 2>&1; then
            if [ -f "$mount_dir/YLVAOS_UPDATE_SOURCE" ]; then
                return 0
            fi
            as_root umount "$mount_dir" >/dev/null 2>&1 || true
        fi
    done

    return 1
}

mount_dir=/run/ylvaos-update-source
current_version="$(read_release_value /etc/ylvaos-release YLVAOS_VERSION)"
current_version="${current_version:-0.00}"

if ! find_update_source "$mount_dir"; then
    echo "No YlvaOS MOD update source was found."
    echo "Install a newer MOD package that contains vm/update, then start YlvaOS again."
    exit 1
fi

target_version="$(read_release_value "$mount_dir/ylvaos-release" YLVAOS_VERSION)"
target_alpine="$(read_release_value "$mount_dir/ylvaos-release" ALPINE_VERSION)"
if [ -z "$target_version" ]; then
    echo "The YlvaOS MOD update source is missing ylvaos-release."
    exit 1
fi

echo "Installed: YlvaOS $current_version"
echo "Bundled:   Alpine Linux ${target_alpine:-unknown} base / YlvaOS $target_version"

if ! version_gt "$current_version" "$target_version"; then
    echo "YlvaOS is already up to date."
    exit 0
fi

if [ ! -f "$mount_dir/rootfs-overlay.tar.gz" ] || [ ! -f "$mount_dir/update.sh" ]; then
    echo "The YlvaOS MOD update source is incomplete."
    exit 1
fi

echo "Updating YlvaOS-managed OS files from the MOD package..."
if [ "$(id -u)" -eq 0 ]; then
    sh "$mount_dir/update.sh"
else
    doas sh "$mount_dir/update.sh"
fi
EOF
chmod 0755 /mnt/usr/lib/ylvaos/update-from-mod

cat >/mnt/usr/bin/Terminal <<'EOF'
#!/bin/sh
set -u
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
export DISPLAY="${DISPLAY:-:0}"

title="${1:-YlvaOS Terminal}"
xterm -title "$title" -geometry 100x28+36+56 &

(sleep 1
win="$(xdotool search --name "$title" 2>/dev/null | tail -n 1)"
if [ -n "$win" ]; then
    xdotool windowactivate "$win" windowfocus "$win" >/dev/null 2>&1 || true
fi) >/dev/null 2>&1 &
EOF
chmod 0755 /mnt/usr/bin/Terminal
ln -sf /usr/bin/Terminal /mnt/usr/bin/terminal

cat >/mnt/usr/bin/Files <<'EOF'
#!/bin/sh
set -u
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        doas "$@"
    fi
}

ensure_user_dirs() {
    mkdir -p "$HOME/Desktop" "$HOME/Documents" "$HOME/Downloads" "$HOME/Pictures" "$HOME/Import"
}

ensure_import_mount() {
    ensure_user_dirs
    if mountpoint -q "$HOME/Import" 2>/dev/null; then
        return 0
    fi

    for dev in /dev/vdb1 /dev/vdb /dev/vdc1 /dev/vdc /dev/sda1 /dev/sda /dev/sdb1 /dev/sdb; do
        [ -b "$dev" ] || continue
        as_root mount -t vfat -o ro,uid="$(id -u)",gid="$(id -g)",utf8=1 "$dev" "$HOME/Import" >/tmp/ylva-import-mount.log 2>&1 && return 0
    done

    return 1
}

start_dbus_session() {
    if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && command -v dbus-launch >/dev/null 2>&1; then
        eval "$(dbus-launch --sh-syntax 2>/dev/null)" || true
    fi
}

ensure_user_dirs
ensure_import_mount >/dev/null 2>&1 || true

target="${1:-$HOME}"
case "$target" in
    home|Home) target="$HOME" ;;
    import|Import) target="$HOME/Import" ;;
    desktop|Desktop) target="$HOME/Desktop" ;;
    documents|Documents) target="$HOME/Documents" ;;
    downloads|Downloads) target="$HOME/Downloads" ;;
    pictures|Pictures) target="$HOME/Pictures" ;;
esac

if [ ! -e "$target" ]; then
    target="$HOME"
fi

if [ -n "${DISPLAY:-}" ] && command -v pcmanfm >/dev/null 2>&1; then
    (
        start_dbus_session
        pcmanfm --no-desktop "$target"
    ) >/tmp/ylva-files.log 2>&1 &
    exit 0
fi

if [ -n "${DISPLAY:-}" ] && command -v xterm >/dev/null 2>&1 && command -v mc >/dev/null 2>&1; then
    xterm -title "YlvaOS Files" -geometry 100x30+72+72 -e mc "$target" &
    exit 0
fi

if command -v mc >/dev/null 2>&1; then
    exec mc "$target"
fi

echo "No file manager is installed."
exit 1
EOF
chmod 0755 /mnt/usr/bin/Files
ln -sf /usr/bin/Files /mnt/usr/bin/files

cat >/mnt/usr/lib/ylvaos/settings-tui <<'EOF'
#!/bin/sh
set -u
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

is_positive_int() {
    case "$1" in
        ''|*[!0-9]* ) return 1 ;;
    esac

    [ "$1" -gt 0 ] 2>/dev/null
}

pause_screen() {
    printf '\nPress Enter to continue... '
    IFS= read -r _ || true
}

read_value() {
    prompt="$1"
    printf '%s: ' "$prompt" >&2
    IFS= read -r value || value=
    printf '%s' "$value"
}

show_status() {
    echo "YlvaOS Settings"
    echo "==============="
    echo
    printf 'User: %s\n' "$(id -un 2>/dev/null || printf ylva)"
    printf 'Kernel: %s\n' "$(uname -r)"
    awk '/^MemTotal:/ { printf "Guest memory: %.0f MiB\n", $2 / 1024 }' /proc/meminfo 2>/dev/null || true
    df -h / 2>/dev/null | awk 'NR==2 { printf "Root disk: %s used / %s total (%s)\n", $3, $2, $5 }' || true
    if ip route 2>/dev/null | grep -q '^default '; then
        echo "Network: connected for this VM session"
    else
        echo "Network: disconnected"
    fi
    if pactl info >/dev/null 2>&1; then
        echo "Audio: PulseAudio ready"
    else
        echo "Audio: not ready"
    fi
}

set_memory() {
    value="$(read_value 'Memory target MiB')"
    if ! is_positive_int "$value"; then
        echo "memory must be a positive MiB value"
        pause_screen
        return
    fi

    YlvaOS set memory "$value"
    pause_screen
}

set_disk() {
    value="$(read_value 'Disk target MiB')"
    if ! is_positive_int "$value"; then
        echo "disk must be a positive MiB value"
        pause_screen
        return
    fi

    YlvaOS set disk "$value"
    pause_screen
}

set_resolution() {
    width="$(read_value 'Desktop width')"
    height="$(read_value 'Desktop height')"
    if ! is_positive_int "$width" || ! is_positive_int "$height"; then
        echo "width and height must be positive integer values"
        pause_screen
        return
    fi

    YlvaOS set desktop "$width" "$height"
    pause_screen
}

set_fps() {
    value="$(read_value 'Desktop refresh FPS')"
    if ! is_positive_int "$value"; then
        echo "fps must be a positive integer value"
        pause_screen
        return
    fi

    YlvaOS set fps "$value"
    pause_screen
}

while :; do
    clear 2>/dev/null || true
    show_status
    cat <<'MENU'

1) Set memory target
2) Set disk target
3) Set desktop resolution
4) Set desktop refresh FPS
5) Setup audio
6) Connect network
7) Open file manager
8) Quit

MENU
    printf 'Select: '
    IFS= read -r choice || exit 0
    case "$choice" in
        1) set_memory ;;
        2) set_disk ;;
        3) set_resolution ;;
        4) set_fps ;;
        5) YlvaOS setup audio; pause_screen ;;
        6) ConnectNetwork; pause_screen ;;
        7) Files; pause_screen ;;
        8|q|Q) exit 0 ;;
        *) echo "Unknown selection."; pause_screen ;;
    esac
done
EOF
chmod 0755 /mnt/usr/lib/ylvaos/settings-tui

cat >/mnt/usr/bin/Settings <<'EOF'
#!/bin/sh
set -u
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

if [ -n "${DISPLAY:-}" ] && command -v xterm >/dev/null 2>&1 && [ "${YLVA_SETTINGS_INLINE:-0}" != 1 ]; then
    YLVA_SETTINGS_INLINE=1 xterm -title "YlvaOS Settings" -geometry 88x30+96+72 -e /usr/lib/ylvaos/settings-tui &
    exit 0
fi

exec /usr/lib/ylvaos/settings-tui
EOF
chmod 0755 /mnt/usr/bin/Settings
ln -sf /usr/bin/Settings /mnt/usr/bin/settings

cat >/mnt/usr/local/bin/ylva-desktop-session <<'EOF'
#!/bin/sh
set -u
export PATH=/sbin:/bin:/usr/sbin:/usr/bin
export SHELL=/bin/ash

get_arg() {
    name="$1"
    for arg in $(cat /proc/cmdline); do
        case "$arg" in
            "$name="*) printf '%s' "$(printf '%s' "$arg" | sed "s/^$name=//")"; return ;;
        esac
    done
}

width="$(get_arg ylva_desktop_width)"
height="$(get_arg ylva_desktop_height)"
case "$width" in ''|*[!0-9]* ) width=1024 ;; esac
case "$height" in ''|*[!0-9]* ) height=768 ;; esac

user="${USER:-ylva}"
home="${HOME:-/home/$user}"
export HOME="$home"
export XDG_RUNTIME_DIR="/tmp/ylva-runtime-$user"
mkdir -p "$XDG_RUNTIME_DIR" "$XDG_RUNTIME_DIR/pulse" "$home/.config/openbox" "$home/.config/tint2"
chmod 700 "$XDG_RUNTIME_DIR" 2>/dev/null || true
export PULSE_SERVER="unix:$XDG_RUNTIME_DIR/pulse/native"
ylva-start-audio >/tmp/ylva-audio.log 2>&1 || true

cat >"$home/.Xresources" <<'EOF_XRES'
XTerm*faceName: DejaVu Sans Mono
XTerm*faceSize: 11
XTerm*background: #07110f
XTerm*foreground: #d4f8dc
XTerm*cursorColor: #d4f8dc
XTerm*scrollBar: false
XTerm*utf8: true
XTerm*locale: true
EOF_XRES
xrdb "$home/.Xresources" 2>/dev/null || true

cat >"$home/.config/openbox/rc.xml" <<'EOF_OBRC'
<?xml version="1.0" encoding="UTF-8"?>
<openbox_config xmlns="http://openbox.org/3.4/rc">
  <focus>
    <focusNew>yes</focusNew>
    <followMouse>yes</followMouse>
    <focusDelay>0</focusDelay>
    <raiseOnFocus>no</raiseOnFocus>
    <focusLast>yes</focusLast>
    <underMouse>yes</underMouse>
  </focus>
  <keyboard>
    <keybind key="C-A-t">
      <action name="Execute">
        <command>Terminal</command>
      </action>
    </keybind>
    <keybind key="C-A-k">
      <action name="Execute">
        <command>Kernel</command>
      </action>
    </keybind>
  </keyboard>
  <mouse>
    <context name="Frame">
      <mousebind button="Left" action="Press">
        <action name="Focus"/>
        <action name="Raise"/>
      </mousebind>
    </context>
    <context name="Client">
      <mousebind button="Left" action="Press">
        <action name="Focus"/>
        <action name="Raise"/>
      </mousebind>
    </context>
    <context name="Titlebar">
      <mousebind button="Left" action="Press">
        <action name="Focus"/>
        <action name="Raise"/>
        <action name="Unshade"/>
      </mousebind>
      <mousebind button="Left" action="Drag">
        <action name="Move"/>
      </mousebind>
      <mousebind button="Left" action="DoubleClick">
        <action name="ToggleMaximize"/>
      </mousebind>
      <mousebind button="Middle" action="Press">
        <action name="Lower"/>
      </mousebind>
      <mousebind button="Right" action="Press">
        <action name="ShowMenu">
          <menu>client-menu</menu>
        </action>
      </mousebind>
    </context>
    <context name="Top Right Bottom Left TLCorner TRCorner BRCorner BLCorner">
      <mousebind button="Left" action="Press">
        <action name="Focus"/>
        <action name="Raise"/>
        <action name="Unshade"/>
      </mousebind>
      <mousebind button="Left" action="Drag">
        <action name="Resize"/>
      </mousebind>
    </context>
    <context name="Iconify">
      <mousebind button="Left" action="Click">
        <action name="Iconify"/>
      </mousebind>
    </context>
    <context name="Maximize">
      <mousebind button="Left" action="Click">
        <action name="ToggleMaximize"/>
      </mousebind>
      <mousebind button="Middle" action="Click">
        <action name="ToggleMaximize">
          <direction>vertical</direction>
        </action>
      </mousebind>
      <mousebind button="Right" action="Click">
        <action name="ToggleMaximize">
          <direction>horizontal</direction>
        </action>
      </mousebind>
    </context>
    <context name="Close">
      <mousebind button="Left" action="Click">
        <action name="Close"/>
      </mousebind>
    </context>
    <context name="Root">
      <mousebind button="Right" action="Press">
        <action name="ShowMenu">
          <menu>root-menu</menu>
        </action>
      </mousebind>
    </context>
  </mouse>
  <menu>
    <file>menu.xml</file>
  </menu>
</openbox_config>
EOF_OBRC

cat >"$home/.config/openbox/menu.xml" <<'EOF_OBMENU'
<?xml version="1.0" encoding="UTF-8"?>
<openbox_menu xmlns="http://openbox.org/3.4/menu">
  <menu id="root-menu" label="YlvaOS">
    <item label="Settings">
      <action name="Execute">
        <command>Settings</command>
      </action>
    </item>
    <item label="File Manager">
      <action name="Execute">
        <command>Files</command>
      </action>
    </item>
    <item label="Terminal">
      <action name="Execute">
        <command>Terminal</command>
      </action>
    </item>
    <separator />
    <item label="Return to Kernel">
      <action name="Execute">
        <command>Kernel</command>
      </action>
    </item>
  </menu>
</openbox_menu>
EOF_OBMENU

cat >"$home/.config/openbox/autostart" <<'EOF_AUTOSTART'
xsetroot -solid '#12211f' &
tint2 &
Terminal &
EOF_AUTOSTART
chmod 0755 "$home/.config/openbox/autostart" 2>/dev/null || true

xrandr -s "${width}x${height}" 2>/dev/null || true
xsetroot -solid '#12211f' 2>/dev/null || true
if command -v dbus-launch >/dev/null 2>&1; then
    exec dbus-launch --exit-with-session openbox-session
fi

exec openbox-session
EOF
chmod 0755 /mnt/usr/local/bin/ylva-desktop-session

cat >/mnt/usr/sbin/ylva-start-desktop <<'EOF'
#!/bin/sh
set -u
export PATH=/sbin:/bin:/usr/sbin:/usr/bin

target_user="${1:-ylva}"
case "$target_user" in ''|*[!a-z0-9_-]* ) target_user=ylva ;; esac

mkdir -p /tmp/.X11-unix /run/dbus /run/udev
chmod 1777 /tmp /tmp/.X11-unix 2>/dev/null || true
if [ -e /dev/virtio-ports/org.ylvaos.control ]; then
    chmod 0666 /dev/virtio-ports/org.ylvaos.control >/dev/null 2>&1 || true
fi
if [ -e /dev/virtio-ports/org.ylvaos.audio ]; then
    chmod 0666 /dev/virtio-ports/org.ylvaos.audio >/dev/null 2>&1 || true
fi
rc-service dbus start >/dev/null 2>&1 || dbus-daemon --system --fork >/dev/null 2>&1 || true
modprobe evdev >/dev/null 2>&1 || true
modprobe usbhid >/dev/null 2>&1 || true
modprobe psmouse >/dev/null 2>&1 || true
if command -v udevadm >/dev/null 2>&1; then
    rc-service udev start >/dev/null 2>&1 || udevd --daemon >/dev/null 2>&1 || /lib/udev/udevd --daemon >/dev/null 2>&1 || true
    udevadm trigger --action=add >/dev/null 2>&1 || true
    udevadm settle >/dev/null 2>&1 || true
fi

if pgrep -f 'Xorg .*:0' >/dev/null 2>&1 || [ -S /tmp/.X11-unix/X0 ]; then
    ylva-control mode desktop
    exit 0
fi

if command -v openvt >/dev/null 2>&1; then
    openvt -f -c 7 -- su - "$target_user" -c 'startx /usr/local/bin/ylva-desktop-session -- :0 -nolisten tcp' >/tmp/ylva-desktop.log 2>&1 &
else
    su - "$target_user" -c 'startx /usr/local/bin/ylva-desktop-session -- :0 -nolisten tcp' >/tmp/ylva-desktop.log 2>&1 &
fi

for _ in 1 2 3 4 5 6 7 8 9 10; do
    if [ -S /tmp/.X11-unix/X0 ]; then
        ylva-control mode desktop
        exit 0
    fi
    sleep 1
done

ylva-control mode desktop
EOF
chmod 0755 /mnt/usr/sbin/ylva-start-desktop

cat >/mnt/usr/sbin/ylva-stop-desktop <<'EOF'
#!/bin/sh
set -u
export PATH=/sbin:/bin:/usr/sbin:/usr/bin

pkill -TERM -f 'ylva-desktop-session' >/dev/null 2>&1 || true
pkill -TERM -f 'openbox-session' >/dev/null 2>&1 || true
pkill -TERM -x tint2 >/dev/null 2>&1 || true
pkill -TERM -x xterm >/dev/null 2>&1 || true
sleep 1
pkill -TERM -f 'Xorg .*:0' >/dev/null 2>&1 || true
exit 0
EOF
chmod 0755 /mnt/usr/sbin/ylva-stop-desktop

cat >/mnt/usr/bin/Desktop <<'EOF'
#!/bin/sh
ylva-control mode desktop-starting
user="${USER:-ylva}"
if [ "$(id -u)" -eq 0 ]; then
    /usr/sbin/ylva-start-desktop "$user" &
else
    doas /usr/sbin/ylva-start-desktop "$user" &
fi
echo "Starting YlvaOS Desktop. Use Kernel from the desktop terminal to return."
EOF
chmod 0755 /mnt/usr/bin/Desktop
ln -sf /usr/bin/Desktop /mnt/usr/bin/desktop

cat >/mnt/usr/bin/Kernel <<'EOF'
#!/bin/sh
ylva-control mode kernel
if [ "$(id -u)" -eq 0 ]; then
    /usr/sbin/ylva-stop-desktop
else
    doas /usr/sbin/ylva-stop-desktop
fi
echo "Returned to YlvaOS kernel console."
EOF
chmod 0755 /mnt/usr/bin/Kernel
ln -sf /usr/bin/Kernel /mnt/usr/bin/kernel

mkdir -p /mnt/etc/X11
cat >/mnt/etc/X11/Xwrapper.config <<'EOF'
allowed_users=anybody
needs_root_rights=yes
EOF

mkdir -p /mnt/etc/X11/xorg.conf.d
cat >/mnt/etc/X11/xorg.conf.d/10-ylvaos-input.conf <<'EOF'
Section "InputClass"
    Identifier "YlvaOS keyboard"
    MatchIsKeyboard "on"
    Driver "libinput"
    Option "XkbLayout" "us"
EndSection

Section "InputClass"
    Identifier "YlvaOS pointer"
    MatchIsPointer "on"
    Driver "libinput"
EndSection
EOF

cat >/mnt/usr/bin/YlvaOS <<'EOF'
#!/bin/sh

emit_host() {
    ylva-control "$@"
}

usage() {
    echo "usage: YlvaOS status | YlvaOS update | YlvaOS settings | YlvaOS files [path]"
    echo "       YlvaOS set memory <MiB> | YlvaOS set disk <MiB>"
    echo "       YlvaOS set desktop <WxH>|<width> <height> | YlvaOS set fps <FPS>"
    echo "       YlvaOS setup wine|font|audio"
}

is_positive_int() {
    case "$1" in
        ''|*[!0-9]* ) return 1 ;;
    esac

    [ "$1" -gt 0 ] 2>/dev/null
}

case "${1:-}" in
    help|'')
        usage
        ;;
    status)
        if [ -f /etc/ylvaos-release ]; then
            . /etc/ylvaos-release
            echo "Alpine Linux ${ALPINE_VERSION:-unknown} base / YlvaOS ${YLVAOS_VERSION:-unknown}"
        else
            echo "YlvaOS $(uname -r)"
        fi
        grep '^MemTotal:' /proc/meminfo
        df -h /
        ;;
    update)
        /usr/lib/ylvaos/update-from-mod
        ;;
    settings)
        Settings
        ;;
    files|file-manager|filemanager)
        if [ -n "${2:-}" ]; then
            Files "$2"
        else
            Files
        fi
        ;;
    set)
        case "${2:-}" in
            memory|mem)
                if ! is_positive_int "${3:-}"; then
                    echo "memory must be a positive MiB value"
                    exit 2
                fi
                ylva-control "set memory $3"
                echo "YlvaOS memory target set to $3 MiB. Reboot YlvaOS to apply."
                ;;
            disk)
                if ! is_positive_int "${3:-}"; then
                    echo "disk must be a positive MiB value"
                    exit 2
                fi
                ylva-control "set disk $3"
                echo "YlvaOS disk target set to $3 MiB. Reboot YlvaOS to apply."
                ;;
            desktop|resolution)
                if [ -n "${4:-}" ]; then
                    width="${3:-}"
                    height="${4:-}"
                else
                    case "${3:-}" in
                        *[xX]* ) ;;
                        *) echo "desktop size must be formatted like 1024x768 or 1024 768"; exit 2 ;;
                    esac
                    width="$(printf '%s' "${3:-}" | sed 's/[xX].*$//')"
                    height="$(printf '%s' "${3:-}" | sed 's/^.*[xX]//')"
                fi
                if ! is_positive_int "$width" || ! is_positive_int "$height"; then
                    echo "desktop size must be formatted like 1024x768 or 1024 768"
                    exit 2
                fi
                ylva-control "set desktop $width $height"
                echo "YlvaOS desktop target set to ${width}x${height}. Reboot YlvaOS to apply."
                ;;
            fps|framerate)
                if ! is_positive_int "${3:-}"; then
                    echo "fps must be a positive integer"
                    exit 2
                fi
                ylva-control "set fps $3"
                echo "YlvaOS desktop refresh target set to $3 fps. Reboot YlvaOS to apply."
                ;;
            *)
                usage
                exit 2
                ;;
        esac
        ;;
    setup)
        case "${2:-}" in
            audio)
                /usr/lib/ylvaos/setup-audio
                ;;
            font)
                /usr/lib/ylvaos/setup-font
                ;;
            wine)
                /usr/lib/ylvaos/setup-wine
                ;;
            *)
                usage
                exit 2
                ;;
        esac
        ;;
    *)
        usage
        exit 2
        ;;
esac
EOF
chmod 0755 /mnt/usr/bin/YlvaOS

if [ -f /mnt/etc/securetty ] && ! grep -q '^ttyS0$' /mnt/etc/securetty; then
    echo ttyS0 >>/mnt/etc/securetty
fi

sed -i '/ttyS0::respawn:/d' /mnt/etc/inittab
echo 'ttyS0::respawn:/sbin/ylva-getty' >>/mnt/etc/inittab

for spec in \\
    'devfs sysinit' \\
    'dmesg sysinit' \\
    'udev sysinit' \\
    'udev-trigger sysinit' \\
    'udev-settle sysinit' \\
    'hwdrivers sysinit' \\
    'modules boot' \\
    'sysctl boot' \\
    'hostname boot' \\
    'bootmisc boot' \\
    'syslog boot' \\
    'local default' \\
    'killprocs shutdown' \\
    'mount-ro shutdown' \\
    'savecache shutdown'
do
    set -- $spec
    if [ -x "/mnt/etc/init.d/$1" ]; then
        chroot /mnt /sbin/rc-update add "$1" "$2" || true
    fi
done

cat >/mnt/usr/lib/ylvaos/managed-files <<'EOF'
etc/apk/repositories
etc/asound.conf
etc/fonts/local.conf
etc/hostname
etc/issue
etc/local.d/ylva-audio.start
etc/motd
etc/os-release
etc/profile.d/ylvaos-audio.sh
etc/profile.d/ylvaos-locale.sh
etc/profile.d/ylvaos-terminal.sh
etc/X11/Xwrapper.config
etc/X11/xorg.conf.d/10-ylvaos-input.conf
etc/ylvaos-release
sbin/ylva-getty
usr/bin/ConnectNetwork
usr/bin/connectnetwork
usr/bin/Desktop
usr/bin/desktop
usr/bin/Files
usr/bin/files
usr/bin/Kernel
usr/bin/kernel
usr/bin/Settings
usr/bin/settings
usr/bin/Terminal
usr/bin/terminal
usr/bin/YlvaOS
usr/bin/ylva-audio-bridge
usr/bin/ylva-control
usr/bin/ylva-host-agent
usr/bin/ylva-splash
usr/bin/ylva-start-audio
usr/lib/ylvaos/managed-files
usr/lib/ylvaos/settings-tui
usr/lib/ylvaos/setup-audio
usr/lib/ylvaos/setup-font
usr/lib/ylvaos/setup-wine
usr/lib/ylvaos/update-from-mod
usr/lib/ylvaos/wine-env
usr/local/bin/ylva-desktop-session
usr/sbin/ylva-start-desktop
usr/sbin/ylva-stop-desktop
EOF

tar -czf /tmp/ylvaos-rootfs-overlay.tar.gz -C /mnt $(cat /mnt/usr/lib/ylvaos/managed-files)

cat >/tmp/ylvaos-update.sh <<'EOF'
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
EOF

export_update_payload() {
    export_mount=/tmp/ylvaos-update-export
    mkdir -p "$export_mount"
    for dev in /dev/vdc /dev/vdc1 /dev/vdd /dev/vdd1 /dev/sdb /dev/sdb1 /dev/sdc /dev/sdc1; do
        [ -b "$dev" ] || continue
        if mount -t vfat -o rw "$dev" "$export_mount" >/tmp/ylva-update-export.log 2>&1; then
            if [ -f "$export_mount/YLVA_UPDATE_EXPORT_DRIVE" ]; then
                rm -f "$export_mount/rootfs-overlay.tar.gz" \
                    "$export_mount/ylvaos-release" \
                    "$export_mount/update.sh" \
                    "$export_mount/YLVAOS_UPDATE_SOURCE"
                cp /tmp/ylvaos-rootfs-overlay.tar.gz "$export_mount/rootfs-overlay.tar.gz"
                cp /mnt/etc/ylvaos-release "$export_mount/ylvaos-release"
                cp /tmp/ylvaos-update.sh "$export_mount/update.sh"
                printf 'YlvaOS MOD update source\n' >"$export_mount/YLVAOS_UPDATE_SOURCE"
                sync
                umount "$export_mount"
                echo __YLVA_UPDATE_EXPORT_DONE__
                return 0
            fi
            umount "$export_mount" >/dev/null 2>&1 || true
        fi
    done

    echo __YLVA_UPDATE_EXPORT_FAILED__
    return 1
}

export_update_payload

mkdir -p /mnt/root
apk --root /mnt info -vv >/mnt/root/YLVAOS_PACKAGES.txt || true
echo __YLVA_PACKAGES_BEGIN__
cat /mnt/root/YLVAOS_PACKAGES.txt || true
echo __YLVA_PACKAGES_END__

sync
umount /mnt
echo __YLVA_INSTALL_DONE__
poweroff -f
"""

    return (
        script.replace("{ALPINE_REPO_MAIN}", ALPINE_REPO_MAIN)
        .replace("{ALPINE_REPO_COMMUNITY}", ALPINE_REPO_COMMUNITY)
        .replace("{ALPINE_VERSION}", ALPINE_VERSION)
        .replace("{YLVAOS_VERSION}", YLVAOS_VERSION)
        .replace("{packages}", packages)
    )


def prepare_install_seed(build_dir: Path) -> Path:
    seed_dir = build_dir / "seed"
    if seed_dir.exists():
        shutil.rmtree(seed_dir)
    seed_dir.mkdir(parents=True)
    with (seed_dir / "install.sh").open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(install_script().replace("\r\n", "\n"))
    return seed_dir


def prepare_update_export(build_dir: Path) -> Path:
    export_dir = build_dir / "update-export"
    if export_dir.exists():
        shutil.rmtree(export_dir)
    export_dir.mkdir(parents=True)
    (export_dir / "YLVA_UPDATE_EXPORT_DRIVE").write_text("YlvaOS update export drive\n", encoding="utf-8")
    return export_dir


def publish_update_payload(root: Path, export_dir: Path) -> None:
    update_dir = root / "Mod_YlvaOS" / "vm" / "update"
    update_dir.mkdir(parents=True, exist_ok=True)
    for child in update_dir.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()

    required = [
        "YLVAOS_UPDATE_SOURCE",
        "ylvaos-release",
        "update.sh",
        "rootfs-overlay.tar.gz",
    ]
    for name in required:
        source = export_dir / name
        if not source.exists():
            raise RuntimeError(f"YlvaOS update payload was not exported: missing {source}")
        shutil.copyfile(source, update_dir / name)
    print(f"Wrote {update_dir}")


def mount_install_seed(console: QemuConsole, prompt: str) -> None:
    command = (
        "mkdir -p /tmp/ylvaos-seed; "
        "mounted=; "
        "for dev in /dev/vdb /dev/vdb1 /dev/vdc /dev/vdc1 /dev/sda /dev/sda1 /dev/sdb /dev/sdb1; do "
        "[ -b \"$dev\" ] || continue; "
        "mount -t vfat -o ro \"$dev\" /tmp/ylvaos-seed >/dev/null 2>&1 && mounted=1 && break; "
        "done; "
        "if [ -z \"$mounted\" ]; then "
        "echo __YLVA_SEED_MOUNT_FAILED__; ls -l /dev/vd* /dev/sd* 2>/dev/null || true; "
        "else test -f /tmp/ylvaos-seed/install.sh && echo __YLVA_SEED_READY__; fi"
    )
    console.run_interactive_command(command, prompt, timeout=45)
    snapshot = console.snapshot()
    if "__YLVA_SEED_READY__" not in snapshot:
        raise RuntimeError("YlvaOS install seed drive was not mounted; see console output above.")


def create_preinstalled_disk(root: Path, iso: Path, disk_mib: int, force: bool) -> Path:
    qemu_dir = root / "Mod_YlvaOS" / "Tools" / "qemu"
    qemu_system = qemu_dir / "qemu-system-x86_64.exe"
    qemu_img = qemu_dir / "qemu-img.exe"
    build_dir = root / "_work" / "ylvaos-image"
    disk = build_dir / "disk.qcow2"

    if not qemu_system.exists():
        raise RuntimeError(f"Missing {qemu_system}")
    if not qemu_img.exists():
        raise RuntimeError(f"Missing {qemu_img}")

    build_dir.mkdir(parents=True, exist_ok=True)
    if disk.exists():
        if not force:
            print(f"Reusing existing {disk}; pass --force to rebuild it.")
            return disk
        disk.unlink()

    seed_dir = prepare_install_seed(build_dir)
    update_export_dir = prepare_update_export(build_dir)
    run_checked([str(qemu_img), "create", "-f", "qcow2", str(disk), f"{disk_mib}M"], cwd=build_dir)

    qemu_args = [
        str(qemu_system),
        "-m",
        "1024",
        "-machine",
        "accel=tcg",
        "-cpu",
        "max",
        "-smp",
        "2",
        "-display",
        "none",
        "-serial",
        "stdio",
        "-monitor",
        "none",
        "-no-reboot",
        "-boot",
        "d",
        "-cdrom",
        str(iso),
        "-drive",
        f"file={disk},if=virtio,format=qcow2",
        "-drive",
        f"file=fat:ro:{seed_dir.as_posix()},if=virtio,format=raw,media=disk,readonly=on",
        "-drive",
        f"file=fat:rw:{update_export_dir.as_posix()},if=virtio,format=raw,media=disk",
        "-netdev",
        "user,id=n0",
        "-device",
        "virtio-net-pci,netdev=n0",
    ]

    console = QemuConsole(qemu_args, build_dir)
    console.start()
    try:
        console.wait_for("localhost login:", 120)
        console.send("root\n")
        console.wait_for("#", 60)
        time.sleep(1.5)
        console.send_chars("export TERM=dumb; PS1='__YLVA_PROMPT__# '\n", char_delay=0.02)
        console.wait_for("__YLVA_PROMPT__#", 30)
        mount_install_seed(console, "__YLVA_PROMPT__#")
        console.send_chars("sh /tmp/ylvaos-seed/install.sh\n", char_delay=0.003)
        console.wait_for("__YLVA_INSTALL_DONE__", INSTALL_TIMEOUT_SECONDS)
        if console.process is not None:
            console.process.wait(timeout=120)
    finally:
        console.terminate()

    log = console.snapshot()
    if "__YLVA_INSTALL_FAILED__:" in log and "__YLVA_INSTALL_DONE__" not in log:
        raise RuntimeError("YlvaOS root disk install failed; see console output above.")
    if "__YLVA_UPDATE_EXPORT_DONE__" not in log:
        raise RuntimeError("YlvaOS update payload export failed; see console output above.")

    legal_dir = root / "Mod_YlvaOS" / "LEGAL"
    legal_dir.mkdir(parents=True, exist_ok=True)
    packages = extract_between(log, "__YLVA_PACKAGES_BEGIN__", "__YLVA_PACKAGES_END__")
    if packages:
        (legal_dir / "alpine-installed-packages.txt").write_text(packages.strip() + "\n", encoding="utf-8")

    publish_update_payload(root, update_export_dir)
    return disk


def extract_between(text: str, start: str, end: str) -> str:
    normalized = text.replace("\r", "")
    start_token = "\n" + start + "\n"
    end_token = "\n" + end + "\n"
    start_index = normalized.find(start_token)
    end_index = normalized.find(end_token, start_index + len(start_token)) if start_index >= 0 else -1
    if start_index < 0 or end_index < 0:
        return ""
    lines = []
    body = normalized[start_index + len(start_token) : end_index]
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("+ "):
            continue
        lines.append(stripped)
    return "\n".join(lines)


def gzip_disk(root: Path, disk: Path) -> Path:
    output = root / "Mod_YlvaOS" / "vm" / "disk.qcow2.gz"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()

    print(f"Compressing {disk} -> {output}")
    with disk.open("rb") as source, gzip.open(temporary, "wb", compresslevel=9) as compressed:
        shutil.copyfileobj(source, compressed, length=1024 * 1024)
    temporary.replace(output)
    return output


def write_manifest(root: Path, kernel: Path, initrd: Path, disk_archive: Path | None, disk: Path | None) -> None:
    legal_dir = root / "Mod_YlvaOS" / "LEGAL"
    legal_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "{",
        '  "name": "YlvaOS Linux VM image",',
        f'  "ylvaosVersion": "{YLVAOS_VERSION}",',
        f'  "base": "Alpine Linux {ALPINE_VERSION} virt x86_64",',
        f'  "alpineIsoUrl": "{ALPINE_ISO_URL}",',
        f'  "alpineIsoSha256": "{ALPINE_ISO_SHA256}",',
        f'  "kernelSha256": "{sha256_file(kernel)}",',
        f'  "initrdSha256": "{sha256_file(initrd)}",',
    ]
    if disk_archive is not None and disk_archive.exists():
        lines.append(f'  "diskArchiveSha256": "{sha256_file(disk_archive)}",')
    if disk is not None and disk.exists():
        lines.append(f'  "diskQcow2Sha256": "{sha256_file(disk)}",')
    if lines[-1].endswith(","):
        lines[-1] = lines[-1][:-1]
    lines.append("}")
    (legal_dir / "YLVAOS_IMAGE_MANIFEST.json").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="rebuild the qcow2 disk if it already exists")
    parser.add_argument("--skip-disk", action="store_true", help="only download ISO and extract kernel/initrd")
    parser.add_argument("--disk-mib", type=int, default=DEFAULT_DISK_MIB)
    args = parser.parse_args()

    root = repo_root()
    iso = ensure_alpine_iso(root)
    kernel, initrd = extract_boot_assets(root, iso)

    disk = None
    disk_archive = None
    if not args.skip_disk:
        disk = create_preinstalled_disk(root, iso, args.disk_mib, args.force)
        disk_archive = gzip_disk(root, disk)

    write_manifest(root, kernel, initrd, disk_archive, disk)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
