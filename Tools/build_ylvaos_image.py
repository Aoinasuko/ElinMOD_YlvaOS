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
import base64
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
            "dbus",
            "dbus-x11",
            "eudev",
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
            "xdotool",
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
cat >/mnt/etc/os-release <<'EOF'
NAME="YlvaOS"
ID=ylvaos
ID_LIKE=alpine
VERSION_ID="{ALPINE_VERSION}"
PRETTY_NAME="YlvaOS 0.1.0 (Alpine Linux {ALPINE_VERSION} base)"
HOME_URL="https://ylva.local/"
SUPPORT_URL="https://alpinelinux.org/"
EOF

cat >/mnt/etc/issue <<'EOF'
YlvaOS 0.1.0 (Alpine Linux {ALPINE_VERSION} base) \\n \\l

EOF

cat >/mnt/etc/motd <<'EOF'
Welcome to YlvaOS.

This is a real Alpine Linux based userspace running inside the Elin MOD QEMU sandbox.
Runtime networking is disabled by default from the MOD side.
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
    for port in /dev/virtio-ports/org.ylvaos.control /dev/virtio-ports/org.ylvaos.audio; do
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

if command -v ylva-audio-bridge >/dev/null 2>&1 && ! pgrep -f '/usr/bin/ylva-audio-bridge' >/dev/null 2>&1; then
    ylva-audio-bridge >/tmp/ylva-audio-bridge.log 2>&1 &
fi

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
export TERM=vt100
stty rows $rows cols $cols -ixon 2>/dev/null || true
export XDG_RUNTIME_DIR="/tmp/ylva-runtime-$user"
mkdir -p "/tmp/ylva-runtime-$user" 2>/dev/null || true
chmod 700 "/tmp/ylva-runtime-$user" 2>/dev/null || true
ylva-start-audio >/tmp/ylva-audio.log 2>&1 || true
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
export TERM=vt100
stty rows 32 cols 140 -ixon 2>/dev/null || true
EOF

cat >/mnt/etc/profile.d/ylvaos-audio.sh <<'EOF'
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

cat >/mnt/etc/asound.conf <<'EOF'
pcm.!default {
    type pulse
}

ctl.!default {
    type pulse
}
EOF

cat >/mnt/usr/bin/ylva-audio-bridge <<'EOF'
#!/bin/sh
set -u
export PATH=/sbin:/bin:/usr/sbin:/usr/bin

pipe=/tmp/ylva-audio.pcm
port=/dev/virtio-ports/org.ylvaos.audio

prepare_pipe() {
    if [ ! -p "$pipe" ]; then
        rm -f "$pipe"
        mkfifo "$pipe"
    fi
    chmod 0666 "$pipe" >/dev/null 2>&1 || true
}

prepare_pipe
while :; do
    if [ ! -e "$port" ]; then
        sleep 1
        continue
    fi

    chmod 0666 "$port" >/dev/null 2>&1 || true
    prepare_pipe
    cat "$pipe" >"$port" 2>/dev/null || sleep 1
done
EOF
chmod 0755 /mnt/usr/bin/ylva-audio-bridge

cat >/mnt/usr/bin/ylva-start-audio <<'EOF'
#!/bin/sh
set -u
export PATH=/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

user="$(id -un 2>/dev/null || printf ylva)"
runtime="${XDG_RUNTIME_DIR:-/tmp/ylva-runtime-$user}"
pipe=/tmp/ylva-audio.pcm
export XDG_RUNTIME_DIR="$runtime"

mkdir -p "$runtime"
chmod 700 "$runtime" 2>/dev/null || true
if [ ! -p "$pipe" ]; then
    rm -f "$pipe"
    mkfifo "$pipe"
fi
chmod 0666 "$pipe" >/dev/null 2>&1 || true

if ! pgrep -u "$(id -u)" pulseaudio >/dev/null 2>&1; then
    pulseaudio --start --exit-idle-time=-1 --log-target=file:/tmp/ylva-pulseaudio.log >/dev/null 2>&1 || true
fi

for _ in 1 2 3 4 5 6 7 8 9 10; do
    if pactl info >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

if ! pactl list short sinks 2>/dev/null | awk '{print $2}' | grep -qx ylva; then
    pactl load-module module-pipe-sink sink_name=ylva file="$pipe" format=s16le rate=44100 channels=2 >/tmp/ylva-pipe-sink.id 2>/dev/null || true
fi

pactl set-default-sink ylva >/dev/null 2>&1 || true

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
if command -v ylva-audio-bridge >/dev/null 2>&1 && ! pgrep -f '/usr/bin/ylva-audio-bridge' >/dev/null 2>&1; then
    ylva-audio-bridge >/tmp/ylva-audio-bridge.log 2>&1 &
fi
EOF
chmod 0755 /mnt/etc/local.d/ylva-audio.start

cat >/mnt/usr/bin/ylva-audio-test <<'EOF'
#!/bin/sh
set -u
ylva-start-audio >/tmp/ylva-audio.log 2>&1 || true
exec speaker-test -D default -c 2 -r 44100 -F S16_LE -t sine -f 440 -l 1
EOF
chmod 0755 /mnt/usr/bin/ylva-audio-test

