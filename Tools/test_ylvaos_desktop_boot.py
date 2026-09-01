#!/usr/bin/env python3
"""Boot YlvaOS with VNC/control enabled and verify the lightweight desktop path."""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from test_ylvaos_boot import Console, repo_root


def find_vnc_display() -> int:
    for display in range(20, 100):
        port = 5900 + display
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
            return display
        except OSError:
            continue
        finally:
            probe.close()
    raise RuntimeError("no free VNC display was found")


def find_free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


class ControlServer:
    def __init__(self, token: str):
        self.token = token
        self.messages: list[str] = []
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop_event.set()
        try:
            self.server.close()
        except OSError:
            pass

    def wait_for(self, message: str, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if message in self.messages:
                    return
            time.sleep(0.2)
        with self.lock:
            seen = ", ".join(self.messages)
        raise TimeoutError(f"Timed out waiting for control message {message!r}; seen: {seen}")

    def _accept_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._read_client, args=(client,), daemon=True).start()

    def _read_client(self, client: socket.socket) -> None:
        with client:
            text = ""
            while not self.stop_event.is_set():
                try:
                    data = client.recv(256)
                except OSError:
                    return
                if not data:
                    return
                text += data.decode("ascii", errors="ignore")
                while "\n" in text:
                    line, text = text.split("\n", 1)
                    line = line.strip()
                    prefix = f"YLVAOS {self.token} "
                    if line.startswith(prefix):
                        message = line[len(prefix) :]
                        print(f"[control] {message}")
                        with self.lock:
                            self.messages.append(message)


class AudioServer:
    def __init__(self):
        self.byte_count = 0
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop_event.set()
        try:
            self.server.close()
        except OSError:
            pass

    def wait_for_bytes(self, minimum: int, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if self.byte_count >= minimum:
                    return
            time.sleep(0.2)
        with self.lock:
            seen = self.byte_count
        raise TimeoutError(f"Timed out waiting for {minimum} audio bytes; received {seen}")

    def _accept_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._read_client, args=(client,), daemon=True).start()

    def _read_client(self, client: socket.socket) -> None:
        with client:
            while not self.stop_event.is_set():
                try:
                    data = client.recv(8192)
                except OSError:
                    return
                if not data:
                    return
                with self.lock:
                    self.byte_count += len(data)


