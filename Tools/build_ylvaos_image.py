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
YLVAOS_VERSION = "0.05"

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
            "procps-ng",
            "vim",
            "nano",
            "doas",
            "less",
            "shadow",
            "linux-firmware-none",
            "linux-lts",
            "ca-certificates",
            "wget",
            "cabextract",
            "unzip",
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
            "dotool",
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

cat >/mnt/etc/modules <<'EOF'
snd-seq
snd-rawmidi
uinput
EOF

mkdir -p /mnt/home/ylva /mnt/etc/doas.d /mnt/etc/profile.d
sed -i 's/^root:[^:]*:/root::/' /mnt/etc/shadow
if grep -q '^wheel:' /mnt/etc/group; then
    sed -i 's/^wheel:.*/wheel:x:10:root/' /mnt/etc/group
else
    echo 'wheel:x:10:root' >>/mnt/etc/group
fi
if ! grep -q '^audio:' /mnt/etc/group; then
    echo 'audio:x:18:' >>/mnt/etc/group
fi
echo 'permit nopass :wheel' >/mnt/etc/doas.d/doas.conf
chmod 0600 /mnt/etc/doas.d/doas.conf

cat >/mnt/home/ylva/README.txt <<'EOF'
YlvaOS quick notes
==================

- nano and vim are preinstalled.
- Wine, PulseAudio, ALSA tools, FluidSynth, and a GM soundfont are preinstalled for lightweight desktop apps.
- The root disk lives under LocalLow/Lafrontier/Elin/YlvaOS/vm/disk.qcow2 after the MOD provisions it.
- Put host files in LocalLow/Lafrontier/Elin/YlvaOS/Import to expose them as a read-only guest drive.
- QEMU runtime networking is disabled by default in the MOD backend.
- Run ConnectNetwork and type yes to enable QEMU user-mode networking for this VM session.
- Run AppLauncher or YlvaOS launch to find and start desktop tools.
- Run TextEditor or YlvaOS edit to edit files. The Import folder is opened read-only.
- After connecting, use PackageManager or YlvaOS package commands to search, update, install, and remove apk packages.
- Run YlvaOS update after installing a newer MOD package to update YlvaOS-managed OS files from the bundled update drive.
- Run SystemMonitor or YlvaOS monitor from the desktop to inspect CPU, memory, disk, network, and processes.
- Run RepairMode or YlvaOS repair commands to back up and reset desktop/user configuration or check package state.
- Run YlvaOS snapshot commands at the YlvaOS login prompt before the VM starts to save or restore the root disk.
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
addgroup "$user" audio >/dev/null 2>&1 || true
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
export EDITOR=TextEditor
export VISUAL=TextEditor
export TERM=vt100
stty rows $rows cols $cols -ixon 2>/dev/null || true
export XDG_RUNTIME_DIR="/tmp/ylva-runtime-$user"
mkdir -p "/tmp/ylva-runtime-$user" "/tmp/ylva-runtime-$user/pulse" 2>/dev/null || true
chmod 700 "/tmp/ylva-runtime-$user" 2>/dev/null || true
export PULSE_SERVER="unix:/tmp/ylva-runtime-$user/pulse/native"
[ -f /usr/lib/ylvaos/wine-env ] && . /usr/lib/ylvaos/wine-env
ylva-start-audio >/tmp/ylva-audio.log 2>&1 || true
if command -v ylva-host-agent >/dev/null 2>&1 && ! pgrep -u "\$(id -u)" -f '[y]lva-host-agent' >/dev/null 2>&1; then
    ylva-host-agent >/tmp/ylva-host-agent.log 2>&1 &
fi
if [ "\${YLVA_SPLASH_SHOWN:-0}" != 1 ]; then
    export YLVA_SPLASH_SHOWN=1
    ylva-splash 2>/dev/null || true
fi
if [ "\${YLVA_UPDATE_NOTICE_SHOWN:-0}" != 1 ]; then
    export YLVA_UPDATE_NOTICE_SHOWN=1
    /usr/lib/ylvaos/update-from-mod --check 2>/dev/null || true
fi
export PS1='YlvaOS:\w\$ '
alias poweroff='doas poweroff'
alias reboot='doas reboot'
alias shutdown='doas poweroff'
alias apps='YlvaOS launch'
alias edit='YlvaOS edit'
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
export EDITOR=TextEditor
export VISUAL=TextEditor
export TERM=vt100
stty rows 32 cols 140 -ixon 2>/dev/null || true
EOF

cat >/mnt/etc/profile.d/ylvaos-editor.sh <<'EOF'
export EDITOR=TextEditor
export VISUAL=TextEditor
EOF

cat >/mnt/etc/profile.d/ylvaos-audio.sh <<'EOF'
[ -f /usr/lib/ylvaos/wine-env ] && . /usr/lib/ylvaos/wine-env
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

want_reply=0
reply_timeout="${YLVA_CONTROL_TIMEOUT:-4}"
while [ "$#" -gt 0 ]; do
    case "$1" in
        --reply)
            want_reply=1
            shift
            ;;
        --timeout)
            shift
            case "${1:-}" in
                ''|*[!0-9]* ) reply_timeout=4 ;;
                *) reply_timeout="$1" ;;
            esac
            shift || true
            ;;
        *)
            break
            ;;
    esac
done

message="$*"
token="$(get_arg ylva_control_token)"
port=/dev/virtio-ports/org.ylvaos.control

if [ -n "$message" ] && [ -n "$token" ] && [ -e "$port" ]; then
    if [ ! -w "$port" ] && [ "$(id -u)" -ne 0 ]; then
        doas chmod 0666 "$port" >/dev/null 2>&1 || true
    fi

    if [ "$want_reply" -eq 1 ]; then
        if exec 3<>"$port" 2>/dev/null && printf 'YLVAOS %s reply %s\n' "$token" "$message" >&3 2>/dev/null; then
            if IFS= read -r -t "$reply_timeout" reply <&3 2>/dev/null; then
                case "$reply" in
                    YLVAOS_REPLY\ *)
                        payload="${reply#YLVAOS_REPLY }"
                        printf '%s' "$payload" | base64 -d 2>/dev/null || true
                        printf '\n'
                        exit 0
                        ;;
                esac
            fi
            echo "YlvaOS host control did not reply."
            exit 1
        fi
    elif printf 'YLVAOS %s %s\n' "$token" "$message" >"$port" 2>/dev/null; then
        exit 0
    fi
fi

if [ "$want_reply" -eq 1 ]; then
    echo "YlvaOS host control is unavailable."
    exit 1
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

pointer_event() {
    x="$1"
    y="$2"
    previous="$3"
    current="$4"
    case "$x:$y:$previous:$current" in
        *[!0-9:]*|'') return 1 ;;
    esac

    [ -S /tmp/.X11-unix/X0 ] || return 1
    command -v xdotool >/dev/null 2>&1 || return 1
    run_xdotool() {
        printf 'xdotool'
        printf ' %s' "$@"
        if DISPLAY=:0 xdotool "$@"; then
            echo ' -> ok'
            return 0
        fi
        status=$?
        echo " -> failed ($status)"
        return "$status"
    }
    send_desktop_pointer() {
        [ -p /tmp/ylva-desktop-input ] || return 1
        printf '%s\n' "$*" >/tmp/ylva-desktop-input
    }
    move_pointer() {
        run_xdotool mousemove "$1" "$2" || send_desktop_pointer move "$1" "$2"
    }
    send_dotool_button() {
        button="$1"
        action="$2"
        command -v dotoolc >/dev/null 2>&1 || return 1
        [ -p /tmp/dotool-pipe ] || return 1
        case "$button" in
            1) name=left ;;
            2) name=middle ;;
            3) name=right ;;
            *) return 1 ;;
        esac
        case "$action" in
            click) command=click ;;
            down|up) command="button$action" ;;
            *) return 1 ;;
        esac
        if {
            awk -v x="$x" -v y="$y" -v width="$desktop_width" -v height="$desktop_height" '
                BEGIN {
                    if (width < 2) width = 1024
                    if (height < 2) height = 768
                    nx = x / (width - 1)
                    ny = y / (height - 1)
                    if (nx < 0) nx = 0
                    if (nx > 1) nx = 1
                    if (ny < 0) ny = 0
                    if (ny > 1) ny = 1
                    printf "mouseto %.8f %.8f\\n", nx, ny
                }
            '
            printf '%s %s\n' "$command" "$name"
        } | doas dotoolc >/dev/null 2>&1; then
            echo "dotool $command $name -> ok"
            return 0
        fi
        status=$?
        echo "dotool $command $name -> failed ($status)"
        return "$status"
    }

    echo "pointer $x $y $previous $current"
    transitioned=0
    for spec in '1 1' '2 2' '4 3'; do
        set -- $spec
        bit="$1"
        button="$2"
        was_down=$((previous & bit))
        is_down=$((current & bit))
        [ "$was_down" -eq "$is_down" ] && continue
        transitioned=1
        if [ "$is_down" -ne 0 ]; then
            pointer_press_x="$x"
            pointer_press_y="$y"
            pointer_press_button="$button"
            move_pointer "$x" "$y" || true
            pointer_dotool_button=
        else
            delta_x=$((x - ${pointer_press_x:-x}))
            delta_y=$((y - ${pointer_press_y:-y}))
            [ "$delta_x" -lt 0 ] && delta_x=$((-delta_x))
            [ "$delta_y" -lt 0 ] && delta_y=$((-delta_y))
            if [ "${pointer_dotool_button:-}" = "$button" ]; then
                send_dotool_button "$button" up || true
            elif [ "${pointer_press_button:-}" = "$button" ] && [ $((delta_x + delta_y)) -le 6 ]; then
                if ! send_dotool_button "$button" click; then
                    # A move to the current tablet coordinate can leave xdotool
                    # --sync waiting forever. Nudge first so it observes a real
                    # X pointer transition before sending the click.
                    if [ "$x" -gt 0 ]; then
                        nudge_x=$((x - 1))
                    else
                        nudge_x=$((x + 1))
                    fi
                    run_xdotool mousemove "$nudge_x" "$y" || true
                    sleep 0.05
                    run_xdotool mousemove --sync "$x" "$y" click "$button" || true
                fi
            else
                move_pointer "$x" "$y" || true
            fi
            pointer_press_button=
            pointer_dotool_button=
        fi
    done

    if [ $((current & 8)) -ne 0 ] && [ $((previous & 8)) -eq 0 ]; then
        transitioned=1
        send_desktop_pointer click "$x" "$y" 4 || run_xdotool mousemove "$x" "$y" click 4 || true
    fi
    if [ $((current & 16)) -ne 0 ] && [ $((previous & 16)) -eq 0 ]; then
        transitioned=1
        send_desktop_pointer click "$x" "$y" 5 || run_xdotool mousemove "$x" "$y" click 5 || true
    fi
    if [ "$transitioned" -eq 0 ]; then
        if [ -n "${pointer_press_button:-}" ] && [ -z "${pointer_dotool_button:-}" ]; then
            delta_x=$((x - ${pointer_press_x:-x}))
            delta_y=$((y - ${pointer_press_y:-y}))
            [ "$delta_x" -lt 0 ] && delta_x=$((-delta_x))
            [ "$delta_y" -lt 0 ] && delta_y=$((-delta_y))
            if [ $((delta_x + delta_y)) -gt 6 ] && send_dotool_button "$pointer_press_button" down; then
                pointer_dotool_button="$pointer_press_button"
            fi
        fi
        # Do not use --sync: the emulated absolute tablet can re-assert a
        # position one pixel away and leave xdotool waiting forever.
        move_pointer "$x" "$y" || true
    fi
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
        pointer\ *)
            set -- $body
            if [ "$#" -eq 5 ]; then
                pointer_event "$2" "$3" "$4" "$5"
            fi
            ;;
    esac
}

token="$(get_arg ylva_control_token)"
desktop_width="$(get_arg ylva_desktop_width)"
desktop_height="$(get_arg ylva_desktop_height)"
case "$desktop_width" in ''|*[!0-9]*) desktop_width=1024 ;; esac
case "$desktop_height" in ''|*[!0-9]*) desktop_height=768 ;; esac
port=/dev/virtio-ports/org.ylvaos.hostinput
ready_sent=0
pointer_press_x=
pointer_press_y=
pointer_press_button=
pointer_dotool_button=

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

    parec --device=ylva.monitor --format=s16le --rate=44100 --channels=2 --latency-msec=220 >"$port" 2>/tmp/ylva-audio-bridge.log || sleep 1
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