cat >/mnt/usr/bin/ylva-wine-init <<'EOF'
#!/bin/sh
set -u
export WINEPREFIX="${WINEPREFIX:-$HOME/.wine}"
ylva-start-audio >/tmp/ylva-audio.log 2>&1 || true
wineboot -u
wine reg add 'HKCU\Software\Wine\Drivers' /v Audio /d pulse /f >/dev/null 2>&1 || true
echo "Wine prefix is ready at $WINEPREFIX."
EOF
chmod 0755 /mnt/usr/bin/ylva-wine-init

cat >/mnt/usr/bin/Elona <<'EOF'
#!/bin/sh
set -u
export WINEPREFIX="${WINEPREFIX:-$HOME/.wine}"
game_dir="${1:-$HOME/Elona}"
exe="$game_dir/elona.exe"

if [ ! -f "$exe" ]; then
    echo "Elona was not found at $exe."
    echo "Copy it to ~/Elona, or run: Elona /path/to/elona"
    exit 2
fi

ylva-start-audio >/tmp/ylva-audio.log 2>&1 || true
cd "$game_dir"
exec wine elona.exe
EOF
chmod 0755 /mnt/usr/bin/Elona
ln -sf /usr/bin/Elona /mnt/usr/bin/elona

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
mkdir -p "$XDG_RUNTIME_DIR" "$home/.config/openbox" "$home/.config/tint2"
chmod 700 "$XDG_RUNTIME_DIR" 2>/dev/null || true
ylva-start-audio >/tmp/ylva-audio.log 2>&1 || true

cat >"$home/.Xresources" <<'EOF_XRES'
XTerm*faceName: DejaVu Sans Mono
XTerm*faceSize: 11
XTerm*background: #07110f
XTerm*foreground: #d4f8dc
XTerm*cursorColor: #d4f8dc
XTerm*scrollBar: false
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
</openbox_config>
EOF_OBRC

cat >"$home/.config/openbox/autostart" <<'EOF_AUTOSTART'
xsetroot -solid '#12211f' &
tint2 &
xterm -title "YlvaOS Terminal" -geometry 100x28+36+56 &
(sleep 1; win="$(xdotool search --name 'YlvaOS Terminal' | head -n 1)"; [ -n "$win" ] && xdotool windowactivate "$win" windowfocus "$win") >/dev/null 2>&1 &
EOF_AUTOSTART
chmod 0755 "$home/.config/openbox/autostart" 2>/dev/null || true

xrandr -s "${width}x${height}" 2>/dev/null || true
xsetroot -solid '#12211f' 2>/dev/null || true
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
    echo "usage: YlvaOS set memory <MiB> | YlvaOS set disk <MiB> | YlvaOS status"
}

is_positive_int() {
    case "$1" in
        ''|*[!0-9]* ) return 1 ;;
    esac

    [ "$1" -gt 0 ] 2>/dev/null
}

case "$1" in
    help|'')
        usage
        ;;
    status)
        echo "YlvaOS $(uname -r)"
        grep '^MemTotal:' /proc/meminfo
        df -h /
        ;;
    set)
        case "$2" in
            memory|mem)
                if ! is_positive_int "$3"; then
                    echo "memory must be a positive MiB value"
                    exit 2
                fi
                ylva-control "set memory $3"
                echo "YlvaOS memory target set to $3 MiB. Reboot YlvaOS to apply."
                ;;
            disk)
                if ! is_positive_int "$3"; then
                    echo "disk must be a positive MiB value"
                    exit 2
                fi
                ylva-control "set disk $3"
                echo "YlvaOS disk target set to $3 MiB. Reboot YlvaOS to apply."
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
        .replace("{packages}", packages)
    )


def upload_script(console: QemuConsole, script: str, prompt: str) -> None:
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    console.run_interactive_command("rm -f /tmp/ylvaos-install.sh /tmp/ylvaos-install.sh.b64", prompt)
    for index in range(0, len(encoded), 160):
        chunk = encoded[index : index + 160]
        console.run_interactive_command(
            f"printf '%s' '{chunk}' >>/tmp/ylvaos-install.sh.b64",
            prompt,
        )
    console.run_interactive_command("base64 -d /tmp/ylvaos-install.sh.b64 >/tmp/ylvaos-install.sh", prompt)


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
        upload_script(console, install_script(), "__YLVA_PROMPT__#")
        console.send_chars("sh /tmp/ylvaos-install.sh\n", char_delay=0.003)
        console.wait_for("__YLVA_INSTALL_DONE__", INSTALL_TIMEOUT_SECONDS)
        if console.process is not None:
            console.process.wait(timeout=120)
    finally:
        console.terminate()

    log = console.snapshot()
    if "__YLVA_INSTALL_FAILED__:" in log and "__YLVA_INSTALL_DONE__" not in log:
        raise RuntimeError("YlvaOS root disk install failed; see console output above.")

    legal_dir = root / "Mod_YlvaOS" / "LEGAL"
    legal_dir.mkdir(parents=True, exist_ok=True)
    packages = extract_between(log, "__YLVA_PACKAGES_BEGIN__", "__YLVA_PACKAGES_END__")
    if packages:
        (legal_dir / "alpine-installed-packages.txt").write_text(packages.strip() + "\n", encoding="utf-8")

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