class VncProbe:
    ENCODING_RAW = 0
    ENCODING_COPY_RECT = 1
    ENCODING_DESKTOP_SIZE = -223

    def __init__(self, port: int):
        self.port = port
        self.sock: socket.socket | None = None
        self.width = 0
        self.height = 0
        self.stop_event = threading.Event()
        self.reader: threading.Thread | None = None

    def close(self) -> None:
        self.stop_event.set()
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def read_framebuffer_size(self) -> tuple[int, int]:
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.sock = sock
        protocol = self._read_exact(12)
        sock.sendall(protocol)
        count = self._read_exact(1)[0]
        if count == 0:
            reason_len = struct.unpack(">I", self._read_exact(4))[0]
            reason = self._read_exact(reason_len).decode("utf-8", errors="replace")
            raise RuntimeError(f"VNC rejected security: {reason}")
        security_types = self._read_exact(count)
        if 1 not in security_types:
            raise RuntimeError(f"VNC no-auth security is unavailable: {security_types!r}")
        sock.sendall(b"\x01")
        result = struct.unpack(">I", self._read_exact(4))[0]
        if result != 0:
            raise RuntimeError(f"VNC security result was {result}")
        sock.sendall(b"\x01")
        width, height = struct.unpack(">HH", self._read_exact(4))
        self._read_exact(16)
        name_len = struct.unpack(">I", self._read_exact(4))[0]
        if name_len:
            self._read_exact(name_len)
        self.width = width
        self.height = height
        self._set_pixel_format()
        self._set_encodings()
        self._request_framebuffer(False)
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()
        return width, height

    def send_key(self, key_sym: int, down: bool) -> None:
        assert self.sock is not None
        self.sock.sendall(struct.pack(">BBHI", 4, 1 if down else 0, 0, key_sym))

    def keypress(self, key_sym: int) -> None:
        self.send_key(key_sym, True)
        time.sleep(0.02)
        self.send_key(key_sym, False)
        time.sleep(0.02)

    def send_text(self, text: str) -> None:
        for ch in text:
            if ch in "\r\n":
                self.keypress(0xFF0D)
            elif ch == "\t":
                self.keypress(0xFF09)
            elif 0x20 <= ord(ch) <= 0x7E:
                self.keypress(ord(ch))

    def send_pointer(self, x: int, y: int, button_mask: int = 0) -> None:
        assert self.sock is not None
        x = max(0, min(65535, x))
        y = max(0, min(65535, y))
        self.sock.sendall(struct.pack(">BBHH", 5, button_mask & 0xFF, x, y))

    def click(self, x: int, y: int) -> None:
        self.send_pointer(x, y, 0)
        time.sleep(0.05)
        self.send_pointer(x, y, 1)
        time.sleep(0.05)
        self.send_pointer(x, y, 0)

    def _read_exact(self, size: int) -> bytes:
        assert self.sock is not None
        chunks = bytearray()
        while len(chunks) < size:
            data = self.sock.recv(size - len(chunks))
            if not data:
                raise RuntimeError("VNC connection closed")
            chunks.extend(data)
        return bytes(chunks)

    def _set_pixel_format(self) -> None:
        assert self.sock is not None
        self.sock.sendall(
            bytes(
                [
                    0,
                    0,
                    0,
                    0,
                    32,
                    24,
                    0,
                    1,
                    0,
                    255,
                    0,
                    255,
                    0,
                    255,
                    16,
                    8,
                    0,
                    0,
                    0,
                    0,
                ]
            )
        )

    def _set_encodings(self) -> None:
        assert self.sock is not None
        self.sock.sendall(
            struct.pack(
                ">BBHiii",
                2,
                0,
                3,
                self.ENCODING_RAW,
                self.ENCODING_COPY_RECT,
                self.ENCODING_DESKTOP_SIZE,
            )
        )

    def _request_framebuffer(self, incremental: bool) -> None:
        assert self.sock is not None
        width = max(1, self.width)
        height = max(1, self.height)
        self.sock.sendall(struct.pack(">BBHHHH", 3, 1 if incremental else 0, 0, 0, width, height))

    def _read_loop(self) -> None:
        try:
            while not self.stop_event.is_set():
                message_type = self._read_exact(1)[0]
                if message_type == 0:
                    self._read_framebuffer_update()
                    self._request_framebuffer(True)
                elif message_type == 2:
                    continue
                elif message_type == 3:
                    self._read_exact(3)
                    length = struct.unpack(">I", self._read_exact(4))[0]
                    if length:
                        self._read_exact(length)
                else:
                    return
        except OSError:
            return
        except RuntimeError:
            return

    def _read_framebuffer_update(self) -> None:
        self._read_exact(1)
        rectangles = struct.unpack(">H", self._read_exact(2))[0]
        for _ in range(rectangles):
            x, y, width, height, encoding = struct.unpack(">HHHHi", self._read_exact(12))
            if encoding == self.ENCODING_RAW:
                self._read_exact(width * height * 4)
            elif encoding == self.ENCODING_COPY_RECT:
                self._read_exact(4)
            elif encoding == self.ENCODING_DESKTOP_SIZE:
                self.width = width
                self.height = height
            else:
                return