mkdir -p "$runtime" "$runtime/pulse"
chmod 700 "$runtime" 2>/dev/null || true

if ! pulseaudio --check >/dev/null 2>&1; then
    pulseaudio --daemonize=yes --exit-idle-time=-1 --realtime=false --log-target=file:/tmp/ylva-pulseaudio.log >/dev/null 2>&1 || true
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
pactl set-sink-volume ylva 100% >/dev/null 2>&1 || true

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

load_alsa_sequencer() {
    if [ "$(id -u)" -eq 0 ]; then
        modprobe snd-seq >>/tmp/ylva-audio-modprobe.log 2>&1 || true
        modprobe snd-seq-midi >>/tmp/ylva-audio-modprobe.log 2>&1 || true
        modprobe snd-rawmidi >>/tmp/ylva-audio-modprobe.log 2>&1 || true
        modprobe snd-timer >>/tmp/ylva-audio-modprobe.log 2>&1 || true
    else
        doas modprobe snd-seq >>/tmp/ylva-audio-modprobe.log 2>&1 || true
        doas modprobe snd-seq-midi >>/tmp/ylva-audio-modprobe.log 2>&1 || true
        doas modprobe snd-rawmidi >>/tmp/ylva-audio-modprobe.log 2>&1 || true
        doas modprobe snd-timer >>/tmp/ylva-audio-modprobe.log 2>&1 || true
    fi
    if [ -e /dev/snd/seq ]; then
        chmod 0666 /dev/snd/seq /dev/snd/timer >/dev/null 2>&1 || true
    fi
}

if pgrep -u "$(id -u)" fluidsynth >/dev/null 2>&1 && ! aplaymidi -l 2>/dev/null | grep -q 'FLUID Synth'; then
    pkill -u "$(id -u)" fluidsynth >/dev/null 2>&1 || true
    sleep 1
fi

if [ -n "$soundfont" ] && [ -f "$soundfont" ] && ! pgrep -u "$(id -u)" fluidsynth >/dev/null 2>&1; then
    load_alsa_sequencer
    fluidsynth -s -i -a pulseaudio -m alsa_seq -g 1.0 -o midi.autoconnect=1 -o audio.period-size=4096 -o audio.periods=8 "$soundfont" >/tmp/ylva-fluidsynth.log 2>&1 &
fi

rm -f /tmp/ylva-fluidsynth-port /tmp/ylva-fluidsynth-index
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if aplaymidi -l 2>/dev/null | grep -q 'FLUID Synth'; then
        aplaymidi -l 2>/dev/null | awk '
            NR == 1 { next }
            NF == 0 { next }
            {
                if ($0 ~ /FLUID Synth/) {
                    print $1 > "/tmp/ylva-fluidsynth-port"
                    print device_index > "/tmp/ylva-fluidsynth-index"
                    found = 1
                    exit
                }
                device_index++
            }
            END { exit found ? 0 : 1 }
        ' || true
        break
    fi
    sleep 1
done

if command -v ylva-midi-bridge >/dev/null 2>&1 && ! pgrep -u "$(id -u)" -f '[y]lva-midi-bridge' >/dev/null 2>&1; then
    ylva-midi-bridge >/tmp/ylva-midi-bridge.log 2>&1 &
fi
EOF
chmod 0755 /mnt/usr/bin/ylva-start-audio

cat >/mnt/usr/bin/ylva-midi-bridge <<'EOF'
#!/bin/sh
set -u
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

find_synth_port() {
    if [ -s /tmp/ylva-fluidsynth-port ]; then
        cat /tmp/ylva-fluidsynth-port
        return 0
    fi

    aplaymidi -l 2>/dev/null | awk '/FLUID Synth/ { print $1; exit }'
}

list_source_ports() {
    aconnect -o 2>/dev/null | awk '
        /^client / {
            client = $2
            sub(":", "", client)
            name = $0
            next
        }
        /^[[:space:]]+[0-9]+[[:space:]]/ {
            port = $1
            if (name ~ /WINE|wine|Midi Through/) {
                print client ":" port
            }
        }
    '
}

while :; do
    synth_port="$(find_synth_port 2>/dev/null || true)"
    if [ -n "$synth_port" ]; then
        for source_port in $(list_source_ports); do
            [ "$source_port" = "$synth_port" ] && continue
            aconnect "$source_port" "$synth_port" >/dev/null 2>&1 || true
        done
    fi
    sleep 1
done
EOF
chmod 0755 /mnt/usr/bin/ylva-midi-bridge

mkdir -p /mnt/etc/local.d
cat >/mnt/etc/local.d/ylva-audio.start <<'EOF'
#!/bin/sh
# The audio bridge is started from the logged-in user session by ylva-start-audio,
# so it can use that user's PulseAudio runtime directory.
exit 0
EOF
chmod 0755 /mnt/etc/local.d/ylva-audio.start

cat >/mnt/etc/local.d/ylva-input.start <<'EOF'
#!/bin/sh
modprobe uinput >/tmp/ylva-dotool.log 2>&1 || true
rm -f /tmp/dotool-pipe
if command -v dotoold >/dev/null 2>&1; then
    DOTOOL_XKB_LAYOUT=us dotoold >>/tmp/ylva-dotool.log 2>&1 &
    for _ in 1 2 3 4 5; do
        [ -p /tmp/dotool-pipe ] && break
        sleep 1
    done
    chmod 0666 /tmp/dotool-pipe >/dev/null 2>&1 || true
fi
exit 0
EOF
chmod 0755 /mnt/etc/local.d/ylva-input.start

mkdir -p /mnt/usr/lib/ylvaos
cat >/mnt/usr/lib/ylvaos/wine-env <<'EOF'
export MUSL_LOCPATH=/usr/share/i18n/locales/musl
export LANG=ja_JP.UTF-8
export LC_CTYPE=ja_JP.UTF-8
export LC_MESSAGES=C.UTF-8
export WINEPREFIX="${WINEPREFIX:-$HOME/.wine}"
export WINEDLLOVERRIDES="${WINEDLLOVERRIDES:-mscoree,mshtml=}"
export WINEDEBUG="${WINEDEBUG:--all}"
export PULSE_LATENCY_MSEC="${PULSE_LATENCY_MSEC:-220}"
if [ -z "${XDG_RUNTIME_DIR:-}" ]; then
    ylva_audio_user="$(id -un 2>/dev/null || printf ylva)"
    export XDG_RUNTIME_DIR="/tmp/ylva-runtime-$ylva_audio_user"
fi
mkdir -p "$XDG_RUNTIME_DIR" "$XDG_RUNTIME_DIR/pulse" 2>/dev/null || true
chmod 700 "$XDG_RUNTIME_DIR" 2>/dev/null || true
export PULSE_SERVER="${PULSE_SERVER:-unix:$XDG_RUNTIME_DIR/pulse/native}"
if [ -f /tmp/ylva-fluidsynth-port ]; then
    export ALSA_OUTPUT_PORTS="$(cat /tmp/ylva-fluidsynth-port 2>/dev/null || true)"
fi
EOF

cat >/mnt/usr/lib/ylvaos/registry-helpers <<'EOF'
#!/bin/sh

ensure_reg_file() {
    file="$1"
    mkdir -p "$(dirname "$file")" 2>/dev/null || true
    if [ ! -s "$file" ]; then
        printf 'WINE REGISTRY Version 2\n\n' >"$file"
    fi
}

write_reg_section() {
    file="$1"
    section="$2"
    tmp="$file.tmp"
    body="$(cat)"

    ensure_reg_file "$file"
    awk -v target="[$section]" '
        /^\[/ { skip = ($0 == target) }
        !skip { print }
    ' "$file" >"$tmp" && mv "$tmp" "$file"

    {
        printf '\n[%s] %s\n' "$section" "$(date +%s)"
        printf '%s\n' "$body"
    } >>"$file"
}
EOF

cat >/mnt/usr/lib/ylvaos/configure-wine-midi <<'EOF'
#!/bin/sh
set -u
. /usr/lib/ylvaos/wine-env
. /usr/lib/ylvaos/registry-helpers

mkdir -p "$WINEPREFIX"
if [ ! -f "$WINEPREFIX/system.reg" ] && [ -z "${DISPLAY:-}" ]; then
    echo "Wine prefix is not initialized yet; run YlvaOS setup wine first."
    exit 0
fi

if [ ! -f /tmp/ylva-fluidsynth-index ] || [ ! -f /tmp/ylva-fluidsynth-port ]; then
    ylva-start-audio >/tmp/ylva-audio.log 2>&1 || true
fi

midi_index="$(cat /tmp/ylva-fluidsynth-index 2>/dev/null || printf 0)"
case "$midi_index" in
    ''|*[!0-9]* ) midi_index=0 ;;
esac

target="#$midi_index"
marker="$WINEPREFIX/.ylvaos-midi-target"
if [ -f "$marker" ] && [ "$(cat "$marker" 2>/dev/null || true)" = "$target" ] && grep -Fq "\"CurrentInstrument\"=\"$target\"" "$WINEPREFIX/user.reg" 2>/dev/null; then
    exit 0
fi

write_reg_section "$WINEPREFIX/user.reg" 'Software\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\Multimedia\\\\MIDIMap' <<REG_MIDI
"UseScheme"=dword:00000000
"AutoScheme"=dword:00000000
"CurrentInstrument"="$target"
REG_MIDI

printf '%s\n' "$target" >"$marker" 2>/dev/null || true
echo "Wine MIDI output is mapped to FluidSynth device $target."
EOF
chmod 0755 /mnt/usr/lib/ylvaos/configure-wine-midi

cat >/mnt/usr/local/bin/wine <<'EOF'
#!/bin/sh
set -u
. /usr/lib/ylvaos/wine-env
case "${1:-}" in
    --version|--help|-h)
        exec /usr/bin/wine "$@"
        ;;
esac
ylva-start-audio >/tmp/ylva-audio.log 2>&1 || true
if [ -f "$WINEPREFIX/system.reg" ] || [ -n "${DISPLAY:-}" ]; then
    /usr/lib/ylvaos/configure-wine-midi >/tmp/ylva-wine-midi.log 2>&1 || true
fi
exec /usr/bin/wine "$@"
EOF
chmod 0755 /mnt/usr/local/bin/wine

for tool in wineboot winecfg wineconsole winefile regedit regsvr32 wineserver; do
    cat >"/mnt/usr/local/bin/$tool" <<EOF
#!/bin/sh
set -u
. /usr/lib/ylvaos/wine-env
ylva-start-audio >/tmp/ylva-audio.log 2>&1 || true
if [ -f "\$WINEPREFIX/system.reg" ] || [ -n "\${DISPLAY:-}" ]; then
    /usr/lib/ylvaos/configure-wine-midi >/tmp/ylva-wine-midi.log 2>&1 || true
fi
exec /usr/bin/$tool "\$@"
EOF
    chmod 0755 "/mnt/usr/local/bin/$tool"
done

cat >/mnt/usr/lib/ylvaos/setup-audio <<'EOF'
#!/bin/sh
set -u
. /usr/lib/ylvaos/wine-env

ylva-start-audio >/tmp/ylva-audio.log 2>&1 || true
sink_ready=0
midi_ready=0
if pactl list short sinks 2>/dev/null | awk '{print $2}' | grep -qx ylva; then
    sink_ready=1
fi
if aplaymidi -l 2>/dev/null | grep -q 'FLUID Synth'; then
    midi_ready=1
fi

if [ "$sink_ready" -eq 1 ] && [ "$midi_ready" -eq 1 ]; then
    echo "YlvaOS audio sink and MIDI synthesizer are ready."
    exit 0
fi

if [ "$sink_ready" -ne 1 ]; then
    echo "YlvaOS audio sink did not become ready. See /tmp/ylva-audio.log and /tmp/ylva-null-sink.log."
fi
if [ "$midi_ready" -ne 1 ]; then
    echo "YlvaOS MIDI synthesizer did not become ready. See /tmp/ylva-fluidsynth.log and /tmp/ylva-audio-modprobe.log."
fi
exit 1
EOF
chmod 0755 /mnt/usr/lib/ylvaos/setup-audio

cat >/mnt/usr/lib/ylvaos/setup-font <<'EOF'
#!/bin/sh
set -u
. /usr/lib/ylvaos/wine-env
. /usr/lib/ylvaos/registry-helpers

mkdir -p "$WINEPREFIX"
fc-cache -f >/tmp/ylva-font-cache.log 2>&1 || true

write_reg_section "$WINEPREFIX/system.reg" 'Software\\\\Microsoft\\\\Windows NT\\\\CurrentVersion\\\\FontSubstitutes' <<'REG_FONT_SYS'
"MS Gothic"="Noto Sans CJK JP"
"MS PGothic"="Noto Sans CJK JP"
"MS UI Gothic"="Noto Sans CJK JP"
"MS Mincho"="Noto Serif CJK JP"
REG_FONT_SYS

write_reg_section "$WINEPREFIX/user.reg" 'Software\\\\Wine\\\\Fonts\\\\Replacements' <<'REG_FONT_USER'
"MS Gothic"="Noto Sans CJK JP"
"MS PGothic"="Noto Sans CJK JP"
"MS UI Gothic"="Noto Sans CJK JP"
"MS Mincho"="Noto Serif CJK JP"
REG_FONT_USER

echo "YlvaOS fonts are ready."
EOF
chmod 0755 /mnt/usr/lib/ylvaos/setup-font

cat >/mnt/usr/lib/ylvaos/setup-wine <<'EOF'
#!/bin/sh
set -u
. /usr/lib/ylvaos/wine-env
. /usr/lib/ylvaos/registry-helpers

marker="$WINEPREFIX/.ylvaos-jp-wine-v7"
directmusic_marker="$WINEPREFIX/.ylvaos-directmusic-runtime-v2"
directmusic_requested="${1:-}"

as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        doas "$@"
    fi
}

has_network() {
    ip route 2>/dev/null | grep -q '^default '
}

install_winetricks() {
    if command -v winetricks >/dev/null 2>&1; then
        return 0
    fi

    if ! has_network; then
        echo "DirectMusic setup needs Internet access. Run ConnectNetwork first, type yes, then run YlvaOS setup wine directmusic again."
        return 1
    fi

    as_root apk update >/tmp/ylva-winetricks-apk-update.log 2>&1 || true
    as_root apk add cabextract unzip wget >/tmp/ylva-winetricks-apk.log 2>&1 || true
    if as_root apk add winetricks >>/tmp/ylva-winetricks-apk.log 2>&1; then
        return 0
    fi

    if command -v wget >/dev/null 2>&1; then
        as_root mkdir -p /usr/local/bin
        as_root wget -O /usr/local/bin/winetricks https://raw.githubusercontent.com/Winetricks/winetricks/master/src/winetricks >/tmp/ylva-winetricks-download.log 2>&1 || return 1
        as_root chmod 0755 /usr/local/bin/winetricks
        command -v winetricks >/dev/null 2>&1
        return $?
    fi

    return 1
}

maybe_setup_directmusic() {
    if [ "$directmusic_requested" != directmusic ]; then
        return 0
    fi

    if [ -f "$directmusic_marker" ]; then
        return 0
    fi

    answer="${YLVAOS_INSTALL_DIRECTMUSIC:-}"
    if [ -z "$answer" ] && [ -t 0 ]; then
        cat <<'DM_WARN'
YlvaOS can install legacy Microsoft DirectMusic components through winetricks.
This may improve MIDI playback in old Windows games, but it downloads and installs third-party Microsoft runtime files into this Wine prefix.
Only continue if you understand and accept the licensing and security implications.
Type yes to install DirectMusic now, or press Enter to skip.
DM_WARN
        printf '> '
        IFS= read -r answer || answer=
    fi

    if [ "$answer" != yes ]; then
        echo "DirectMusic runtime setup skipped."
        return 0
    fi

    if ! install_winetricks; then
        echo "DirectMusic runtime setup could not prepare winetricks."
        return 1
    fi

    winetricks -q directmusic gmdls >/tmp/ylva-winetricks-directmusic.log 2>&1 || {
        echo "DirectMusic runtime setup failed. See /tmp/ylva-winetricks-directmusic.log."
        return 1
    }

    touch "$directmusic_marker"
    echo "DirectMusic runtime is ready for this Wine prefix."
}

print_directmusic_hint() {
    if [ "$directmusic_requested" = directmusic ] || [ -f "$directmusic_marker" ]; then
        return 0
    fi

    cat <<'DM_HINT'
For MIDI music in legacy DirectMusic games such as Elona, run:
  ConnectNetwork
  YlvaOS setup wine directmusic
DM_HINT
}

apply_wine_registry_settings() {
    write_reg_section "$WINEPREFIX/user.reg" 'Software\\\\Wine\\\\Drivers' <<'REG_WINE_DRIVERS'
"Audio"="pulse"
REG_WINE_DRIVERS

    write_reg_section "$WINEPREFIX/user.reg" 'Software\\\\Wine\\\\DirectSound' <<'REG_WINE_DSOUND'
"HardwareAcceleration"="Emulation"
"DefaultSampleRate"="44100"
"DefaultBitsPerSample"="16"
REG_WINE_DSOUND

    /usr/lib/ylvaos/configure-wine-midi >/tmp/ylva-wine-midi.log 2>&1 || true

    write_reg_section "$WINEPREFIX/user.reg" 'Control Panel\\\\International' <<'REG_WINE_INTL'
"LocaleName"="ja-JP"
"sCountry"="Japan"
"sLanguage"="JPN"
REG_WINE_INTL

    write_reg_section "$WINEPREFIX/system.reg" 'System\\\\CurrentControlSet\\\\Control\\\\Nls\\\\CodePage' <<'REG_WINE_CODEPAGE'
"ACP"="932"
"OEMCP"="932"
"MACCP"="10001"
REG_WINE_CODEPAGE

    write_reg_section "$WINEPREFIX/system.reg" 'System\\\\CurrentControlSet\\\\Control\\\\Nls\\\\Language' <<'REG_WINE_LANGUAGE'
"Default"="0411"
"InstallLanguage"="0411"
REG_WINE_LANGUAGE

    /usr/lib/ylvaos/setup-font >/tmp/ylva-font-setup.log 2>&1 || true

    # Wine owns the in-memory registry once wineserver starts. Import the same
    # values through reg.exe so existing prefixes and the live registry agree.
    : >/tmp/ylva-wine-registry.log
    wine_reg_add() {
        timeout 30 /usr/bin/wine reg add "$@" /f >>/tmp/ylva-wine-registry.log 2>&1
    }

    wine_reg_add 'HKCU\\Software\\Wine\\Drivers' /v Audio /t REG_SZ /d pulse || true
    wine_reg_add 'HKCU\\Software\\Wine\\DirectSound' /v HardwareAcceleration /t REG_SZ /d Emulation || true
    wine_reg_add 'HKCU\\Software\\Wine\\DirectSound' /v DefaultSampleRate /t REG_SZ /d 44100 || true
    wine_reg_add 'HKCU\\Software\\Wine\\DirectSound' /v DefaultBitsPerSample /t REG_SZ /d 16 || true
    wine_reg_add 'HKCU\\Control Panel\\International' /v LocaleName /t REG_SZ /d ja-JP || true
    wine_reg_add 'HKCU\\Control Panel\\International' /v sCountry /t REG_SZ /d Japan || true
    wine_reg_add 'HKCU\\Control Panel\\International' /v sLanguage /t REG_SZ /d JPN || true
    wine_reg_add 'HKLM\\System\\CurrentControlSet\\Control\\Nls\\CodePage' /v ACP /t REG_SZ /d 932 || true
    wine_reg_add 'HKLM\\System\\CurrentControlSet\\Control\\Nls\\CodePage' /v OEMCP /t REG_SZ /d 932 || true
    wine_reg_add 'HKLM\\System\\CurrentControlSet\\Control\\Nls\\CodePage' /v MACCP /t REG_SZ /d 10001 || true
    wine_reg_add 'HKLM\\System\\CurrentControlSet\\Control\\Nls\\Language' /v Default /t REG_SZ /d 0411 || true
    wine_reg_add 'HKLM\\System\\CurrentControlSet\\Control\\Nls\\Language' /v InstallLanguage /t REG_SZ /d 0411 || true

    for font_name in 'MS Gothic' 'MS PGothic' 'MS UI Gothic'; do
        wine_reg_add 'HKLM\\Software\\Microsoft\\Windows NT\\CurrentVersion\\FontSubstitutes' /v "$font_name" /t REG_SZ /d 'Noto Sans CJK JP' || true
        wine_reg_add 'HKCU\\Software\\Wine\\Fonts\\Replacements' /v "$font_name" /t REG_SZ /d 'Noto Sans CJK JP' || true
    done
    wine_reg_add 'HKLM\\Software\\Microsoft\\Windows NT\\CurrentVersion\\FontSubstitutes' /v 'MS Mincho' /t REG_SZ /d 'Noto Serif CJK JP' || true
    wine_reg_add 'HKCU\\Software\\Wine\\Fonts\\Replacements' /v 'MS Mincho' /t REG_SZ /d 'Noto Serif CJK JP' || true

    midi_target="$(cat "$WINEPREFIX/.ylvaos-midi-target" 2>/dev/null || true)"
    if [ -n "$midi_target" ]; then
        wine_reg_add 'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Multimedia\\MIDIMap' /v UseScheme /t REG_DWORD /d 0 || true
        wine_reg_add 'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Multimedia\\MIDIMap' /v AutoScheme /t REG_DWORD /d 0 || true
        wine_reg_add 'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Multimedia\\MIDIMap' /v CurrentInstrument /t REG_SZ /d "$midi_target" || true
    fi
    timeout 15 /usr/bin/wineserver -w >>/tmp/ylva-wine-registry.log 2>&1 || true
}

if [ -f "$marker" ]; then
    /usr/lib/ylvaos/setup-audio >/tmp/ylva-audio.log 2>&1 || true
    apply_wine_registry_settings
    echo "Wine prefix is ready at $WINEPREFIX."
    if [ "$directmusic_requested" = directmusic ]; then
        maybe_setup_directmusic
    else
        print_directmusic_hint
    fi
    exit 0
fi

mkdir -p "$WINEPREFIX"
/usr/lib/ylvaos/setup-audio >/tmp/ylva-audio.log 2>&1 || true
timeout 45 /usr/bin/wineboot -u >/tmp/ylva-wineboot.log 2>&1 || true
timeout 15 /usr/bin/wineserver -w >/tmp/ylva-wineserver-wait.log 2>&1 || true
apply_wine_registry_settings

touch "$marker"
echo "Wine prefix is ready at $WINEPREFIX."
if [ "$directmusic_requested" = directmusic ]; then
    maybe_setup_directmusic
else
    print_directmusic_hint
fi
EOF
chmod 0755 /mnt/usr/lib/ylvaos/setup-wine

cat >/mnt/usr/lib/ylvaos/update-from-mod <<'EOF'
#!/bin/sh
set -u
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

check_only=0
case "${1:-}" in
    --check) check_only=1 ;;
    '') ;;
    *)
        echo "usage: update-from-mod [--check]"
        exit 2
        ;;
esac

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
    if [ "$check_only" -eq 1 ]; then
        exit 0
    fi
    echo "No YlvaOS MOD update source was found."
    echo "Install a newer MOD package that contains vm/update, then start YlvaOS again."
    exit 1
fi

target_version="$(read_release_value "$mount_dir/ylvaos-release" YLVAOS_VERSION)"
target_alpine="$(read_release_value "$mount_dir/ylvaos-release" ALPINE_VERSION)"
if [ -z "$target_version" ]; then
    if [ "$check_only" -eq 1 ]; then
        exit 0
    fi
    echo "The YlvaOS MOD update source is missing ylvaos-release."
    exit 1
fi

if ! version_gt "$current_version" "$target_version"; then
    if [ "$check_only" -eq 0 ]; then
        echo "Installed: YlvaOS $current_version"
        echo "Bundled:   Alpine Linux ${target_alpine:-unknown} base / YlvaOS $target_version"
        echo "YlvaOS is already up to date."
    fi
    exit 0
fi

if [ ! -f "$mount_dir/rootfs-overlay.tar.gz" ] || [ ! -f "$mount_dir/update.sh" ]; then
    if [ "$check_only" -eq 1 ]; then
        exit 0
    fi
    echo "The YlvaOS MOD update source is incomplete."
    exit 1
fi

if [ "$check_only" -eq 1 ]; then
    echo
    echo "A YlvaOS update is available from the installed MOD."
    echo "Installed: YlvaOS $current_version"
    echo "Bundled:   Alpine Linux ${target_alpine:-unknown} base / YlvaOS $target_version"
    echo 'Run "YlvaOS update" to install it. YlvaOS will restart automatically.'
    echo
    exit 0