class QmpInput:
    QCODE_BY_CHAR = {ch: ch for ch in "abcdefghijklmnopqrstuvwxyz0123456789"}
    QCODE_BY_CHAR.update({"-": "minus", "=": "equal", " ": "spc", "/": "slash", ".": "dot"})

    def __init__(self, port: int):
        self.port = port
        self.sock: socket.socket | None = None
        self.buffer = b""

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def connect(self) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=2)
                break
            except OSError:
                time.sleep(0.1)
        if self.sock is None:
            raise RuntimeError("QMP connection failed")
        self._read_json()
        self._execute({"execute": "qmp_capabilities"})

    def keypress(self, qcode: str) -> None:
        self._execute(
            {
                "execute": "send-key",
                "arguments": {"keys": [{"type": "qcode", "data": qcode}]},
            }
        )
        time.sleep(0.05)

    def send_text(self, text: str) -> None:
        for ch in text:
            if ch in "\r\n":
                self.keypress("ret")
                continue
            qcode = self.QCODE_BY_CHAR.get(ch.lower())
            if qcode is not None:
                self.keypress(qcode)

    def click(self, x: int, y: int) -> None:
        self._execute(
            {
                "execute": "input-send-event",
                "arguments": {
                    "events": [
                        {"type": "abs", "data": {"axis": "x", "value": x}},
                        {"type": "abs", "data": {"axis": "y", "value": y}},
                        {"type": "btn", "data": {"button": "left", "down": True}},
                        {"type": "btn", "data": {"button": "left", "down": False}},
                    ]
                },
            }
        )

    def _send_key(self, qcode: str, down: bool) -> None:
        self._execute(
            {
                "execute": "input-send-event",
                "arguments": {
                    "events": [
                        {
                            "type": "key",
                            "data": {"key": {"type": "qcode", "data": qcode}, "down": down},
                        }
                    ]
                },
            }
        )

    def _execute(self, command: dict) -> dict:
        assert self.sock is not None
        self.sock.sendall((json.dumps(command) + "\r\n").encode("utf-8"))
        while True:
            response = self._read_json()
            if "event" in response:
                continue
            if "error" in response:
                raise RuntimeError(f"QMP command failed: {response['error']}")
            return response

    def _read_json(self) -> dict:
        assert self.sock is not None
        while b"\n" not in self.buffer:
            data = self.sock.recv(4096)
            if not data:
                raise RuntimeError("QMP connection closed")
            self.buffer += data
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line.strip())


def console_length(console: Console) -> int:
    with console.lock:
        return len(console.text)


def console_snapshot(console: Console) -> str:
    with console.lock:
        return console.text