fi

echo "Installed: YlvaOS $current_version"
echo "Bundled:   Alpine Linux ${target_alpine:-unknown} base / YlvaOS $target_version"

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

cat >/mnt/usr/lib/ylvaos/text-editor <<'EOF'
#!/bin/sh
set -u
set -f
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
export MUSL_LOCPATH="${MUSL_LOCPATH:-/usr/share/i18n/locales/musl}"
export LANG="${LANG:-ja_JP.UTF-8}"
export LC_CTYPE="${LC_CTYPE:-ja_JP.UTF-8}"
export LC_MESSAGES="${LC_MESSAGES:-C.UTF-8}"

home_dir="${HOME:-/home/ylva}"

usage() {
    cat <<'USAGE'
usage: TextEditor [file]
       TextEditor edit <file>
       TextEditor view <file>
       TextEditor check <file>
       TextEditor status

YlvaOS edit is an alias for TextEditor.
Files under ~/Import are opened read-only because the host Import drive is mounted read-only.
USAGE
}

as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        doas "$@"
    fi
}

ensure_user_dirs() {
    mkdir -p "$home_dir/Desktop" "$home_dir/Documents" "$home_dir/Downloads" "$home_dir/Pictures" "$home_dir/Import"
}

ensure_import_mount() {
    ensure_user_dirs
    if mountpoint -q "$home_dir/Import" 2>/dev/null; then
        return 0
    fi

    for dev in /dev/vdb1 /dev/vdb /dev/vdc1 /dev/vdc /dev/sda1 /dev/sda /dev/sdb1 /dev/sdb; do
        [ -b "$dev" ] || continue
        as_root mount -t vfat -o ro,uid="$(id -u)",gid="$(id -g)",utf8=1 "$dev" "$home_dir/Import" >/tmp/ylva-import-mount.log 2>&1 && return 0
    done

    return 1
}

expand_path() {
    value="${1:-}"
    case "$value" in
        ''|home|Home)
            value="$home_dir/Documents/notes.txt"
            ;;
        '~')
            value="$home_dir"
            ;;
        '~/'*)
            value="$home_dir/${value#~/}"
            ;;
        import|Import)
            ensure_import_mount >/dev/null 2>&1 || true
            value="$home_dir/Import"
            ;;
        import:*|Import:*)
            ensure_import_mount >/dev/null 2>&1 || true
            value="$home_dir/Import/${value#*:}"
            ;;
    esac
    printf '%s\n' "$value"
}

canonical_path() {
    path="$1"
    if resolved="$(readlink -f "$path" 2>/dev/null)"; then
        printf '%s\n' "$resolved"
        return
    fi

    parent="$(dirname "$path")"
    base="$(basename "$path")"
    if resolved_parent="$(readlink -f "$parent" 2>/dev/null)"; then
        printf '%s/%s\n' "$resolved_parent" "$base"
    else
        printf '%s\n' "$path"
    fi
}

is_import_path() {
    path="$(canonical_path "$1")"
    import_root="$(canonical_path "$home_dir/Import")"
    case "$path" in
        "$import_root"|"$import_root"/*)
            return 0
            ;;
    esac
    return 1
}

status_command() {
    echo "YlvaOS Text Editor"
    echo "=================="
    echo
    echo "Editor command: TextEditor"
    if command -v nano >/dev/null 2>&1; then
        echo "Backend: nano"
    else
        echo "Backend: vim"
    fi
    echo "CLI alias: YlvaOS edit"
    echo "EDITOR=${EDITOR:-TextEditor}"
    echo "VISUAL=${VISUAL:-TextEditor}"
    echo "Default file: $home_dir/Documents/notes.txt"
    if mountpoint -q "$home_dir/Import" 2>/dev/null; then
        echo "Import: mounted read-only"
    else
        echo "Import: not mounted"
    fi
    echo "Locale: ${LANG:-unknown}"
}

check_path_command() {
    ensure_user_dirs
    target="$(expand_path "${1:-}")"
    case "$target" in
        "$home_dir/Import"|"$home_dir/Import"/*)
            ensure_import_mount >/dev/null 2>&1 || true
            ;;
    esac
    if is_import_path "$target"; then
        echo "Mode: read-only"
        echo "Path: $target"
    else
        echo "Mode: editable"
        echo "Path: $target"
    fi
}

open_editor() {
    mode="$1"
    shift || true
    ensure_user_dirs
    target="$(expand_path "${1:-}")"
    case "$target" in
        "$home_dir/Import"|"$home_dir/Import"/*)
            ensure_import_mount >/dev/null 2>&1 || true
            ;;
    esac
    if [ -d "$target" ]; then
        target="$target/untitled.txt"
    fi

    read_only=0
    if [ "$mode" = view ]; then
        read_only=1
    fi
    if is_import_path "$target"; then
        read_only=1
    fi

    if [ "$read_only" != 1 ]; then
        parent="$(dirname "$target")"
        mkdir -p "$parent" 2>/dev/null || true
    fi

    if [ "$read_only" = 1 ]; then
        echo "Opening read-only: $target"
        echo "Use a file outside ~/Import to save changes."
        if command -v nano >/dev/null 2>&1; then
            exec nano -v "$target"
        fi
        exec vim -R "$target"
    fi

    if command -v nano >/dev/null 2>&1; then
        exec nano "$target"
    fi
    exec vim '+set paste' '+startinsert' "$target"
}

case "${1:-}" in
    help|--help|-h)
        usage
        ;;
    status|--status)
        status_command
        ;;
    check|--check)
        shift || true
        check_path_command "${1:-}"
        ;;
    view|read)
        shift || true
        open_editor view "$@"
        ;;
    edit|new|open)
        shift || true
        open_editor edit "$@"
        ;;
    *)
        open_editor edit "$@"
        ;;
esac
EOF
chmod 0755 /mnt/usr/lib/ylvaos/text-editor

cat >/mnt/usr/bin/TextEditor <<'EOF'
#!/bin/sh
set -u
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
export MUSL_LOCPATH="${MUSL_LOCPATH:-/usr/share/i18n/locales/musl}"
export LANG="${LANG:-ja_JP.UTF-8}"
export LC_CTYPE="${LC_CTYPE:-ja_JP.UTF-8}"
export LC_MESSAGES="${LC_MESSAGES:-C.UTF-8}"

case "${1:-}" in
    status|--status|check|--check|help|--help|-h)
        exec /usr/lib/ylvaos/text-editor "$@"
        ;;
esac

if [ -n "${DISPLAY:-}" ] && command -v xterm >/dev/null 2>&1 && [ "${YLVA_EDITOR_INLINE:-0}" != 1 ]; then
    YLVA_EDITOR_INLINE=1 xterm -title "YlvaOS Text Editor" -geometry 104x32+176+136 -e /usr/lib/ylvaos/text-editor "$@"
    exit $?
fi

exec /usr/lib/ylvaos/text-editor "$@"
EOF
chmod 0755 /mnt/usr/bin/TextEditor
ln -sf /usr/bin/TextEditor /mnt/usr/bin/texteditor
ln -sf /usr/bin/TextEditor /mnt/usr/bin/Editor
ln -sf /usr/bin/TextEditor /mnt/usr/bin/editor

cat >/mnt/usr/lib/ylvaos/app-launcher <<'EOF'
#!/bin/sh
set -u
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

usage() {
    cat <<'USAGE'
usage: AppLauncher
       AppLauncher --list
       AppLauncher --run <app-or-command>

YlvaOS launch is an alias for AppLauncher.
The first launcher entries are Terminal, File Manager, Settings, Text Editor, and System Monitor.
USAGE
}

fixed_entries() {
    cat <<'ENTRIES'
Terminal	Terminal	Open a YlvaOS terminal window
File Manager	Files	Browse home folders and the read-only Import drive
Settings	Settings	Configure YlvaOS memory, disk, desktop, audio, and network
Text Editor	TextEditor	Edit text files with the YlvaOS editor
System Monitor	SystemMonitor	Inspect CPU, memory, disk, network, and processes
Snapshot Manager	SnapshotManager	Manage root disk snapshots
Package Manager	PackageManager	Search, update, install, and remove Alpine apk packages
Repair Mode	RepairMode	Repair desktop, user config, packages, and serial login
Return to Kernel	Kernel	Leave the desktop and return to kernel console mode
ENTRIES
}

normalize_name() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -d ' _-'
}

valid_command_name() {
    name="${1:-}"
    case "$name" in
        ''|-*|*/*|*\\*|*[!abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._+@:-]*)
            return 1
            ;;
    esac
    [ "${#name}" -le 128 ]
}

resolve_fixed_command() {
    wanted="$(normalize_name "$1")"
    fixed_entries | while IFS="$(printf '\t')" read -r label command description; do
        [ -n "$label" ] || continue
        if [ "$wanted" = "$(normalize_name "$label")" ] || [ "$wanted" = "$(normalize_name "$command")" ]; then
            printf '%s\n' "$command"
            exit 0
        fi
    done
}

path_commands() {
    printf '%s' "$PATH" | tr ':' '\n' | while IFS= read -r dir; do
        [ -d "$dir" ] || continue
        for path in "$dir"/*; do
            [ -f "$path" ] || continue
            [ -x "$path" ] || continue
            basename "$path"
        done
    done | sort -u |
        grep -Ev '^(AppLauncher|applauncher|ApplicationLauncher|applicationlauncher|Launcher|launcher|app-launcher|TextEditor|texteditor|Editor|editor|YlvaOS|desktop|Desktop|kernel|Kernel)$' |
        head -n 80
}

launcher_entries() {
    fixed_entries
    path_commands | while IFS= read -r command; do
        [ -n "$command" ] || continue
        printf '%s\t%s\t%s\n' "$command" "$command" "Run command in a terminal window"
    done
}

list_command() {
    echo "YlvaOS Application Launcher"
    echo "==========================="
    echo
    launcher_entries | awk -F '\t' '{ printf "%2d) %-20s %s\\n", NR, $1, $3 }'
}

run_command_entry() {
    requested="${1:-}"
    if [ -z "$requested" ]; then
        echo "Application name is required."
        return 2
    fi

    command="$(resolve_fixed_command "$requested" || true)"
    if [ -z "$command" ]; then
        if ! valid_command_name "$requested" || ! command -v "$requested" >/dev/null 2>&1; then
            echo "Application was not found: $requested"
            return 1
        fi
        command="$requested"
    fi

    case "$command" in
        Terminal|Files|Settings|TextEditor|SystemMonitor|SnapshotManager|PackageManager|RepairMode|Kernel)
            "$command" &
            ;;
        *)
            if [ -n "${DISPLAY:-}" ] && command -v xterm >/dev/null 2>&1; then
                xterm -title "YlvaOS: $command" -geometry 100x28+192+152 -e "$command" &
            else
                "$command"
            fi
            ;;
    esac
    echo "Launched: $command"
}

pick_entry() {
    index="$1"
    case "$index" in
        ''|*[!0-9]*)
            return 1
            ;;
    esac
    launcher_entries | sed -n "${index}p" | awk -F '\t' '{ print $2 }'
}

pause_screen() {
    printf '\nPress Enter to continue... '
    IFS= read -r _ || true
}

tui() {
    while :; do
        clear 2>/dev/null || true
        list_command
        cat <<'MENU'

Type a number or application name. Use q to quit.
MENU
        printf 'Launch: '
        IFS= read -r choice || exit 0
        case "$choice" in
            q|Q|'')
                exit 0
                ;;
            *[!0-9]*)
                run_command_entry "$choice" || true
                pause_screen
                ;;
            *)
                command="$(pick_entry "$choice" || true)"
                if [ -n "$command" ]; then
                    run_command_entry "$command" || true
                else
                    echo "Unknown selection."
                fi
                pause_screen
                ;;
        esac
    done
}

case "${1:-}" in
    help|--help|-h)
        usage
        ;;
    --list|list)
        list_command
        ;;
    --run|run|open|launch)
        shift || true
        run_command_entry "${1:-}"
        ;;
    '')
        tui
        ;;
    *)
        run_command_entry "$1"
        ;;
esac
EOF
chmod 0755 /mnt/usr/lib/ylvaos/app-launcher

cat >/mnt/usr/bin/AppLauncher <<'EOF'
#!/bin/sh
set -u
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

case "${1:-}" in
    --list|list|--run|run|open|launch|help|--help|-h)
        exec /usr/lib/ylvaos/app-launcher "$@"
        ;;
esac

if [ "$#" -eq 0 ] && [ -n "${DISPLAY:-}" ] && command -v xterm >/dev/null 2>&1 && [ "${YLVA_LAUNCHER_INLINE:-0}" != 1 ]; then
    YLVA_LAUNCHER_INLINE=1 xterm -title "YlvaOS Application Launcher" -geometry 104x34+128+96 -e /usr/lib/ylvaos/app-launcher &
    exit 0
fi

exec /usr/lib/ylvaos/app-launcher "$@"
EOF
chmod 0755 /mnt/usr/bin/AppLauncher
ln -sf /usr/bin/AppLauncher /mnt/usr/bin/applauncher
ln -sf /usr/bin/AppLauncher /mnt/usr/bin/ApplicationLauncher
ln -sf /usr/bin/AppLauncher /mnt/usr/bin/applicationlauncher
ln -sf /usr/bin/AppLauncher /mnt/usr/bin/Launcher
ln -sf /usr/bin/AppLauncher /mnt/usr/bin/launcher

cat >/mnt/usr/lib/ylvaos/snapshot-tui <<'EOF'
#!/bin/sh
set -u
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

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

confirm_action() {
    message="$1"
    if command -v dialog >/dev/null 2>&1 && [ -t 1 ]; then
        dialog --clear --yesno "$message" 8 72
        return $?
    fi

    printf '%s\nType yes to continue: ' "$message"
    IFS= read -r answer || answer=
    [ "$answer" = yes ]
}

show_list() {
    YlvaOS snapshot list || true
}

create_snapshot() {
    name="$(read_value 'Snapshot name')"
    memo="$(read_value 'Memo')"
    YlvaOS snapshot create "$name" "$memo" || true
    pause_screen
}

restore_snapshot() {
    name="$(read_value 'Snapshot to restore')"
    if confirm_action "Restore snapshot '$name'? This replaces only the YlvaOS root disk. Settings and Import are not overwritten."; then
        YlvaOS snapshot restore "$name" --yes || true
    else
        echo "Snapshot restore cancelled."
    fi
    pause_screen
}

delete_snapshot() {
    name="$(read_value 'Snapshot to delete')"
    if confirm_action "Delete snapshot '$name'?"; then
        YlvaOS snapshot delete "$name" --yes || true
    else
        echo "Snapshot delete cancelled."
    fi
    pause_screen
}

while :; do
    clear 2>/dev/null || true
    echo "YlvaOS Snapshot Manager"
    echo "======================="
    echo
    show_list
    cat <<'MENU'

Snapshot create, restore, and delete are available only while the VM is stopped.
Shut down with poweroff, reopen the computer, and run YlvaOS snapshot commands at the login prompt.

1) Create snapshot
2) Restore snapshot
3) Delete snapshot
4) Refresh
5) Quit

MENU
    printf 'Select: '
    IFS= read -r choice || exit 0
    case "$choice" in
        1) create_snapshot ;;
        2) restore_snapshot ;;
        3) delete_snapshot ;;
        4) ;;
        5|q|Q) exit 0 ;;
        *) echo "Unknown selection."; pause_screen ;;
    esac
done
EOF
chmod 0755 /mnt/usr/lib/ylvaos/snapshot-tui

cat >/mnt/usr/bin/SnapshotManager <<'EOF'
#!/bin/sh
set -u
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

if [ -n "${DISPLAY:-}" ] && command -v xterm >/dev/null 2>&1 && [ "${YLVA_SNAPSHOT_INLINE:-0}" != 1 ]; then
    YLVA_SNAPSHOT_INLINE=1 xterm -title "YlvaOS Snapshot Manager" -geometry 104x32+112+88 -e /usr/lib/ylvaos/snapshot-tui &
    exit 0
fi

exec /usr/lib/ylvaos/snapshot-tui
EOF
chmod 0755 /mnt/usr/bin/SnapshotManager
ln -sf /usr/bin/SnapshotManager /mnt/usr/bin/snapshotmanager

cat >/mnt/usr/lib/ylvaos/system-monitor-tui <<'EOF'
#!/bin/sh
set -u
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

sort_by=cpu
interval=2
cpu_state=/tmp/ylva-monitor-cpu-prev

read_cpu_percent() {
    line="$(awk '/^cpu / { idle=$5 + $6; total=0; for (i=2; i<=NF; i++) total += $i; printf "%s %s\\n", total, idle; exit }' /proc/stat 2>/dev/null || true)"
    set -- $line
    total="${1:-0}"
    idle="${2:-0}"
    prev_total=
    prev_idle=
    if [ -r "$cpu_state" ]; then
        read -r prev_total prev_idle <"$cpu_state" || true
    fi
    printf '%s %s\\n' "$total" "$idle" >"$cpu_state" 2>/dev/null || true
    awk -v total="$total" -v idle="$idle" -v prev_total="${prev_total:-}" -v prev_idle="${prev_idle:-}" '
        BEGIN {
            if (prev_total == "" || total <= prev_total) {
                printf "sampling"
                exit
            }
            dt = total - prev_total
            di = idle - prev_idle
            if (dt <= 0) {
                printf "sampling"
            } else {
                printf "%.1f%%", (dt - di) * 100 / dt
            }
        }
    '
}

print_memory() {
    awk '
        /^MemTotal:/ { total=$2 }
        /^MemAvailable:/ { available=$2 }
        END {
            if (total > 0) {
                used = total - available
                printf "%.0f MiB used / %.0f MiB total (%.1f%%)\\n", used / 1024, total / 1024, used * 100 / total
            } else {
                print "unknown"
            }
        }
    ' /proc/meminfo 2>/dev/null
}

print_disk() {
    df -h / 2>/dev/null | awk 'NR==2 { printf "%s used / %s total (%s), %s available\\n", $3, $2, $5, $4 }'
}

print_network() {
    interfaces="$(for path in /sys/class/net/*; do name="${path##*/}"; [ "$name" = lo ] && continue; printf '%s ' "$name"; done 2>/dev/null)"
    if ip route 2>/dev/null | grep -q '^default '; then
        printf 'connected'
    else
        printf 'disabled'
    fi
    if [ -n "$interfaces" ]; then
        printf ' (%s)' "$interfaces"
    fi
    printf '\n'
}

print_host_status() {
    status="$(ylva-control --reply --timeout 1 host status 2>/dev/null || true)"
    if [ -n "$status" ]; then
        printf '%s\n' "$status"
    else
        echo "Host QEMU: unavailable from this session"
    fi
}

print_processes() {
    case "$sort_by" in
        pid) sort_arg=pid ;;
        name) sort_arg=comm ;;
        mem) sort_arg=-rss ;;
        *) sort_arg=-pcpu ;;
    esac

    printf '%6s  %-24s  %6s  %9s\\n' PID PROCESS CPU RSS
    if ps -eo pid=,comm=,pcpu=,rss= --sort="$sort_arg" >/tmp/ylva-monitor-ps 2>/dev/null; then
        awk 'NF >= 4 && NR <= 18 { printf "%6s  %-24.24s  %6.1f  %8.1fM\\n", $1, $2, $3, $4 / 1024 }' /tmp/ylva-monitor-ps
        return
    fi

    ps 2>/dev/null | awk 'NR > 1 && NR <= 18 { printf "%6s  %-24.24s  %6s  %9s\\n", $1, $4, "-", "-" }'
}

render_screen() {
    echo "YlvaOS System Monitor"
    echo "====================="
    echo
    printf 'Guest CPU: '
    read_cpu_percent
    printf '\n'
    printf 'Guest memory: '
    print_memory
    printf 'Root disk: '
    print_disk
    printf 'Guest network: '
    print_network
    print_host_status
    echo
    echo "Processes (sorted by $sort_by)"
    print_processes
}

protected_process() {
    pid="$1"
    case "$pid" in
        ''|*[!0-9]*|1) return 0 ;;
    esac

    if [ ! -d "/proc/$pid" ]; then
        return 1
    fi

    comm="$(cat "/proc/$pid/comm" 2>/dev/null || true)"
    case "$comm" in
        init|openrc|agetty|ylva-getty|Xorg|xinit|startx|openbox|openbox-session|tint2|xterm|dbus-daemon|pulseaudio|ylva-*)
            return 0
            ;;
    esac

    cmdline="$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)"
    case "$cmdline" in
        *ylva-desktop-session*|*ylva-start-desktop*|*system-monitor-tui*)
            return 0
            ;;
    esac

    return 1
}

kill_process() {
    pid="${1:-}"
    assume_yes="${2:-0}"
    case "$pid" in
        ''|*[!0-9]*)
            echo "PID must be a positive integer."
            return 2
            ;;
    esac

    if [ ! -d "/proc/$pid" ]; then
        echo "PID $pid is not running."
        return 1
    fi

    if protected_process "$pid"; then
        echo "Refusing to terminate protected process PID $pid."
        return 1
    fi

    comm="$(cat "/proc/$pid/comm" 2>/dev/null || printf '?')"
    if [ "$assume_yes" != 1 ]; then
        printf 'Terminate PID %s (%s)? Type yes to continue: ' "$pid" "$comm"
        IFS= read -r answer || answer=
        if [ "$answer" != yes ]; then
            echo "Process termination cancelled."
            return 1
        fi
    fi

    if kill -TERM "$pid" 2>/tmp/ylva-monitor-kill.log; then
        sleep 1
        if kill -0 "$pid" 2>/dev/null; then
            echo "Sent TERM to PID $pid, but it is still running."
        else
            echo "Terminated PID $pid."
        fi
        return 0
    fi

    echo "Could not terminate PID $pid."
    cat /tmp/ylva-monitor-kill.log 2>/dev/null || true
    return 1
}

case "${1:-}" in
    --once)
        render_screen
        exit 0
        ;;
    --kill)
        assume=0
        if [ "${3:-}" = "--yes" ] || [ "${3:-}" = "-y" ]; then
            assume=1
        fi
        kill_process "${2:-}" "$assume"
        exit $?
        ;;
esac

while :; do
    clear 2>/dev/null || true
    render_screen
    cat <<'MENU'

[c] CPU sort  [m] memory sort  [p] PID sort  [n] name sort  [k] terminate PID  [q] quit
MENU
    printf 'Select: '
    if IFS= read -r -t "$interval" choice; then
        case "$choice" in
            c|C) sort_by=cpu ;;
            m|M) sort_by=mem ;;
            p|P) sort_by=pid ;;
            n|N) sort_by=name ;;
            k|K)
                printf 'PID: '
                IFS= read -r pid || pid=
                kill_process "$pid" 0
                printf '\nPress Enter to continue... '
                IFS= read -r _ || true
                ;;
            q|Q) exit 0 ;;
        esac
    fi
done
EOF
chmod 0755 /mnt/usr/lib/ylvaos/system-monitor-tui

cat >/mnt/usr/bin/SystemMonitor <<'EOF'
#!/bin/sh
set -u
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

case "${1:-}" in
    --once|--kill)
        exec /usr/lib/ylvaos/system-monitor-tui "$@"
        ;;
esac

if [ -n "${DISPLAY:-}" ] && command -v xterm >/dev/null 2>&1 && [ "${YLVA_MONITOR_INLINE:-0}" != 1 ]; then
    YLVA_MONITOR_INLINE=1 xterm -title "YlvaOS System Monitor" -geometry 112x34+128+96 -e /usr/lib/ylvaos/system-monitor-tui "$@" &
    exit 0
fi

exec /usr/lib/ylvaos/system-monitor-tui "$@"
EOF
chmod 0755 /mnt/usr/bin/SystemMonitor
ln -sf /usr/bin/SystemMonitor /mnt/usr/bin/systemmonitor

cat >/mnt/usr/lib/ylvaos/package-helper <<'EOF'
#!/bin/sh
set -u
set -f
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

log_dir="${HOME:-/home/ylva}/YlvaOS/logs"
log_file="$log_dir/package.log"

usage() {
    cat <<'USAGE'
usage: YlvaOS package status
       YlvaOS package update
       YlvaOS package search <name>
       YlvaOS package install [--yes] <package...>
       YlvaOS package remove [--yes] <package...>
       YlvaOS package log

PackageManager opens the interactive package helper.
YlvaOS uses Alpine Linux, so packages are managed with apk, not apt.
USAGE
}