def wait_for_new(console: Console, needle: str, start_index: int, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with console.lock:
            if console.text.find(needle, start_index) >= 0:
                return
        if console.process.poll() is not None:
            raise RuntimeError(f"QEMU exited before seeing new {needle!r}")
        time.sleep(0.2)
    raise TimeoutError(f"Timed out waiting for new {needle!r}")


def run_command(console: Console, command: str, prompt: str, timeout: int = 60) -> None:
    start = console_length(console)
    time.sleep(0.5)
    console.send(command)
    wait_for_new(console, prompt, start, timeout)


def mount_import_command(inner: str) -> str:
    return (
        "mkdir -p ~/Import; "
        "dev=/dev/vdb1; "
        "[ -b \"$dev\" ] || dev=/dev/vdb; "
        "[ -b \"$dev\" ] || dev=/dev/sda1; "
        "[ -b \"$dev\" ] || dev=/dev/sda; "
        "doas mount -t vfat -o ro,uid=$(id -u),gid=$(id -g) \"$dev\" ~/Import; "
        + inner
        + "; doas umount ~/Import"
    )


def print_input_diagnostics(console: Console) -> None:
    command = (
        "echo __YLVA_INPUT_DIAG_BEGIN__; "
        "ls -l /dev/input /dev/input/by-id 2>/dev/null || true; "
        "echo __YLVA_XORG_INPUT_DIAG__; "
        "grep -Eai 'config/udev|libinput|QEMU|keyboard|mouse|pointer|input|No input' "
        "/var/log/Xorg.0.log /home/*/.local/share/xorg/Xorg.0.log 2>/dev/null | tail -n 160 || true; "
        "echo __YLVA_INPUT_DIAG_END__"
    )
    run_command(console, command, "YlvaOS:~$", 60)


def prepare_disk(root: Path, copy_disk: bool) -> Path:
    if not copy_disk:
        return root / "_work" / "ylvaos-image" / "disk.qcow2"

    source = root / "Mod_YlvaOS" / "vm" / "disk.qcow2.gz"
    destination = root / "_work" / "test-ylvaos-desktop.qcow2"
    if destination.exists():
        destination.unlink()
    with gzip.open(source, "rb") as src, destination.open("wb") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--copy-disk", action="store_true")
    parser.add_argument("--elona-dir", type=Path, help="Optional local Elona directory used for a short Wine launch smoke test.")
    args = parser.parse_args()

    root = repo_root()
    qemu = root / "Mod_YlvaOS" / "Tools" / "qemu" / "qemu-system-x86_64.exe"
    kernel = root / "Mod_YlvaOS" / "vm" / "assets" / "vmlinuz"
    initrd = root / "Mod_YlvaOS" / "vm" / "assets" / "initrd.img"
    disk = prepare_disk(root, args.copy_disk)
    import_dir = root / "_work" / "ylvaos-import-test"
    if import_dir.exists():
        shutil.rmtree(import_dir)
    import_dir.mkdir(parents=True, exist_ok=True)
    (import_dir / "ylva-import-test.txt").write_text("YLVA_IMPORT_OK\n", encoding="utf-8")
    if args.elona_dir is not None:
        elona_dir = args.elona_dir.resolve()
        if not (elona_dir / "elona.exe").is_file():
            raise FileNotFoundError(f"elona.exe was not found in {elona_dir}")
        shutil.copytree(elona_dir, import_dir / "elona")
    test_user = "aoi_nasuko"
    test_password = "ylva"
    token = "desktoptesttoken"
    control = ControlServer(token)
    control.start()
    audio = AudioServer()
    audio.start()
    display = find_vnc_display()
    vnc_port = 5900 + display
    qmp_port = find_free_port()
    password_b64 = base64.b64encode(test_password.encode("utf-8")).decode("ascii")
    append = (
        "console=ttyS0 root=/dev/vda rootfstype=ext4 rw "
        "modules=virtio_pci,virtio_blk,ext4 hostname=YlvaOS "
        f"ylva_user={test_user} ylva_password_b64={password_b64} "
        f"ylva_rows=32 ylva_cols=140 ylva_control_token={token} "
        "ylva_desktop_width=1024 ylva_desktop_height=768"
    )

    process = subprocess.Popen(
        [
            str(qemu),
            "-m",
            "2048",
            "-machine",
            "accel=tcg",
            "-cpu",
            "max",
            "-smp",
            "2",
            "-display",
            "none",
            "-vga",
            "std",
            "-vnc",
            f"127.0.0.1:{display}",
            "-k",
            "en-us",
            "-usb",
            "-device",
            "usb-kbd",
            "-device",
            "usb-tablet",
            "-serial",
            "stdio",
            "-monitor",
            "none",
            "-qmp",
            f"tcp:127.0.0.1:{qmp_port},server=on,wait=off",
            "-no-reboot",
            "-net",
            "none",
            "-drive",
            f"file={disk},if=virtio,format=qcow2",
            "-drive",
            f"file=fat:ro:{import_dir.as_posix()},if=virtio,format=raw,media=disk,readonly=on",
            "-device",
            "virtio-serial-pci",
            "-chardev",
            f"socket,id=ylva_ctl,host=127.0.0.1,port={control.port},server=off,reconnect-ms=1000",
            "-device",
            "virtserialport,chardev=ylva_ctl,name=org.ylvaos.control",
            "-chardev",
            f"socket,id=ylva_audio,host=127.0.0.1,port={audio.port},server=off,reconnect-ms=1000",
            "-device",
            "virtserialport,chardev=ylva_audio,name=org.ylvaos.audio",
            "-kernel",
            str(kernel),
            "-initrd",
            str(initrd),
            "-append",
            append,
        ],
        cwd=str(root / "_work"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )

    console = Console(process)
    probe = VncProbe(vnc_port)
    qmp = QmpInput(qmp_port)
    try:
        qmp.connect()
        console.wait_for_any(["YlvaOS:~$"], 180)
        width, height = probe.read_framebuffer_size()
        print(f"[vnc] framebuffer {width}x{height}")
        if width <= 0 or height <= 0:
            raise RuntimeError("VNC framebuffer has an invalid size")
        run_command(console, "command -v Desktop && command -v Kernel", "YlvaOS:~$", 60)
        snapshot = console_snapshot(console)
        if "/usr/bin/Desktop" not in snapshot or "/usr/bin/Kernel" not in snapshot:
            raise RuntimeError("Desktop or Kernel command was not found in the guest")
        run_command(
            console,
            "command -v wine && command -v pactl && command -v speaker-test && command -v ylva-audio-test && command -v Elona",
            "YlvaOS:~$",
            60,
        )
        snapshot = console_snapshot(console)
        for path in ["/usr/bin/wine", "/usr/bin/pactl", "/usr/bin/speaker-test", "/usr/bin/ylva-audio-test", "/usr/bin/Elona"]:
            if path not in snapshot:
                raise RuntimeError(f"{path} was not found in the guest")
        run_command(
            console,
            "wine --version; ylva-start-audio >/tmp/ylva-audio.log 2>&1; pactl list short sinks | awk '{print $2}' | grep -qx ylva && echo __YLVA_PULSE_SINK__",
            "YlvaOS:~$",
            90,
        )
        snapshot = console_snapshot(console)
        if "wine-" not in snapshot or "__YLVA_PULSE_SINK__" not in snapshot:
            raise RuntimeError("Wine or the YlvaOS PulseAudio sink did not start")
        run_command(
            console,
            "timeout 15 ylva-audio-test >/tmp/ylva-audio-test.log 2>&1; echo __YLVA_AUDIO_TEST_DONE__",
            "YlvaOS:~$",
            30,
        )
        audio.wait_for_bytes(4096, 30)
        run_command(
            console,
            mount_import_command("cat ~/Import/ylva-import-test.txt"),
            "YlvaOS:~$",
            60,
        )
        snapshot = console_snapshot(console)
        if "YLVA_IMPORT_OK" not in snapshot:
            raise RuntimeError("read-only import drive was not visible inside the guest")
        if args.elona_dir is not None:
            run_command(
                console,
                mount_import_command("rm -rf ~/ElonaTest; cp -r ~/Import/elona ~/ElonaTest; test -f ~/ElonaTest/elona.exe && echo __YLVA_ELONA_COPIED__"),
                "YlvaOS:~$",
                180,
            )
            snapshot = console_snapshot(console)
            if "__YLVA_ELONA_COPIED__" not in snapshot:
                raise RuntimeError("Elona test files were not copied into the guest")
        run_command(console, "Desktop", "YlvaOS:~$", 90)
        control.wait_for("mode desktop-starting", 30)
        control.wait_for("mode desktop", 90)
        run_command(console, "[ -S /tmp/.X11-unix/X0 ] && echo __YLVA_X_READY__ || (tail -n 80 /tmp/ylva-desktop.log; echo __YLVA_X_MISSING__)", "YlvaOS:~$", 60)
        snapshot = console_snapshot(console)
        if "__YLVA_X_READY__" not in snapshot:
            raise RuntimeError("Xorg did not create /tmp/.X11-unix/X0")
        if args.elona_dir is not None:
            run_command(
                console,
                "DISPLAY=:0 sh -c 'cd ~/ElonaTest && Elona ~/ElonaTest >/tmp/ylva-elona.log 2>&1 &' ; sleep 12; pgrep -fa 'elona.exe|[w]ine' >/tmp/ylva-elona-processes.txt; if [ -s /tmp/ylva-elona-processes.txt ]; then cat /tmp/ylva-elona-processes.txt; echo __YLVA_ELONA_STARTED__; else tail -n 80 /tmp/ylva-elona.log; echo __YLVA_ELONA_NOT_RUNNING__; fi; wineserver -k >/dev/null 2>&1 || true",
                "YlvaOS:~$",
                60,
            )
            snapshot = console_snapshot(console)
            if "__YLVA_ELONA_STARTED__" not in snapshot:
                raise RuntimeError("Elona did not remain running under Wine long enough for the smoke test")
        try:
            probe.click(80, 80)
            probe.send_text("kernel\n")
            control.wait_for("mode kernel", 12)
        except TimeoutError:
            qmp.click(80, 80)
            qmp.send_text("kernel\n")
            try:
                control.wait_for("mode kernel", 30)
            except TimeoutError:
                print_input_diagnostics(console)
                raise
        run_command(
            console,
            "for i in 1 2 3 4 5; do [ ! -S /tmp/.X11-unix/X0 ] && echo __YLVA_X_STOPPED__ && break; sleep 1; done",
            "YlvaOS:~$",
            30,
        )
        snapshot = console_snapshot(console)
        if "__YLVA_X_STOPPED__" not in snapshot:
            raise RuntimeError("Kernel command reached the host, but Xorg did not stop")
        run_command(console, "poweroff", "Power down", 60)
        process.wait(timeout=60)
        return 0
    finally:
        probe.close()
        qmp.close()
        control.close()
        audio.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