ensure_log() {
    mkdir -p "$log_dir" 2>/dev/null || true
    touch "$log_file" 2>/dev/null || true
}

log_event() {
    ensure_log
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$log_file" 2>/dev/null || true
}

run_apk() {
    if [ "$(id -u)" -eq 0 ]; then
        apk "$@"
    else
        doas apk "$@"
    fi
}

network_connected() {
    ip route 2>/dev/null | grep -q '^default '
}

print_network_state() {
    if network_connected; then
        echo "Network: connected for this VM session"
    else
        echo "Network: disabled"
    fi
}

require_network() {
    if network_connected; then
        return 0
    fi

    cat <<'NETWORK'
Network is disabled for this VM session.
Run ConnectNetwork, read the English warning, and type yes before package search, update, or install.
NETWORK
    return 1
}

disk_summary() {
    df -h / 2>/dev/null | awk 'NR==2 { printf "Root disk: %s available, %s used of %s (%s)\\n", $4, $3, $2, $5 }'
}

valid_package_name() {
    pkg="${1:-}"
    case "$pkg" in
        ''|-*|*/*|*\\*|*[!abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._+@:-]*)
            return 1
            ;;
    esac
    [ "${#pkg}" -le 128 ]
}

validate_packages() {
    if [ "$#" -eq 0 ]; then
        echo "At least one package name is required."
        return 2
    fi

    for pkg in "$@"; do
        if ! valid_package_name "$pkg"; then
            echo "Invalid package name: $pkg"
            echo "Use plain Alpine package names such as vim, htop, or dosbox."
            return 2
        fi
    done
}

confirm_action() {
    message="$1"
    assume_yes="${2:-0}"
    if [ "$assume_yes" = 1 ]; then
        return 0
    fi

    if command -v dialog >/dev/null 2>&1 && [ -t 1 ]; then
        dialog --clear --yesno "$message" 8 72
        return $?
    fi

    printf '%s Type yes to continue: ' "$message"
    IFS= read -r answer || answer=
    [ "$answer" = yes ]
}

parse_yes() {
    assume_yes=0
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --yes|-y)
                assume_yes=1
                shift
                ;;
            --)
                shift
                break
                ;;
            *)
                break
                ;;
        esac
    done
    printf '%s\n' "$assume_yes"
    return 0
}

status_command() {
    echo "YlvaOS Package Manager Helper"
    echo "============================="
    echo
    print_network_state
    disk_summary
    if [ -f /lib/apk/db/installed ]; then
        count="$(grep -c '^P:' /lib/apk/db/installed 2>/dev/null || printf 0)"
        echo "Installed packages: $count"
    fi
    echo "Log file: $log_file"
    echo
    echo "Tip: use apk package names, not apt package names."
}

update_command() {
    require_network || return 1
    ensure_log
    log_event "apk update"
    run_apk update >/tmp/ylva-package-run.log 2>&1
    status=$?
    cat /tmp/ylva-package-run.log | tee -a "$log_file"
    return "$status"
}

search_command() {
    query="${1:-}"
    if ! valid_package_name "$query"; then
        echo "Search requires one plain package name or prefix."
        return 2
    fi
    require_network || return 1
    ensure_log
    log_event "apk search $query"
    run_apk search "$query" >/tmp/ylva-package-run.log 2>&1
    status=$?
    cat /tmp/ylva-package-run.log | tee -a "$log_file"
    return "$status"
}

install_command() {
    assume_yes=0
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --yes|-y)
                assume_yes=1
                shift
                ;;
            --)
                shift
                break
                ;;
            *)
                break
                ;;
        esac
    done

    validate_packages "$@" || return $?
    require_network || return 1
    ensure_log
    echo "Install request"
    echo "==============="
    print_network_state
    disk_summary
    printf 'Packages:'
    for pkg in "$@"; do
        printf ' %s' "$pkg"
    done
    printf '\n\n'
    echo "APK simulation follows. It includes dependency and size information when apk can calculate it."
    if ! run_apk --simulate add "$@" >/tmp/ylva-package-plan.log 2>&1; then
        cat /tmp/ylva-package-plan.log | tee -a "$log_file"
        log_event "apk --simulate add failed: $*"
        echo "Package install was not started."
        return 1
    fi
    cat /tmp/ylva-package-plan.log | tee -a "$log_file"

    if ! confirm_action "Install these packages?" "$assume_yes"; then
        echo "Package install cancelled."
        return 1
    fi

    log_event "apk add $*"
    run_apk add "$@" >/tmp/ylva-package-run.log 2>&1
    status=$?
    cat /tmp/ylva-package-run.log | tee -a "$log_file"
    return "$status"
}

remove_command() {
    assume_yes=0
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --yes|-y)
                assume_yes=1
                shift
                ;;
            --)
                shift
                break
                ;;
            *)
                break
                ;;
        esac
    done

    validate_packages "$@" || return $?
    ensure_log
    echo "Remove request"
    echo "=============="
    print_network_state
    disk_summary
    printf 'Packages:'
    for pkg in "$@"; do
        printf ' %s' "$pkg"
    done
    printf '\n\n'
    echo "APK simulation follows."
    if ! run_apk --simulate del "$@" >/tmp/ylva-package-plan.log 2>&1; then
        cat /tmp/ylva-package-plan.log | tee -a "$log_file"
        log_event "apk --simulate del failed: $*"
        echo "Package removal was not started."
        return 1
    fi
    cat /tmp/ylva-package-plan.log | tee -a "$log_file"

    if ! confirm_action "Remove these packages?" "$assume_yes"; then
        echo "Package removal cancelled."
        return 1
    fi

    log_event "apk del $*"
    run_apk del "$@" >/tmp/ylva-package-run.log 2>&1
    status=$?
    cat /tmp/ylva-package-run.log | tee -a "$log_file"
    return "$status"
}

show_log() {
    if [ -s "$log_file" ]; then
        tail -n 80 "$log_file"
    else
        echo "No package log has been written yet."
    fi
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

tui() {
    while :; do
        clear 2>/dev/null || true
        status_command
        cat <<'MENU'

1) Search packages
2) Update package index
3) Install packages
4) Remove packages
5) Show package log
6) Quit

MENU
        printf 'Select: '
        IFS= read -r choice || exit 0
        case "$choice" in
            1)
                query="$(read_value 'Search name')"
                search_command "$query" || true
                pause_screen
                ;;
            2)
                update_command || true
                pause_screen
                ;;
            3)
                names="$(read_value 'Packages to install')"
                install_command $names || true
                pause_screen
                ;;
            4)
                names="$(read_value 'Packages to remove')"
                remove_command $names || true
                pause_screen
                ;;
            5)
                show_log
                pause_screen
                ;;
            6|q|Q)
                exit 0
                ;;
            *)
                echo "Unknown selection."
                pause_screen
                ;;
        esac
    done
}

case "${1:-}" in
    ''|menu|tui)
        tui
        ;;
    help|--help|-h)
        usage
        ;;
    status)
        status_command
        ;;
    update)
        shift || true
        update_command
        ;;
    search|find)
        shift || true
        search_command "${1:-}"
        ;;
    install|add)
        shift || true
        install_command "$@"
        ;;
    remove|del|delete|rm)
        shift || true
        remove_command "$@"
        ;;
    log)
        show_log
        ;;
    *)
        usage
        exit 2
        ;;
esac
EOF
chmod 0755 /mnt/usr/lib/ylvaos/package-helper

cat >/mnt/usr/bin/PackageManager <<'EOF'
#!/bin/sh
set -u
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

if [ "$#" -eq 0 ] && [ -n "${DISPLAY:-}" ] && command -v xterm >/dev/null 2>&1 && [ "${YLVA_PACKAGE_INLINE:-0}" != 1 ]; then
    YLVA_PACKAGE_INLINE=1 xterm -title "YlvaOS Package Manager" -geometry 104x32+144+112 -e /usr/lib/ylvaos/package-helper &
    exit 0
fi

exec /usr/lib/ylvaos/package-helper "$@"
EOF
chmod 0755 /mnt/usr/bin/PackageManager
ln -sf /usr/bin/PackageManager /mnt/usr/bin/packagemanager

cat >/mnt/usr/lib/ylvaos/repair-mode <<'EOF'
#!/bin/sh
set -u
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

home_dir="${HOME:-/home/ylva}"
log_dir="$home_dir/YlvaOS/logs"
log_file="$log_dir/repair.log"
backup_root="$home_dir/YlvaOS/backups"
backup_dir=

usage() {
    cat <<'USAGE'
usage: YlvaOS repair status
       YlvaOS repair desktop [--yes]
       YlvaOS repair user-config [--yes]
       YlvaOS repair packages [--check|--yes]
       YlvaOS repair safe-console [--yes]
       YlvaOS repair login [--yes]
       YlvaOS repair all [--yes]

RepairMode opens the interactive repair helper.
Destructive repairs back up affected files under ~/YlvaOS/backups first.
USAGE
}

ensure_dirs() {
    mkdir -p "$log_dir" "$backup_root" 2>/dev/null || true
}

log_event() {
    ensure_dirs
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$log_file" 2>/dev/null || true
}

run_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        doas "$@"
    fi
}

run_apk() {
    if [ "$(id -u)" -eq 0 ]; then
        apk "$@"
    else
        doas apk "$@"
    fi
}

make_backup_dir() {
    ensure_dirs
    if [ -z "$backup_dir" ]; then
        backup_dir="$backup_root/repair-$(date '+%Y%m%d-%H%M%S')-$$"
        mkdir -p "$backup_dir"
    fi
}

backup_item() {
    src="$1"
    [ -e "$src" ] || [ -L "$src" ] || return 0
    make_backup_dir
    rel="$(printf '%s' "$src" | sed 's#^/##')"
    dest="$backup_dir/$rel"
    mkdir -p "$(dirname "$dest")"
    if cp -a "$src" "$dest" 2>/dev/null; then
        echo "Backed up $src"
        return 0
    fi
    if run_root cp -a "$src" "$dest" 2>/dev/null; then
        echo "Backed up $src"
        return 0
    fi
    echo "Warning: could not back up $src"
    return 1
}

confirm_action() {
    message="$1"
    assume_yes="${2:-0}"
    if [ "$assume_yes" = 1 ]; then
        return 0
    fi

    if command -v dialog >/dev/null 2>&1 && [ -t 1 ]; then
        dialog --clear --yesno "$message" 8 72
        return $?
    fi

    printf '%s Type yes to continue: ' "$message"
    IFS= read -r answer || answer=
    [ "$answer" = yes ]
}

network_connected() {
    ip route 2>/dev/null | grep -q '^default '
}

print_network_state() {
    if network_connected; then
        echo "Network: connected for this VM session"
    else
        echo "Network: disabled"
    fi
}

disk_summary() {
    df -h / 2>/dev/null | awk 'NR==2 { printf "Root disk: %s available, %s used of %s (%s)\\n", $4, $3, $2, $5 }'
}

status_command() {
    echo "YlvaOS Repair Mode"
    echo "=================="
    echo
    print_network_state
    disk_summary
    if [ -f "$home_dir/.ylvaos-safe-console" ]; then
        echo "Desktop safe-console marker: enabled"
    else
        echo "Desktop safe-console marker: disabled"
    fi
    if [ -f /lib/apk/db/installed ]; then
        count="$(grep -c '^P:' /lib/apk/db/installed 2>/dev/null || printf 0)"
        echo "Package database: present ($count packages)"
    else
        echo "Package database: missing"
    fi
    echo "Repair log: $log_file"
    echo "Backup folder: $backup_root"
}

reset_desktop_files() {
    backup_item "$home_dir/.Xresources" || true
    backup_item "$home_dir/.config/openbox" || true
    backup_item "$home_dir/.config/tint2" || true
    rm -rf "$home_dir/.config/openbox" "$home_dir/.config/tint2" "$home_dir/.Xresources"
    mkdir -p "$home_dir/.config/openbox" "$home_dir/.config/tint2"
    rm -f "$home_dir/.ylvaos-safe-console"
    rm -f /tmp/ylva-desktop-input /tmp/ylva-desktop-input.pid /tmp/ylva-monitor-cpu-prev 2>/dev/null || true
    echo "Desktop configuration was reset. Run Desktop to regenerate Openbox and tint2 files."
}

desktop_command() {
    assume_yes="${1:-0}"
    if ! confirm_action "Reset desktop config and clear safe-console mode?" "$assume_yes"; then
        echo "Desktop repair cancelled."
        return 1
    fi
    log_event "repair desktop"
    reset_desktop_files
    [ -n "$backup_dir" ] && echo "Backup written to $backup_dir"
}

write_user_profile() {
    cat >"$home_dir/.profile" <<'EOF_PROFILE'
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
[ -f /etc/profile.d/ylvaos-locale.sh ] && . /etc/profile.d/ylvaos-locale.sh
[ -f /etc/profile.d/ylvaos-editor.sh ] && . /etc/profile.d/ylvaos-editor.sh
[ -f /etc/profile.d/ylvaos-audio.sh ] && . /etc/profile.d/ylvaos-audio.sh
EOF_PROFILE
    cat >"$home_dir/.ashrc" <<'EOF_ASHRC'
alias ll='ls -la'
alias apps='YlvaOS launch'
alias edit='YlvaOS edit'
alias pkg='YlvaOS package'
alias repair='YlvaOS repair'
EOF_ASHRC
}

user_config_command() {
    assume_yes="${1:-0}"
    if ! confirm_action "Back up and recreate YlvaOS user configuration files?" "$assume_yes"; then
        echo "User config repair cancelled."
        return 1
    fi
    log_event "repair user-config"
    backup_item "$home_dir/.profile" || true
    backup_item "$home_dir/.ashrc" || true
    backup_item "$home_dir/.Xresources" || true
    backup_item "$home_dir/.config/openbox" || true
    backup_item "$home_dir/.config/tint2" || true
    mkdir -p "$home_dir/.config" "$home_dir/YlvaOS/logs" "$home_dir/Import"
    write_user_profile
    reset_desktop_files
    [ -n "$backup_dir" ] && echo "Backup written to $backup_dir"
}

packages_command() {
    apply=0
    assume_yes=0
    case "${1:-}" in
        --yes|-y)
            apply=1
            assume_yes=1
            ;;
        --apply)
            apply=1
            ;;
        --check|'')
            apply=0
            ;;
        *)
            echo "Unknown packages option: $1"
            return 2
            ;;
    esac

    ensure_dirs
    if [ "$apply" != 1 ]; then
        echo "Checking package state with apk --simulate fix."
        log_event "apk --simulate fix"
        run_apk --simulate fix >/tmp/ylva-repair-package.log 2>&1
        status=$?
        cat /tmp/ylva-repair-package.log | tee -a "$log_file"
        echo "No package changes were applied. Run YlvaOS repair packages --yes to apply apk fix."
        return "$status"
    fi

    if ! network_connected; then
        echo "Network is disabled. Local package metadata can still be checked, but reinstalling missing packages may require ConnectNetwork."
    fi
    if ! confirm_action "Apply apk fix to repair installed packages?" "$assume_yes"; then
        echo "Package repair cancelled."
        return 1
    fi
    log_event "apk fix"
    run_apk fix >/tmp/ylva-repair-package.log 2>&1
    status=$?
    cat /tmp/ylva-repair-package.log | tee -a "$log_file"
    return "$status"
}

safe_console_command() {
    assume_yes="${1:-0}"
    if ! confirm_action "Enable safe-console mode and prevent normal Desktop startup?" "$assume_yes"; then
        echo "Safe-console repair cancelled."
        return 1
    fi
    log_event "repair safe-console"
    mkdir -p "$home_dir"
    cat >"$home_dir/.ylvaos-safe-console" <<'EOF_MARKER'
YlvaOS Repair Mode safe-console is enabled.
Run YlvaOS repair desktop --yes to reset desktop config and re-enable normal Desktop startup.
Run Desktop --force for a one-time desktop start.
EOF_MARKER
    echo "Safe-console mode enabled. Desktop will refuse normal startup until desktop repair clears this marker."
}

login_command() {
    assume_yes="${1:-0}"
    if ! confirm_action "Repair serial console login entries in /etc/inittab and /etc/securetty?" "$assume_yes"; then
        echo "Login repair cancelled."
        return 1
    fi
    log_event "repair login"
    backup_item /etc/securetty || true
    backup_item /etc/inittab || true
    run_root sh -c 'touch /etc/securetty; grep -qx ttyS0 /etc/securetty || echo ttyS0 >>/etc/securetty'
    run_root sed -i '/ttyS0::respawn:/d' /etc/inittab
    run_root sh -c "echo 'ttyS0::respawn:/sbin/ylva-getty' >>/etc/inittab"
    echo "Serial console login entries were repaired."
    [ -n "$backup_dir" ] && echo "Backup written to $backup_dir"
}

all_command() {
    assume_yes="${1:-0}"
    desktop_command "$assume_yes" || return $?
    user_config_command "$assume_yes" || return $?
    packages_command --check || return $?
    login_command "$assume_yes" || return $?
}

show_log() {
    if [ -s "$log_file" ]; then
        tail -n 80 "$log_file"
    else
        echo "No repair log has been written yet."
    fi
}

pause_screen() {
    printf '\nPress Enter to continue... '
    IFS= read -r _ || true
}

tui() {
    while :; do
        clear 2>/dev/null || true
        status_command
        cat <<'MENU'

1) Reset desktop config
2) Repair user config
3) Check package state
4) Apply package repair
5) Enable safe-console mode
6) Repair serial login
7) Show repair log
8) Quit

MENU
        printf 'Select: '
        IFS= read -r choice || exit 0
        case "$choice" in
            1) desktop_command 0 || true; pause_screen ;;
            2) user_config_command 0 || true; pause_screen ;;
            3) packages_command --check || true; pause_screen ;;
            4) packages_command --apply || true; pause_screen ;;
            5) safe_console_command 0 || true; pause_screen ;;
            6) login_command 0 || true; pause_screen ;;
            7) show_log; pause_screen ;;
            8|q|Q) exit 0 ;;
            *) echo "Unknown selection."; pause_screen ;;
        esac
    done
}

assume_yes=0
case "${2:-}" in
    --yes|-y)
        assume_yes=1
        ;;
esac

case "${1:-}" in
    ''|menu|tui)
        tui
        ;;
    help|--help|-h)
        usage
        ;;
    status)
        status_command
        ;;
    desktop)
        desktop_command "$assume_yes"
        ;;
    user-config|user|config)
        user_config_command "$assume_yes"
        ;;
    packages|package)
        shift || true
        packages_command "${1:-}"
        ;;
    safe-console|console)
        safe_console_command "$assume_yes"
        ;;
    login|serial)
        login_command "$assume_yes"
        ;;
    all)
        all_command "$assume_yes"
        ;;
    log)
        show_log
        ;;
    *)
        usage
        exit 2
        ;;
esac
EOF
chmod 0755 /mnt/usr/lib/ylvaos/repair-mode

cat >/mnt/usr/bin/RepairMode <<'EOF'
#!/bin/sh
set -u
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

if [ "$#" -eq 0 ] && [ -n "${DISPLAY:-}" ] && command -v xterm >/dev/null 2>&1 && [ "${YLVA_REPAIR_INLINE:-0}" != 1 ]; then
    YLVA_REPAIR_INLINE=1 xterm -title "YlvaOS Repair Mode" -geometry 104x32+160+128 -e /usr/lib/ylvaos/repair-mode &
    exit 0
fi

exec /usr/lib/ylvaos/repair-mode "$@"
EOF
chmod 0755 /mnt/usr/bin/RepairMode
ln -sf /usr/bin/RepairMode /mnt/usr/bin/repairmode

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
    awk '/^MemTotal:/ { printf "Guest memory: %.0f MiB\\n", $2 / 1024 }' /proc/meminfo 2>/dev/null || true
    df -h / 2>/dev/null | awk 'NR==2 { printf "Root disk: %s used / %s total (%s)\\n", $3, $2, $5 }' || true
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
7) Application launcher
8) Text editor
9) Open file manager
10) Snapshot manager
11) System monitor
12) Package manager
13) Repair mode
14) Quit

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
        7) AppLauncher; pause_screen ;;
        8) TextEditor; pause_screen ;;
        9) Files; pause_screen ;;
        10) SnapshotManager; pause_screen ;;
        11) SystemMonitor; pause_screen ;;
        12) PackageManager; pause_screen ;;
        13) RepairMode; pause_screen ;;
        14|q|Q) exit 0 ;;
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

cat >/mnt/usr/bin/ylva-desktop-input-agent <<'EOF'
#!/bin/sh
set -u
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
export DISPLAY=:0

fifo=/tmp/ylva-desktop-input
rm -f "$fifo"
mkfifo -m 0600 "$fifo"
exec 3<>"$fifo"

run_xdotool() {
    printf 'xdotool'
    printf ' %s' "$@"
    if xdotool "$@"; then
        echo ' -> ok'
        return 0
    fi
    status=$?
    echo " -> failed ($status)"
    return "$status"
}

while IFS=' ' read -r action x y button <&3; do
    case "$x:$y" in
        *[!0-9:]*|'') continue ;;
    esac
    case "$action" in
        move)
            run_xdotool mousemove "$x" "$y" || true
            ;;
        click)
            case "${button:-}" in 1|2|3|4|5) ;; *) continue ;; esac
            if [ "$x" -gt 0 ]; then
                nudge_x=$((x - 1))
            else
                nudge_x=$((x + 1))
            fi
            if setsid sh -c "unset XAUTHORITY; DISPLAY=:0 xdotool mousemove $nudge_x $y; sleep 0.05; DISPLAY=:0 xdotool mousemove --sync $x $y click $button"; then
                echo "setsid click $x $y $button -> ok"
            else
                echo "setsid click $x $y $button -> failed ($?)"
            fi
            ;;
    esac
done
EOF
chmod 0755 /mnt/usr/bin/ylva-desktop-input-agent

cat >/mnt/usr/local/bin/ylva-desktop-session <<'EOF'
#!/bin/sh
set -u
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
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
export EDITOR=TextEditor
export VISUAL=TextEditor
export XDG_RUNTIME_DIR="/tmp/ylva-runtime-$user"
mkdir -p "$XDG_RUNTIME_DIR" "$XDG_RUNTIME_DIR/pulse" "$home/.config/openbox" "$home/.config/tint2"
chmod 700 "$XDG_RUNTIME_DIR" 2>/dev/null || true
export PULSE_SERVER="unix:$XDG_RUNTIME_DIR/pulse/native"
ylva-start-audio >/tmp/ylva-audio.log 2>&1 || true
if [ -s /tmp/ylva-desktop-input.pid ]; then
    kill "$(cat /tmp/ylva-desktop-input.pid)" >/dev/null 2>&1 || true
fi
rm -f /tmp/ylva-desktop-input /tmp/ylva-desktop-input.pid
ylva-desktop-input-agent >/tmp/ylva-desktop-input.log 2>&1 &
echo $! >/tmp/ylva-desktop-input.pid

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
    <keybind key="C-A-space">
      <action name="Execute">
        <command>AppLauncher</command>
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
      <mousebind button="A-Left" action="Press">
        <action name="Focus"/>
        <action name="Raise"/>
      </mousebind>
      <mousebind button="A-Left" action="Drag">
        <action name="Move"/>
      </mousebind>
      <mousebind button="A-Right" action="Press">
        <action name="Focus"/>
        <action name="Raise"/>
        <action name="Unshade"/>
      </mousebind>
      <mousebind button="A-Right" action="Drag">
        <action name="Resize"/>
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
      <mousebind button="Left" action="Press">
        <action name="Focus"/>
        <action name="Raise"/>
      </mousebind>
      <mousebind button="Left" action="Click">
        <action name="Iconify"/>
      </mousebind>
    </context>
    <context name="Maximize">
      <mousebind button="Left" action="Press">
        <action name="Focus"/>
        <action name="Raise"/>
        <action name="Unshade"/>
      </mousebind>
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
      <mousebind button="Left" action="Press">
        <action name="Focus"/>
        <action name="Raise"/>
        <action name="Unshade"/>
      </mousebind>
      <mousebind button="Left" action="Click">
        <action name="Close"/>
      </mousebind>
    </context>
    <context name="Client">
      <mousebind button="Left" action="Press">
        <action name="Focus"/>
        <action name="Raise"/>
      </mousebind>
      <mousebind button="Middle" action="Press">
        <action name="Focus"/>
        <action name="Raise"/>
      </mousebind>
      <mousebind button="Right" action="Press">
        <action name="Focus"/>
        <action name="Raise"/>
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
    <item label="Application Launcher">
      <action name="Execute">
        <command>AppLauncher</command>
      </action>
    </item>
    <item label="Terminal">
      <action name="Execute">
        <command>Terminal</command>
      </action>
    </item>
    <item label="File Manager">
      <action name="Execute">
        <command>Files</command>
      </action>
    </item>
    <item label="Text Editor">
      <action name="Execute">
        <command>TextEditor</command>
      </action>
    </item>
    <item label="Settings">
      <action name="Execute">
        <command>Settings</command>
      </action>
    </item>
    <item label="System Monitor">
      <action name="Execute">
        <command>SystemMonitor</command>
      </action>
    </item>
    <separator />
    <item label="Snapshot Manager">
      <action name="Execute">
        <command>SnapshotManager</command>
      </action>
    </item>
    <item label="Package Manager">
      <action name="Execute">
        <command>PackageManager</command>
      </action>
    </item>
    <item label="Repair Mode">
      <action name="Execute">
        <command>RepairMode</command>
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
if [ -s /tmp/ylva-desktop-input.pid ]; then
    kill "$(cat /tmp/ylva-desktop-input.pid)" >/dev/null 2>&1 || true
fi
rm -f /tmp/ylva-desktop-input /tmp/ylva-desktop-input.pid
exit 0
EOF
chmod 0755 /mnt/usr/sbin/ylva-stop-desktop

cat >/mnt/usr/bin/Desktop <<'EOF'
#!/bin/sh
set -u
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

force=0
case "${1:-}" in
    --force|-f)
        force=1
        ;;
esac

user="${USER:-ylva}"
home="${HOME:-/home/$user}"
if [ "$force" != 1 ] && [ -f "$home/.ylvaos-safe-console" ]; then
    echo "YlvaOS Repair Mode safe-console is enabled."
    echo "Desktop startup is disabled so you can repair broken GUI configuration from the console."
    echo "Run YlvaOS repair desktop --yes to reset desktop config and re-enable normal Desktop startup."
    echo "Run Desktop --force for a one-time desktop start without clearing the marker."
    ylva-control mode kernel
    exit 1
fi

ylva-control mode desktop-starting
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
    echo "usage: YlvaOS status | YlvaOS update | YlvaOS launch [app] | YlvaOS edit [file]"
    echo "       YlvaOS settings | YlvaOS files [path] | YlvaOS monitor"
    echo "       YlvaOS snapshot list | create <name> [memo] | restore <name> | delete <name>"
    echo "       YlvaOS package status|update|search <name>|install [--yes] <package...>|remove [--yes] <package...>"
    echo "       YlvaOS repair status|desktop [--yes]|packages [--check|--yes]|user-config [--yes]|safe-console [--yes]"
    echo "       YlvaOS set memory <MiB> | YlvaOS set disk <MiB>"
    echo "       YlvaOS set desktop <WxH>|<width> <height> | YlvaOS set fps <FPS>"
    echo "       YlvaOS setup wine [directmusic] | YlvaOS setup font|audio"
}

is_positive_int() {
    case "$1" in
        ''|*[!0-9]* ) return 1 ;;
    esac

    [ "$1" -gt 0 ] 2>/dev/null
}

valid_snapshot_name() {
    name="${1:-}"
    case "$name" in
        ''|.|..|.*|*.|*[!abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-]*)
            return 1
            ;;
    esac

    device_name="${name%%.*}"
    upper="$(printf '%s' "$device_name" | tr '[:lower:]' '[:upper:]')"
    case "$upper" in
        CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])
            return 1
            ;;
    esac

    [ "${#name}" -le 64 ]
}

memo_base64() {
    printf '%s' "$*" | base64 | tr -d '\n'
}

confirm_snapshot() {
    action="$1"
    name="$2"
    assume_yes="$3"
    if [ "$assume_yes" = 1 ]; then
        return 0
    fi

    if command -v dialog >/dev/null 2>&1 && [ -t 1 ]; then
        dialog --clear --yesno "Snapshot $action '$name'?" 8 64
        return $?
    fi

    printf 'Snapshot %s %s? Type yes to continue: ' "$action" "$name"
    IFS= read -r answer || answer=
    [ "$answer" = yes ]
}

snapshot_command() {
    subcommand="${1:-list}"
    shift || true
    case "$subcommand" in
        list|ls)
            ylva-control --reply snapshot list
            ;;
        create|save)
            name="${1:-}"
            shift || true
            if ! valid_snapshot_name "$name"; then
                echo "snapshot name may contain only ASCII letters, digits, dot, underscore, and hyphen; it cannot start or end with a dot or use a Windows reserved device name"
                exit 2
            fi
            memo="$(memo_base64 "$*")"
            ylva-control --reply snapshot create "$name" "$memo"
            ;;
        restore)
            name="${1:-}"
            shift || true
            assume_yes=0
            if [ "${1:-}" = "--yes" ] || [ "${1:-}" = "-y" ]; then
                assume_yes=1
            fi
            if ! valid_snapshot_name "$name"; then
                echo "invalid snapshot name"
                exit 2
            fi
            if confirm_snapshot restore "$name" "$assume_yes"; then
                ylva-control --reply snapshot restore "$name"
            else
                echo "Snapshot restore cancelled."
            fi
            ;;
        delete|remove|rm)
            name="${1:-}"
            shift || true
            assume_yes=0
            if [ "${1:-}" = "--yes" ] || [ "${1:-}" = "-y" ]; then
                assume_yes=1
            fi
            if ! valid_snapshot_name "$name"; then
                echo "invalid snapshot name"
                exit 2
            fi
            if confirm_snapshot delete "$name" "$assume_yes"; then
                ylva-control --reply snapshot delete "$name"
            else
                echo "Snapshot delete cancelled."
            fi
            ;;
        *)
            usage
            exit 2
            ;;
    esac
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
    launch|launcher|apps|app)
        shift || true
        AppLauncher "$@"
        ;;
    edit|editor|text|text-editor|texteditor)
        shift || true
        TextEditor "$@"
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
    monitor|system-monitor|systemmonitor)
        shift || true
        SystemMonitor "$@"
        ;;
    snapshot|snapshots)
        shift || true
        snapshot_command "$@"
        ;;
    package|packages|pkg)
        shift || true
        /usr/lib/ylvaos/package-helper "$@"
        ;;
    repair)
        shift || true
        /usr/lib/ylvaos/repair-mode "$@"
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
                if [ -n "${3:-}" ]; then
                    /usr/lib/ylvaos/setup-wine "$3"
                else
                    /usr/lib/ylvaos/setup-wine
                fi
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
etc/local.d/ylva-input.start
etc/modules
etc/motd
etc/os-release
etc/profile.d/ylvaos-audio.sh
etc/profile.d/ylvaos-editor.sh
etc/profile.d/ylvaos-locale.sh
etc/profile.d/ylvaos-terminal.sh
etc/X11/Xwrapper.config
etc/X11/xorg.conf.d/10-ylvaos-input.conf
etc/ylvaos-release
sbin/ylva-getty
usr/bin/AppLauncher
usr/bin/applauncher
usr/bin/ApplicationLauncher
usr/bin/applicationlauncher
usr/bin/ConnectNetwork
usr/bin/connectnetwork
usr/bin/Desktop
usr/bin/desktop
usr/bin/Files
usr/bin/files
usr/bin/Editor
usr/bin/editor
usr/bin/Kernel
usr/bin/kernel
usr/bin/Launcher
usr/bin/launcher
usr/bin/PackageManager
usr/bin/packagemanager
usr/bin/RepairMode
usr/bin/repairmode
usr/bin/SnapshotManager
usr/bin/snapshotmanager
usr/bin/Settings
usr/bin/settings
usr/bin/SystemMonitor
usr/bin/systemmonitor
usr/bin/TextEditor
usr/bin/texteditor
usr/bin/Terminal
usr/bin/terminal
usr/bin/YlvaOS
usr/bin/ylva-audio-bridge
usr/bin/ylva-control
usr/bin/ylva-desktop-input-agent
usr/bin/ylva-host-agent
usr/bin/ylva-midi-bridge
usr/bin/ylva-splash
usr/bin/ylva-start-audio
usr/local/bin/regedit
usr/local/bin/regsvr32
usr/local/bin/wine
usr/local/bin/wineboot
usr/local/bin/winecfg
usr/local/bin/wineconsole
usr/local/bin/winefile
usr/local/bin/wineserver
usr/lib/ylvaos/managed-files
usr/lib/ylvaos/app-launcher
usr/lib/ylvaos/configure-wine-midi
usr/lib/ylvaos/package-helper
usr/lib/ylvaos/repair-mode
usr/lib/ylvaos/registry-helpers
usr/lib/ylvaos/snapshot-tui
usr/lib/ylvaos/settings-tui
usr/lib/ylvaos/setup-audio
usr/lib/ylvaos/setup-font
usr/lib/ylvaos/setup-wine
usr/lib/ylvaos/system-monitor-tui
usr/lib/ylvaos/text-editor
usr/lib/ylvaos/update-from-mod
usr/lib/ylvaos/wine-env
usr/local/bin/ylva-desktop-session
usr/sbin/ylva-start-desktop
usr/sbin/ylva-stop-desktop
EOF

cp /mnt/usr/lib/ylvaos/managed-files /tmp/ylvaos-managed-files
find /mnt/lib/modules -type f \( -path '*/kernel/sound/*' -o -name 'uinput.ko*' -o -name 'modules.alias*' -o -name 'modules.builtin*' -o -name 'modules.dep*' -o -name 'modules.devname' -o -name 'modules.order' -o -name 'modules.softdep' -o -name 'modules.symbols*' -o -name 'modules.weakdep' \) 2>/dev/null |
    sed 's#^/mnt/##' >>/tmp/ylvaos-managed-files
find /mnt/boot -maxdepth 1 -type f \( -name 'vmlinuz-*' -o -name 'initramfs-*' -o -name 'config-*' -o -name 'System.map-*' \) 2>/dev/null |
    sed 's#^/mnt/##' >>/tmp/ylvaos-managed-files
sort -u /tmp/ylvaos-managed-files >/tmp/ylvaos-managed-files.sorted
cp /tmp/ylvaos-managed-files.sorted /mnt/usr/lib/ylvaos/managed-files
tar -czf /tmp/ylvaos-rootfs-overlay.tar.gz -C /mnt -T /tmp/ylvaos-managed-files.sorted

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
                kernel_asset="$(find /mnt/boot -maxdepth 1 -type f \( -name 'vmlinuz-lts' -o -name 'vmlinuz-*-lts' \) 2>/dev/null | head -n 1)"
                initrd_asset="$(find /mnt/boot -maxdepth 1 -type f \( -name 'initramfs-lts' -o -name 'initramfs-*-lts' \) 2>/dev/null | head -n 1)"
                if [ -z "$kernel_asset" ]; then
                    kernel_asset="$(find /mnt/boot -maxdepth 1 -type f -name 'vmlinuz-*' 2>/dev/null | head -n 1)"
                fi
                if [ -z "$initrd_asset" ]; then
                    initrd_asset="$(find /mnt/boot -maxdepth 1 -type f -name 'initramfs-*' 2>/dev/null | head -n 1)"
                fi
                if [ -z "$kernel_asset" ] || [ -z "$initrd_asset" ]; then
                    echo __YLVA_BOOT_ASSET_EXPORT_FAILED__
                    umount "$export_mount" >/dev/null 2>&1 || true
                    return 1
                fi
                cp "$kernel_asset" "$export_mount/vmlinuz"
                cp "$initrd_asset" "$export_mount/initrd.img"
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
    # QEMU fixes vvfat capacity from the directory's initial contents. A sparse
    # reserve keeps large rootfs, kernel, and initrd exports from being truncated.
    with (export_dir / "YLVA_UPDATE_EXPORT_RESERVE.bin").open("wb") as reserve:
        reserve.truncate(256 * 1024 * 1024)
    return export_dir


def publish_update_payload(root: Path, export_dir: Path) -> None:
    update_dir = root / "Mod_YlvaOS" / "vm" / "update"
    assets_dir = root / "Mod_YlvaOS" / "vm" / "assets"
    update_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)
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

    boot_assets = {
        "vmlinuz": assets_dir / "vmlinuz",
        "initrd.img": assets_dir / "initrd.img",
    }
    for name, destination in boot_assets.items():
        source = export_dir / name
        if not source.exists():
            raise RuntimeError(f"YlvaOS boot asset was not exported: missing {source}")
        shutil.copyfile(source, destination)

    print(f"Wrote {update_dir}")
    print(f"Wrote {assets_dir}")


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
