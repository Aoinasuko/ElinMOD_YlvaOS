#!/usr/bin/env python3
"""Boot YlvaOS with VNC/control enabled and verify the lightweight desktop path."""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import re
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


class HostInputServer:
    def __init__(self, token: str):
        self.token = token
        self.clients: list[socket.socket] = []
        self.ready_clients: set[socket.socket] = set()
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
        with self.lock:
            clients = list(self.clients)
            self.clients.clear()
            self.ready_clients.clear()
        for client in clients:
            try:
                client.close()
            except OSError:
                pass

    def send_paste(self, text: str) -> None:
        payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
        self.send_line(f"YLVAOS_HOST {self.token} paste-b64 {payload}")

    def send_pointer(self, x: int, y: int, previous_mask: int, button_mask: int) -> None:
        self.send_line(f"YLVAOS_HOST {self.token} pointer {x} {y} {previous_mask} {button_mask}")

    def send_line(self, line: str) -> None:
        data = (line + "\n").encode("ascii")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with self.lock:
                clients = list(self.ready_clients)
            for client in clients:
                try:
                    client.sendall(data)
                    return
                except OSError:
                    with self.lock:
                        if client in self.clients:
                            self.clients.remove(client)
                        self.ready_clients.discard(client)
            time.sleep(0.2)
        raise TimeoutError("Timed out waiting for the host input agent")

    def _accept_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            with self.lock:
                self.clients.append(client)
            threading.Thread(target=self._read_until_closed, args=(client,), daemon=True).start()

    def _read_until_closed(self, client: socket.socket) -> None:
        with client:
            pending = bytearray()
            while not self.stop_event.is_set():
                try:
                    data = client.recv(256)
                except OSError:
                    break
                if not data:
                    break
                pending.extend(data)
                while b"\n" in pending:
                    raw, _, rest = pending.partition(b"\n")
                    pending = bytearray(rest)
                    line = raw.rstrip(b"\r").decode("ascii", errors="ignore")
                    if line == f"YLVAOS_HOST {self.token} ready":
                        with self.lock:
                            self.ready_clients.add(client)
        with self.lock:
            if client in self.clients:
                self.clients.remove(client)
            self.ready_clients.discard(client)


class AudioServer:
    def __init__(self):
        self.byte_count = 0
        self.signal_samples = 0
        self.max_abs_sample = 0
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

    def reset_signal(self) -> None:
        with self.lock:
            self.signal_samples = 0
            self.max_abs_sample = 0

    def wait_for_signal(self, minimum_samples: int, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if self.signal_samples >= minimum_samples:
                    return
            time.sleep(0.2)
        with self.lock:
            seen = self.signal_samples
            peak = self.max_abs_sample
            byte_count = self.byte_count
        raise TimeoutError(f"Timed out waiting for {minimum_samples} non-silent audio samples; received {seen}, peak {peak}, bytes {byte_count}")

    def _accept_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._read_client, args=(client,), daemon=True).start()

    def _read_client(self, client: socket.socket) -> None:
        carry = b""
        with client:
            while not self.stop_event.is_set():
                try:
                    data = client.recv(8192)
                except OSError:
                    return
                if not data:
                    return
                payload = carry + data
                if len(payload) % 2:
                    carry = payload[-1:]
                    payload = payload[:-1]
                else:
                    carry = b""
                signal_samples = 0
                max_abs_sample = 0
                for (sample,) in struct.iter_unpack("<h", payload):
                    absolute = abs(sample)
                    if absolute > max_abs_sample:
                        max_abs_sample = absolute
                    if absolute > 256:
                        signal_samples += 1
                with self.lock:
                    self.byte_count += len(data)
                    self.signal_samples += signal_samples
                    if max_abs_sample > self.max_abs_sample:
                        self.max_abs_sample = max_abs_sample


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
        self.write_lock = threading.Lock()

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
        with self.write_lock:
            self.sock.sendall(struct.pack(">BBHI", 4, 1 if down else 0, 0, key_sym))

    def keypress(self, key_sym: int) -> None:
        self.send_key(key_sym, True)
        time.sleep(0.02)
        self.send_key(key_sym, False)
        time.sleep(0.02)

    def hotkey(self, key_syms: list[int]) -> None:
        for key_sym in key_syms:
            self.send_key(key_sym, True)
            time.sleep(0.02)
        for key_sym in reversed(key_syms):
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
        with self.write_lock:
            self.sock.sendall(struct.pack(">BBHH", 5, button_mask & 0xFF, x, y))

    def click(self, x: int, y: int) -> None:
        self.send_pointer(x, y, 0)
        time.sleep(0.10)
        self.send_pointer(x, y, 1)
        time.sleep(0.15)
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
        with self.write_lock:
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

    def click(self, x: int, y: int, width: int, height: int) -> None:
        absolute_x = round(max(0, min(width - 1, x)) * 0x7FFF / max(1, width - 1))
        absolute_y = round(max(0, min(height - 1, y)) * 0x7FFF / max(1, height - 1))
        self._execute(
            {
                "execute": "input-send-event",
                "arguments": {
                    "events": [
                        {"type": "abs", "data": {"axis": "x", "value": absolute_x}},
                        {"type": "abs", "data": {"axis": "y", "value": absolute_y}},
                    ]
                },
            }
        )
        time.sleep(0.10)
        self._execute(
            {
                "execute": "input-send-event",
                "arguments": {
                    "events": [{"type": "btn", "data": {"button": "left", "down": True}}]
                },
            }
        )
        time.sleep(0.15)
        self._execute(
            {
                "execute": "input-send-event",
                "arguments": {
                    "events": [{"type": "btn", "data": {"button": "left", "down": False}}]
                },
            }
        )

    def connect_user_network(self) -> None:
        self._execute_allow_duplicate(
            {
                "execute": "netdev_add",
                "arguments": {"type": "user", "id": "ylva_net"},
            }
        )

        self._execute_allow_duplicate(
            {
                "execute": "device_add",
                "arguments": {"driver": "virtio-net-pci", "netdev": "ylva_net", "id": "ylva_nic"},
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

    def _execute_allow_duplicate(self, command: dict) -> dict:
        try:
            return self._execute(command)
        except RuntimeError as exc:
            text = str(exc).lower()
            if "duplicate" in text or "already exists" in text:
                return {}
            raise

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
    time.sleep(0.5)
    start = console_length(console)
    console.send(command)
    wait_for_new(console, prompt, start, timeout)


def mount_import_command(inner: str) -> str:
    return (
        "mkdir -p ~/Import; "
        "import_mounted=0; "
        "for dev in /dev/vdb1 /dev/vdb /dev/vdc1 /dev/vdc /dev/vdd1 /dev/vdd /dev/sda1 /dev/sda /dev/sdb1 /dev/sdb /dev/sdc1 /dev/sdc; do "
        "[ -b \"$dev\" ] || continue; "
        "if doas mount -t vfat -o ro,uid=$(id -u),gid=$(id -g),utf8=1 \"$dev\" ~/Import >/tmp/ylva-import-mount.log 2>&1; then "
        "if [ -f ~/Import/ylva-import-test.txt ] || [ -d ~/Import/elona ]; then import_mounted=1; break; fi; "
        "doas umount ~/Import >/dev/null 2>&1 || true; "
        "fi; "
        "done; "
        "if [ \"$import_mounted\" -eq 1 ]; then "
        + inner
        + "; doas umount ~/Import; else cat /tmp/ylva-import-mount.log 2>/dev/null || true; printf '\\137\\137YLVA_IMPORT_NOT_FOUND\\137\\137\\n'; fi"
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


def prepare_disk(root: Path, copy_disk: bool, reuse_test_disk: bool) -> Path:
    destination = root / "_work" / "test-ylvaos-desktop.qcow2"
    if reuse_test_disk:
        if not destination.is_file():
            raise FileNotFoundError(f"Reusable test disk was not found: {destination}")
        return destination

    if not copy_disk:
        return root / "_work" / "ylvaos-image" / "disk.qcow2"

    source = root / "Mod_YlvaOS" / "vm" / "disk.qcow2.gz"
    if destination.exists():
        destination.unlink()
    with gzip.open(source, "rb") as src, destination.open("wb") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--copy-disk", action="store_true")
    parser.add_argument("--reuse-test-disk", action="store_true", help="Reuse the prior writable desktop test disk for diagnostics.")
    parser.add_argument("--quick-pointer", action="store_true", help="Skip online/audio checks and verify the desktop host-input click path.")
    parser.add_argument("--elona-dir", type=Path, help="Optional local Elona directory used for a short Wine launch smoke test.")
    args = parser.parse_args()

    root = repo_root()
    qemu = root / "Mod_YlvaOS" / "Tools" / "qemu" / "qemu-system-x86_64.exe"
    kernel = root / "Mod_YlvaOS" / "vm" / "assets" / "vmlinuz"
    initrd = root / "Mod_YlvaOS" / "vm" / "assets" / "initrd.img"
    disk = prepare_disk(root, args.copy_disk, args.reuse_test_disk)
    update_dir = root / "Mod_YlvaOS" / "vm" / "update"
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
    host_input = HostInputServer(token)
    host_input.start()
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
            "4096",
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
            "-drive",
            f"file=fat:ro:{update_dir.as_posix()},if=virtio,format=raw,media=disk,readonly=on",
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
            "-chardev",
            f"socket,id=ylva_hostinput,host=127.0.0.1,port={host_input.port},server=off,reconnect-ms=1000",
            "-device",
            "virtserialport,chardev=ylva_hostinput,name=org.ylvaos.hostinput",
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
        snapshot = console_snapshot(console)
        if "^           Ylva OS" not in snapshot or "(  * *)   by aoi_nasuko" not in snapshot or "Alpine Linux 3.24.1 base / YlvaOS 0.05" not in snapshot:
            raise RuntimeError("YlvaOS login splash was not printed")
        width, height = probe.read_framebuffer_size()
        print(f"[vnc] framebuffer {width}x{height}")
        if width <= 0 or height <= 0:
            raise RuntimeError("VNC framebuffer has an invalid size")
        if args.quick_pointer:
            run_command(console, "Desktop", "YlvaOS:~$", 90)
            control.wait_for("mode desktop-starting", 30)
            control.wait_for("mode desktop", 90)
            run_command(
                console,
                "DISPLAY=:0 Settings; sleep 2; "
                "DISPLAY=:0 xdotool search --name 'YlvaOS Settings' | head -n 1 >/tmp/ylva-settings-window; "
                "win=$(cat /tmp/ylva-settings-window); DISPLAY=:0 xdotool windowactivate \"$win\" windowfocus \"$win\"; sleep 1",
                "YlvaOS:~$",
                30,
            )
            run_command(
                console,
                "DISPLAY=:0 sh -c 'win=$(cat /tmp/ylva-settings-window); eval \"$(xdotool getwindowgeometry --shell \"$win\")\"; ext=$(xprop -id \"$win\" _NET_FRAME_EXTENTS 2>/dev/null | sed \"s/.*=//;s/,//g\"); set -- $ext; printf \"\\137\\137YLVA_SETTINGS_GEOM\\137\\137 %s %s %s %s %s %s %s %s\\n\" \"$X\" \"$Y\" \"$WIDTH\" \"$HEIGHT\" \"${1:-0}\" \"${2:-0}\" \"${3:-0}\" \"${4:-0}\"'",
                "YlvaOS:~$",
                30,
            )
            match = re.search(r"__YLVA_SETTINGS_GEOM__\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", console_snapshot(console))
            if match is None:
                raise RuntimeError("Could not read YlvaOS Settings geometry")
            settings_x, settings_y, settings_width, _settings_height, _left_ext, right_ext, top_ext, _bottom_ext = (int(value) for value in match.groups())
            settings_click_x = settings_x + settings_width + right_ext - 9
            settings_click_y = settings_y - top_ext + max(10, top_ext // 2 + 2)
            host_input.send_pointer(settings_click_x, settings_click_y, 0, 0)
            time.sleep(1)
            host_input.send_pointer(settings_click_x, settings_click_y, 0, 1)
            time.sleep(0.15)
            host_input.send_pointer(settings_click_x, settings_click_y, 1, 0)
            time.sleep(1)
            run_command(
                console,
                "DISPLAY=:0 sh -c 'if xdotool search --name \"YlvaOS Settings\" >/dev/null 2>&1; then status=1; else status=0; fi; printf \"\\137\\137YLVA_SETTINGS_STATUS_HOSTINPUT\\137\\137:%s\\n\" \"$status\"'",
                "YlvaOS:~$",
                30,
            )
            if "__YLVA_SETTINGS_STATUS_HOSTINPUT__:0" not in console_snapshot(console):
                for _ in range(2):
                    host_input.send_pointer(settings_click_x, settings_click_y, 0, 0)
                    time.sleep(0.5)
                    host_input.send_pointer(settings_click_x, settings_click_y, 0, 1)
                    time.sleep(0.15)
                    host_input.send_pointer(settings_click_x, settings_click_y, 1, 0)
                    time.sleep(1)
                    run_command(
                        console,
                        "DISPLAY=:0 sh -c 'if xdotool search --name \"YlvaOS Settings\" >/dev/null 2>&1; then status=1; else status=0; fi; printf \"\\137\\137YLVA_SETTINGS_STATUS_HOSTINPUT_RETRY\\137\\137:%s\\n\" \"$status\"'",
                        "YlvaOS:~$",
                        30,
                    )
                    if "__YLVA_SETTINGS_STATUS_HOSTINPUT_RETRY__:0" in console_snapshot(console):
                        break
                else:
                    raise RuntimeError("The host-input channel did not deliver the left click")
            run_command(
                console,
                "DISPLAY=:0 AppLauncher; sleep 3; "
                "DISPLAY=:0 xdotool search --name 'YlvaOS Application Launcher' | head -n 1 >/tmp/ylva-launcher-window; "
                "test -s /tmp/ylva-launcher-window && DISPLAY=:0 xdotool windowkill \"$(cat /tmp/ylva-launcher-window)\" >/dev/null 2>&1; "
                "test -s /tmp/ylva-launcher-window && printf '\\137\\137YLVA_APP_LAUNCHER_VISIBLE\\137\\137\\n'",
                "YlvaOS:~$",
                30,
            )
            if "__YLVA_APP_LAUNCHER_VISIBLE__" not in console_snapshot(console):
                raise RuntimeError("YlvaOS Application Launcher did not open on the desktop")
            run_command(
                console,
                "DISPLAY=:0 SystemMonitor; sleep 3; "
                "DISPLAY=:0 xdotool search --name 'YlvaOS System Monitor' | head -n 1 >/tmp/ylva-monitor-window; "
                "test -s /tmp/ylva-monitor-window && DISPLAY=:0 xdotool windowkill \"$(cat /tmp/ylva-monitor-window)\" >/dev/null 2>&1; "
                "test -s /tmp/ylva-monitor-window && printf '\\137\\137YLVA_MONITOR_VISIBLE\\137\\137\\n'",
                "YlvaOS:~$",
                30,
            )
            if "__YLVA_MONITOR_VISIBLE__" not in console_snapshot(console):
                raise RuntimeError("YlvaOS System Monitor did not open on the desktop")
            run_command(
                console,
                "DISPLAY=:0 PackageManager; sleep 3; "
                "DISPLAY=:0 xdotool search --name 'YlvaOS Package Manager' | head -n 1 >/tmp/ylva-package-window; "
                "test -s /tmp/ylva-package-window && DISPLAY=:0 xdotool windowkill \"$(cat /tmp/ylva-package-window)\" >/dev/null 2>&1; "
                "test -s /tmp/ylva-package-window && printf '\\137\\137YLVA_PACKAGE_MANAGER_VISIBLE\\137\\137\\n'",
                "YlvaOS:~$",
                30,
            )
            if "__YLVA_PACKAGE_MANAGER_VISIBLE__" not in console_snapshot(console):
                raise RuntimeError("YlvaOS Package Manager did not open on the desktop")
            run_command(
                console,
                "DISPLAY=:0 RepairMode; sleep 3; "
                "DISPLAY=:0 xdotool search --name 'YlvaOS Repair Mode' | head -n 1 >/tmp/ylva-repair-window; "
                "test -s /tmp/ylva-repair-window && DISPLAY=:0 xdotool windowkill \"$(cat /tmp/ylva-repair-window)\" >/dev/null 2>&1; "
                "test -s /tmp/ylva-repair-window && printf '\\137\\137YLVA_REPAIR_MODE_VISIBLE\\137\\137\\n'",
                "YlvaOS:~$",
                30,
            )
            if "__YLVA_REPAIR_MODE_VISIBLE__" not in console_snapshot(console):
                raise RuntimeError("YlvaOS Repair Mode did not open on the desktop")
            run_command(
                console,
                "mkdir -p ~/Documents; rm -f ~/Documents/ylva-editor-smoke.txt; "
                "DISPLAY=:0 TextEditor ~/Documents/ylva-editor-smoke.txt & sleep 4; "
                "DISPLAY=:0 xdotool search --name 'YlvaOS Text Editor' | tail -n 1 >/tmp/ylva-editor-window; "
                "test -s /tmp/ylva-editor-window && DISPLAY=:0 xdotool windowactivate \"$(cat /tmp/ylva-editor-window)\" windowfocus \"$(cat /tmp/ylva-editor-window)\"; "
                "test -s /tmp/ylva-editor-window && printf '\\137\\137YLVA_TEXT_EDITOR_VISIBLE\\137\\137\\n'",
                "YlvaOS:~$",
                45,
            )
            if "__YLVA_TEXT_EDITOR_VISIBLE__" not in console_snapshot(console):
                raise RuntimeError("YlvaOS Text Editor did not open on the desktop")
            run_command(
                console,
                "DISPLAY=:0 sh -c 'win=$(cat /tmp/ylva-editor-window); xdotool windowactivate --sync \"$win\" windowfocus \"$win\"'; "
                "printf '\\137\\137YLVA_TEXT_EDITOR_FOCUSED\\137\\137\\n'",
                "YlvaOS:~$",
                30,
            )
            host_input.send_paste("YlvaOS editor paste OK\n")
            time.sleep(4)
            run_command(
                console,
                "DISPLAY=:0 sh -c 'win=$(cat /tmp/ylva-editor-window); xdotool windowactivate --sync \"$win\" windowfocus \"$win\" key --clearmodifiers ctrl+o Return ctrl+x'; "
                "sleep 3; "
                "grep -q 'YlvaOS editor paste OK' ~/Documents/ylva-editor-smoke.txt && "
                "printf '\\137\\137YLVA_TEXT_EDITOR_SAVE_OK\\137\\137\\n'",
                "YlvaOS:~$",
                45,
            )
            if "__YLVA_TEXT_EDITOR_SAVE_OK__" not in console_snapshot(console):
                run_command(
                    console,
                    "printf '\\137\\137YLVA_TEXT_EDITOR_DIAG\\137\\137\\n'; "
                    "od -An -tx1 ~/Documents/ylva-editor-smoke.txt 2>/dev/null || true; "
                    "DISPLAY=:0 xdotool search --name 'YlvaOS Text Editor' 2>/dev/null || true; "
                    "printf '\\137\\137YLVA_TEXT_EDITOR_DIAG_END\\137\\137\\n'",
                    "YlvaOS:~$",
                    30,
                )
                raise RuntimeError("YlvaOS Text Editor did not save pasted text")
            run_command(
                console,
                "YlvaOS package status >/tmp/ylva-package-status 2>&1 && "
                "grep -q 'YlvaOS Package Manager Helper' /tmp/ylva-package-status && "
                "(YlvaOS package update >/tmp/ylva-package-offline 2>&1 || true) && "
                "grep -q 'Network is disabled' /tmp/ylva-package-offline && "
                "grep -q ConnectNetwork /tmp/ylva-package-offline && "
                "YlvaOS repair status >/tmp/ylva-repair-status 2>&1 && "
                "grep -q 'YlvaOS Repair Mode' /tmp/ylva-repair-status && "
                "printf '\\137\\137YLVA_PACKAGE_REPAIR_QUICK_CLI_OK\\137\\137\\n'",
                "YlvaOS:~$",
                45,
            )
            if "__YLVA_PACKAGE_REPAIR_QUICK_CLI_OK__" not in console_snapshot(console):
                raise RuntimeError("YlvaOS package or repair CLI did not report the expected quick-path state")

            network_errors: list[Exception] = []

            def enable_network_after_guest_request() -> None:
                try:
                    control.wait_for("network connect", 40)
                    qmp.connect_user_network()
                except Exception as exc:
                    network_errors.append(exc)

            network_thread = threading.Thread(target=enable_network_after_guest_request, daemon=True)
            network_thread.start()
            run_command(
                console,
                "printf 'yes\\n' | ConnectNetwork; ip route | grep -q '^default ' && printf '\\137\\137YLVA_QUICK_NETWORK_CONNECTED\\137\\137\\n'",
                "YlvaOS:~$",
                90,
            )
            network_thread.join(timeout=10)
            if network_errors:
                raise network_errors[0]
            if "__YLVA_QUICK_NETWORK_CONNECTED__" not in console_snapshot(console):
                raise RuntimeError("ConnectNetwork did not establish a default route in quick path")
            run_command(
                console,
                "YlvaOS package update >/tmp/ylva-package-update 2>&1 && "
                "YlvaOS package search htop >/tmp/ylva-package-search 2>&1 && "
                "grep -q '^htop-' /tmp/ylva-package-search && "
                "YlvaOS package install --yes htop >/tmp/ylva-package-install 2>&1 && "
                "apk info -e htop >/dev/null 2>&1 && "
                "YlvaOS package remove --yes htop >/tmp/ylva-package-remove 2>&1 && "
                "! apk info -e htop >/dev/null 2>&1 && "
                "grep -q 'apk add htop' ~/YlvaOS/logs/package.log && "
                "grep -q 'apk del htop' ~/YlvaOS/logs/package.log && "
                "printf '\\137\\137YLVA_PACKAGE_INSTALL_REMOVE_OK\\137\\137\\n'",
                "YlvaOS:~$",
                300,
            )
            if "__YLVA_PACKAGE_INSTALL_REMOVE_OK__" not in console_snapshot(console):
                run_command(
                    console,
                    "printf '\\137\\137YLVA_PACKAGE_DIAG\\137\\137\\n'; "
                    "for file in /tmp/ylva-package-update /tmp/ylva-package-search /tmp/ylva-package-install /tmp/ylva-package-remove ~/YlvaOS/logs/package.log; do "
                    "echo --- $file; tail -n 80 \"$file\" 2>/dev/null || true; "
                    "done; "
                    "printf '\\137\\137YLVA_PACKAGE_DIAG_END\\137\\137\\n'",
                    "YlvaOS:~$",
                    45,
                )
                raise RuntimeError("YlvaOS Package Manager did not search, install, remove, or log htop correctly")
            run_command(
                console,
                "YlvaOS monitor --once >/tmp/ylva-monitor-once; "
                "grep -q 'YlvaOS System Monitor' /tmp/ylva-monitor-once && "
                "grep -q 'Guest CPU:' /tmp/ylva-monitor-once && "
                "grep -q 'Guest memory:' /tmp/ylva-monitor-once && "
                "grep -q 'Root disk:' /tmp/ylva-monitor-once && "
                "grep -q 'Guest network:' /tmp/ylva-monitor-once && "
                "grep -q 'PID  PROCESS' /tmp/ylva-monitor-once && "
                "(sleep 60 & pid=$!; "
                "printf 'no\\n' | /usr/lib/ylvaos/system-monitor-tui --kill \"$pid\" >/tmp/ylva-kill-cancel 2>&1 || true; "
                "if ! kill -0 \"$pid\" 2>/dev/null; then exit 1; fi; "
                "printf 'yes\\n' | /usr/lib/ylvaos/system-monitor-tui --kill \"$pid\" >/tmp/ylva-kill-confirm 2>&1 || true; "
                "for i in 1 2 3 4 5; do kill -0 \"$pid\" 2>/dev/null || break; sleep 1; done; "
                "if kill -0 \"$pid\" 2>/dev/null; then exit 1; fi; "
                "/usr/lib/ylvaos/system-monitor-tui --kill 1 --yes >/tmp/ylva-kill-protected 2>&1 || true; "
                "grep -q 'Refusing to terminate protected process PID 1' /tmp/ylva-kill-protected && "
                "printf '\\137\\137YLVA_MONITOR_QUICK_OK\\137\\137\\n')",
                "YlvaOS:~$",
                90,
            )
            if "__YLVA_MONITOR_QUICK_OK__" not in console_snapshot(console):
                raise RuntimeError("YlvaOS System Monitor quick path did not report or control processes correctly")
            run_command(
                console,
                "mkdir -p ~/ClickTest/OpenMe; DISPLAY=:0 Files ~/ClickTest; sleep 3; "
                "win=$(DISPLAY=:0 xdotool search --class pcmanfm | tail -n 1); "
                "DISPLAY=:0 xdotool windowmove --sync \"$win\" 100 100 windowsize --sync \"$win\" 800 600 windowactivate \"$win\" windowfocus \"$win\"; sleep 2",
                "YlvaOS:~$",
                30,
            )
            folder_x = 303
            folder_y = 220
            for _ in range(2):
                host_input.send_pointer(folder_x, folder_y, 0, 1)
                time.sleep(0.12)
                host_input.send_pointer(folder_x, folder_y, 1, 0)
                time.sleep(0.12)
            time.sleep(2)
            run_command(
                console,
                "win=$(DISPLAY=:0 xdotool search --class pcmanfm | tail -n 1); "
                "title=$(DISPLAY=:0 xdotool getwindowname \"$win\"); "
                "printf '\\137\\137YLVA_FILE_MANAGER_TITLE\\137\\137:%s\\n' \"$title\"",
                "YlvaOS:~$",
                30,
            )
            if "__YLVA_FILE_MANAGER_TITLE__:OpenMe" not in console_snapshot(console):
                raise RuntimeError("The host-input channel did not deliver the file-manager double click")
            run_command(console, "poweroff", "Power down", 60)
            process.wait(timeout=60)
            return 0
        run_command(console, "command -v Desktop && command -v Kernel && command -v ConnectNetwork && command -v Settings && command -v Files && command -v AppLauncher && command -v TextEditor && command -v nano && command -v SnapshotManager && command -v SystemMonitor && command -v PackageManager && command -v RepairMode && command -v pcmanfm && command -v mc && command -v dialog && command -v ylva-splash && command -v ylva-host-agent && test -x /usr/lib/ylvaos/update-from-mod && test -x /usr/lib/ylvaos/app-launcher && test -x /usr/lib/ylvaos/text-editor && test -x /usr/lib/ylvaos/snapshot-tui && test -x /usr/lib/ylvaos/system-monitor-tui && test -x /usr/lib/ylvaos/package-helper && test -x /usr/lib/ylvaos/repair-mode", "YlvaOS:~$", 60)
        snapshot = console_snapshot(console)
        for path in ["/usr/bin/Desktop", "/usr/bin/Kernel", "/usr/bin/ConnectNetwork", "/usr/bin/Settings", "/usr/bin/Files", "/usr/bin/AppLauncher", "/usr/bin/TextEditor", "/usr/bin/nano", "/usr/bin/SnapshotManager", "/usr/bin/SystemMonitor", "/usr/bin/PackageManager", "/usr/bin/RepairMode", "/usr/bin/pcmanfm", "/usr/bin/mc", "/usr/bin/dialog", "/usr/bin/ylva-splash", "/usr/bin/ylva-host-agent", "/usr/lib/ylvaos/app-launcher", "/usr/lib/ylvaos/text-editor", "/usr/lib/ylvaos/snapshot-tui", "/usr/lib/ylvaos/system-monitor-tui", "/usr/lib/ylvaos/package-helper", "/usr/lib/ylvaos/repair-mode"]:
            if path not in snapshot:
                raise RuntimeError(f"{path} was not found in the guest")
        run_command(console, "YlvaOS update", "YlvaOS:~$", 40)
        if "YlvaOS is already up to date." not in console_snapshot(console):
            raise RuntimeError("YlvaOS update did not detect the bundled same-version payload")
        run_command(
            console,
            "AppLauncher --list >/tmp/ylva-launcher-list && "
            "YlvaOS launch --list >/tmp/ylva-launcher-alias-list && "
            "grep -q '1) Terminal' /tmp/ylva-launcher-list && "
            "grep -q '2) File Manager' /tmp/ylva-launcher-list && "
            "grep -q '3) Settings' /tmp/ylva-launcher-list && "
            "grep -q '4) Text Editor' /tmp/ylva-launcher-list && "
            "grep -q '5) System Monitor' /tmp/ylva-launcher-list && "
            "grep -q '1) Terminal' /tmp/ylva-launcher-alias-list && "
            "TextEditor status >/tmp/ylva-editor-status && "
            "YlvaOS edit status >/tmp/ylva-editor-alias-status && "
            "grep -q 'YlvaOS Text Editor' /tmp/ylva-editor-status && "
            "grep -q 'Backend: nano' /tmp/ylva-editor-status && "
            "grep -q 'YlvaOS Text Editor' /tmp/ylva-editor-alias-status && "
            "[ \"$EDITOR\" = TextEditor ] && [ \"$VISUAL\" = TextEditor ] && "
            "TextEditor check ~/Import/example.txt >/tmp/ylva-editor-check && "
            "grep -q 'Mode: read-only' /tmp/ylva-editor-check && "
            "printf '\\137\\137YLVA_LAUNCHER_EDITOR_OK\\137\\137\\n'",
            "YlvaOS:~$",
            45,
        )
        snapshot = console_snapshot(console)
        if "__YLVA_LAUNCHER_EDITOR_OK__" not in snapshot:
            raise RuntimeError("YlvaOS Application Launcher or Text Editor did not report the expected state")
        run_command(
            console,
            "YlvaOS monitor --once >/tmp/ylva-monitor-once; "
            "grep -q 'YlvaOS System Monitor' /tmp/ylva-monitor-once && "
            "grep -q 'Guest CPU:' /tmp/ylva-monitor-once && "
            "grep -q 'Guest memory:' /tmp/ylva-monitor-once && "
            "grep -q 'Root disk:' /tmp/ylva-monitor-once && "
            "grep -q 'Guest network:' /tmp/ylva-monitor-once && "
            "grep -q 'PID  PROCESS' /tmp/ylva-monitor-once && "
            "(sleep 60 & pid=$!; "
            "printf 'no\\n' | /usr/lib/ylvaos/system-monitor-tui --kill \"$pid\" >/tmp/ylva-kill-cancel 2>&1 || true; "
            "if ! kill -0 \"$pid\" 2>/dev/null; then exit 1; fi; "
            "printf 'yes\\n' | /usr/lib/ylvaos/system-monitor-tui --kill \"$pid\" >/tmp/ylva-kill-confirm 2>&1 || true; "
            "for i in 1 2 3 4 5; do kill -0 \"$pid\" 2>/dev/null || break; sleep 1; done; "
            "if kill -0 \"$pid\" 2>/dev/null; then exit 1; fi; "
            "/usr/lib/ylvaos/system-monitor-tui --kill 1 --yes >/tmp/ylva-kill-protected 2>&1 || true; "
            "grep -q 'Refusing to terminate protected process PID 1' /tmp/ylva-kill-protected && "
            "printf '\\137\\137YLVA_MONITOR_OK\\137\\137\\n')",
            "YlvaOS:~$",
            90,
        )
        snapshot = console_snapshot(console)
        if "__YLVA_MONITOR_OK__" not in snapshot:
            raise RuntimeError("YlvaOS System Monitor did not report or control processes correctly")
        run_command(
            console,
            "YlvaOS package status >/tmp/ylva-package-status 2>&1 && "
            "grep -q 'YlvaOS Package Manager Helper' /tmp/ylva-package-status && "
            "(YlvaOS package update >/tmp/ylva-package-offline 2>&1 || true) && "
            "grep -q 'Network is disabled' /tmp/ylva-package-offline && "
            "grep -q ConnectNetwork /tmp/ylva-package-offline && "
            "YlvaOS repair status >/tmp/ylva-repair-status 2>&1 && "
            "grep -q 'YlvaOS Repair Mode' /tmp/ylva-repair-status && "
            "printf '\\137\\137YLVA_PACKAGE_REPAIR_CLI_OK\\137\\137\\n'",
            "YlvaOS:~$",
            45,
        )
        snapshot = console_snapshot(console)
        if "__YLVA_PACKAGE_REPAIR_CLI_OK__" not in snapshot:
            raise RuntimeError("YlvaOS package or repair CLI did not report the expected state")
        run_command(
            console,
            "printf 'no\\n' | ConnectNetwork; printf '\\137\\137YLVA_NETWORK_CANCEL_DONE\\137\\137\\n'",
            "YlvaOS:~$",
            40,
        )
        snapshot = console_snapshot(console)
        if "ConnectNetwork cancelled." not in snapshot or "__YLVA_NETWORK_CANCEL_DONE__" not in snapshot:
            raise RuntimeError("ConnectNetwork cancellation path did not complete")

        network_errors: list[Exception] = []

        def enable_network_after_guest_request() -> None:
            try:
                control.wait_for("network connect", 40)
                qmp.connect_user_network()
            except Exception as exc:
                network_errors.append(exc)

        network_thread = threading.Thread(target=enable_network_after_guest_request, daemon=True)
        network_thread.start()
        run_command(
            console,
            "printf 'yes\\n' | ConnectNetwork; ip route | grep -q '^default ' && printf '\\137\\137YLVA_NETWORK_CONNECTED\\137\\137\\n'",
            "YlvaOS:~$",
            90,
        )
        network_thread.join(timeout=10)
        if network_errors:
            raise network_errors[0]
        snapshot = console_snapshot(console)
        if "YlvaOS networking is connected through QEMU user-mode NAT." not in snapshot or "__YLVA_NETWORK_CONNECTED__" not in snapshot:
            raise RuntimeError("ConnectNetwork did not establish a default route")
        run_command(
            console,
            "doas apk update >/tmp/ylva-apk-update.log 2>&1 && printf '\\137\\137YLVA_APK_UPDATE_OK\\137\\137\\n' || (tail -n 40 /tmp/ylva-apk-update.log; printf '\\137\\137YLVA_APK_UPDATE_FAILED\\137\\137\\n')",
            "YlvaOS:~$",
            180,
        )
        snapshot = console_snapshot(console)
        if "__YLVA_APK_UPDATE_OK__" not in snapshot or "__YLVA_APK_UPDATE_FAILED__" in snapshot:
            raise RuntimeError("apk update failed after ConnectNetwork")
        run_command(
            console,
            "for cmd in wine pactl pacat parec speaker-test YlvaOS Settings Files pcmanfm mc dialog; do command -v \"$cmd\"; done; "
            "for cmd in ylva-midi-bridge; do command -v \"$cmd\"; done; "
            "for path in /usr/lib/ylvaos/setup-audio /usr/lib/ylvaos/setup-font /usr/lib/ylvaos/setup-wine /usr/lib/ylvaos/configure-wine-midi /usr/lib/ylvaos/registry-helpers; do test -r \"$path\" && echo \"$path\"; done; "
            "! command -v ylva-audio-test >/dev/null 2>&1 && "
            "! command -v ylva-configure-wine >/dev/null 2>&1 && "
            "! command -v ylva-wine-init >/dev/null 2>&1 && "
            "! command -v Elona >/dev/null 2>&1 && "
            "! command -v elona >/dev/null 2>&1 && "
            "printf '\\137\\137YLVA_COMMAND_LAYOUT_OK\\137\\137\\n'",
            "YlvaOS:~$",
            60,
        )
        snapshot = console_snapshot(console)
        for path in [
            "/usr/local/bin/wine",
            "/usr/bin/pactl",
            "/usr/bin/pacat",
            "/usr/bin/parec",
            "/usr/bin/speaker-test",
            "/usr/bin/YlvaOS",
            "/usr/bin/Settings",
            "/usr/bin/Files",
            "/usr/bin/pcmanfm",
            "/usr/bin/mc",
            "/usr/bin/dialog",
            "/usr/bin/ylva-midi-bridge",
            "/usr/lib/ylvaos/setup-audio",
            "/usr/lib/ylvaos/setup-font",
            "/usr/lib/ylvaos/setup-wine",
            "/usr/lib/ylvaos/configure-wine-midi",
            "/usr/lib/ylvaos/registry-helpers",
        ]:
            if path not in snapshot:
                raise RuntimeError(f"{path} was not found in the guest")
        if "__YLVA_COMMAND_LAYOUT_OK__" not in snapshot:
            raise RuntimeError("YlvaOS command layout still exposes an old helper command")
        run_command(
            console,
            "wine --version; YlvaOS setup audio; pactl list short sinks | awk '{print $2}' | grep -qx ylva && pgrep -u $(id -u) fluidsynth >/dev/null && aplaymidi -l | grep -q 'FLUID Synth' && printf '\\137\\137YLVA_PULSE_SINK\\137\\137\\n'",
            "YlvaOS:~$",
            90,
        )
        snapshot = console_snapshot(console)
        if "wine-" not in snapshot or "__YLVA_PULSE_SINK__" not in snapshot:
            raise RuntimeError("Wine or the YlvaOS PulseAudio sink did not start")
        wine_setup_start = console_length(console)
        run_command(
            console,
            "YlvaOS setup wine >/tmp/ylva-wine-setup.log 2>&1; wine reg query 'HKLM\\System\\CurrentControlSet\\Control\\Nls\\CodePage' /v ACP; wine reg query 'HKCU\\Software\\Wine\\Drivers' /v Audio | grep -qi pulse && printf '\\137\\137YLVA_WINE_PULSE_OK\\137\\137\\n'; test -s ~/.wine/.ylvaos-midi-target && cat ~/.wine/.ylvaos-midi-target; fc-match 'MS Gothic'; printf '\\137\\137YLVA_WINE_CONFIGURED\\137\\137\\n'",
            "YlvaOS:~$",
            240,
        )
        snapshot = console_snapshot(console)[wine_setup_start:]
        if "__YLVA_WINE_CONFIGURED__" not in snapshot or "932" not in snapshot or "__YLVA_WINE_PULSE_OK__" not in snapshot or "#" not in snapshot or "Noto Sans CJK JP" not in snapshot:
            raise RuntimeError("Wine Japanese locale/font configuration did not complete")
        audio.reset_signal()
        run_command(
            console,
            "timeout 15 sh -c '. /usr/lib/ylvaos/wine-env; dd if=/dev/urandom bs=4096 count=128 2>/dev/null | pacat --playback --device=ylva --format=s16le --rate=44100 --channels=2' >/tmp/ylva-audio-test.log 2>&1; echo __YLVA_AUDIO_TEST_DONE__",
            "YlvaOS:~$",
            30,
        )
        audio.wait_for_signal(1024, 30)
        audio.reset_signal()
        run_command(
            console,
            "printf '%s' 'TVRoZAAAAAYAAAABAGBNVHJrAAAAFgD/UQMHoSAAwAAAkDx/YIA8AAD/LwA=' | base64 -d >/tmp/ylva-midi-test.mid; port=$(aplaymidi -l | awk '/FLUID Synth/ { print $1; exit }'); test -n \"$port\" && timeout 8 aplaymidi -p \"$port\" /tmp/ylva-midi-test.mid >/tmp/ylva-midi-test.log 2>&1; cat /tmp/ylva-midi-test.log; printf '\\137\\137YLVA_MIDI_TEST_DONE\\137\\137\\n'",
            "YlvaOS:~$",
            30,
        )
        audio.wait_for_signal(128, 30)
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
        run_command(console, "[ -S /tmp/.X11-unix/X0 ] && printf '\\137\\137YLVA_X_READY\\137\\137\\n' || (tail -n 80 /tmp/ylva-desktop.log; printf '\\137\\137YLVA_X_MISSING\\137\\137\\n')", "YlvaOS:~$", 60)
        snapshot = console_snapshot(console)
        if "__YLVA_X_READY__" not in snapshot:
            raise RuntimeError("Xorg did not create /tmp/.X11-unix/X0")
        run_command(
            console,
            "grep -q 'context name=\"Close\"' ~/.config/openbox/rc.xml && "
            "grep -q 'action name=\"Close\"' ~/.config/openbox/rc.xml && "
            "grep -q 'context name=\"Iconify\"' ~/.config/openbox/rc.xml && "
            "grep -q 'action name=\"Iconify\"' ~/.config/openbox/rc.xml && "
            "grep -q 'context name=\"Maximize\"' ~/.config/openbox/rc.xml && "
            "grep -q 'action name=\"ToggleMaximize\"' ~/.config/openbox/rc.xml && "
            "grep -q 'context name=\"Client\"' ~/.config/openbox/rc.xml && "
            "grep -q 'button=\"A-Left\"' ~/.config/openbox/rc.xml && "
            "grep -q 'C-A-space' ~/.config/openbox/rc.xml && "
            "grep -q 'AppLauncher' ~/.config/openbox/menu.xml && "
            "grep -q 'TextEditor' ~/.config/openbox/menu.xml && "
            "grep -q 'PackageManager' ~/.config/openbox/menu.xml && "
            "grep -q 'SystemMonitor' ~/.config/openbox/menu.xml && "
            "grep -q 'SnapshotManager' ~/.config/openbox/menu.xml && "
            "grep -q 'RepairMode' ~/.config/openbox/menu.xml && "
            "printf '\\137\\137YLVA_WINDOW_BUTTON_BINDS_OK\\137\\137\\n'",
            "YlvaOS:~$",
            30,
        )
        snapshot = console_snapshot(console)
        if "__YLVA_WINDOW_BUTTON_BINDS_OK__" not in snapshot:
            raise RuntimeError("Openbox window button mouse bindings were not generated")
        run_command(console, "DISPLAY=:0 xdotool search --name 'YlvaOS Terminal' | head -n 1 >/tmp/ylva-terminal-window; test -s /tmp/ylva-terminal-window && printf '\\137\\137YLVA_TERMINAL_VISIBLE\\137\\137\\n'", "YlvaOS:~$", 30)
        snapshot = console_snapshot(console)
        if "__YLVA_TERMINAL_VISIBLE__" not in snapshot:
            raise RuntimeError("Initial desktop terminal was not visible")
        run_command(console, "DISPLAY=:0 Settings; sleep 2; DISPLAY=:0 xdotool search --name 'YlvaOS Settings' | head -n 1 >/tmp/ylva-settings-window; test -s /tmp/ylva-settings-window && printf '\\137\\137YLVA_SETTINGS_VISIBLE\\137\\137\\n'", "YlvaOS:~$", 30)
        snapshot = console_snapshot(console)
        if "__YLVA_SETTINGS_VISIBLE__" not in snapshot:
            raise RuntimeError("YlvaOS Settings did not open on the desktop")
        run_command(
            console,
            "DISPLAY=:0 sh -c 'win=$(cat /tmp/ylva-settings-window); eval \"$(xdotool getwindowgeometry --shell \"$win\")\"; ext=$(xprop -id \"$win\" _NET_FRAME_EXTENTS 2>/dev/null | sed \"s/.*=//;s/,//g\"); set -- $ext; printf \"\\137\\137YLVA_SETTINGS_GEOM\\137\\137 %s %s %s %s %s %s %s %s\\n\" \"$X\" \"$Y\" \"$WIDTH\" \"$HEIGHT\" \"${1:-0}\" \"${2:-0}\" \"${3:-0}\" \"${4:-0}\"'",
            "YlvaOS:~$",
            30,
        )
        snapshot = console_snapshot(console)
        match = re.search(r"__YLVA_SETTINGS_GEOM__\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", snapshot)
        if match is None:
            raise RuntimeError("Could not read YlvaOS Settings geometry")
        settings_x, settings_y, settings_width, _settings_height, left_ext, right_ext, top_ext, _bottom_ext = (int(value) for value in match.groups())
        settings_click_x = settings_x + settings_width + right_ext - 9
        settings_click_y = settings_y - top_ext + max(10, top_ext // 2 + 2)
        probe.click(settings_click_x, settings_click_y)
        time.sleep(1)
        close_status_start = console_length(console)
        run_command(
            console,
            "DISPLAY=:0 sh -c 'if xdotool search --name \"YlvaOS Settings\" >/dev/null 2>&1; then status=1; else status=0; fi; printf \"\\137\\137YLVA_SETTINGS_STATUS\\137\\137:%s\\n\" \"$status\"'",
            "YlvaOS:~$",
            30,
        )
        snapshot = console_snapshot(console)[close_status_start:]
        if "__YLVA_SETTINGS_STATUS__:0" not in snapshot:
            print("[vnc] left click did not close Settings; trying the virtio host-input channel")
            host_input.send_pointer(settings_click_x, settings_click_y, 0, 1)
            time.sleep(0.15)
            host_input.send_pointer(settings_click_x, settings_click_y, 1, 0)
            time.sleep(1)
            close_status_start = console_length(console)
            run_command(
                console,
                "DISPLAY=:0 sh -c 'if xdotool search --name \"YlvaOS Settings\" >/dev/null 2>&1; then status=1; else status=0; fi; printf \"\\137\\137YLVA_SETTINGS_STATUS_HOSTINPUT\\137\\137:%s\\n\" \"$status\"'",
                "YlvaOS:~$",
                30,
            )
            snapshot = console_snapshot(console)[close_status_start:]
        if "__YLVA_SETTINGS_STATUS_HOSTINPUT__:0" not in snapshot and "__YLVA_SETTINGS_STATUS__:0" not in snapshot:
            print("[host-input] left click did not close Settings; trying QMP input-send-event fallback")
            qmp.click(settings_click_x, settings_click_y, 1024, 768)
            time.sleep(1)
            close_status_start = console_length(console)
            run_command(
                console,
                "DISPLAY=:0 sh -c 'if xdotool search --name \"YlvaOS Settings\" >/dev/null 2>&1; then status=1; else status=0; fi; printf \"\\137\\137YLVA_SETTINGS_STATUS_QMP\\137\\137:%s\\n\" \"$status\"'",
                "YlvaOS:~$",
                30,
            )
            snapshot = console_snapshot(console)[close_status_start:]
            if "__YLVA_SETTINGS_STATUS_QMP__:0" not in snapshot:
                run_command(
                    console,
                    "printf '\\137\\137YLVA_HOST_INPUT_LOG\\137\\137\\n'; "
                    "cat /tmp/ylva-host-agent.log 2>/dev/null || true; "
                    "DISPLAY=:0 xdotool getmouselocation 2>/dev/null || true",
                    "YlvaOS:~$",
                    30,
                )
                run_command(
                    console,
                    f"DISPLAY=:0 xdotool mousemove --sync {settings_click_x} {settings_click_y} click 1; sleep 1; "
                    "if xdotool search --name 'YlvaOS Settings' >/dev/null 2>&1; then status=1; else status=0; fi; "
                    "printf '\\137\\137YLVA_SETTINGS_STATUS_XDOTOOL\\137\\137:%s\\n' \"$status\"",
                    "YlvaOS:~$",
                    30,
                )
                snapshot = console_snapshot(console)
                if "__YLVA_SETTINGS_STATUS_XDOTOOL__:0" in snapshot:
                    raise RuntimeError("Guest xdotool closed Settings, but VNC, host-input, and QMP did not deliver the left click")
                raise RuntimeError("VNC, QMP, and guest xdotool could not close Settings at the calculated title-bar coordinate")
        run_command(console, "DISPLAY=:0 Files; sleep 3; pgrep -fa '[p]cmanfm|[m]c' >/tmp/ylva-files-processes; test -s /tmp/ylva-files-processes && cat /tmp/ylva-files-processes && printf '\\137\\137YLVA_FILES_OPENED\\137\\137\\n'", "YlvaOS:~$", 30)
        snapshot = console_snapshot(console)
        if "__YLVA_FILES_OPENED__" not in snapshot:
            raise RuntimeError("YlvaOS File Manager did not start")
        run_command(
            console,
            "YlvaOS set desktop 800x600; YlvaOS set fps 20; printf '\\137\\137YLVA_DISPLAY_SETTINGS_SENT\\137\\137\\n'",
            "YlvaOS:~$",
            30,
        )
        control.wait_for("set desktop 800 600", 10)
        control.wait_for("set fps 20", 10)
        snapshot = console_snapshot(console)
        if "__YLVA_DISPLAY_SETTINGS_SENT__" not in snapshot:
            raise RuntimeError("YlvaOS display settings command did not complete")
        run_command(
            console,
            "DISPLAY=:0 sh -c 'win=$(xdotool search --name \"YlvaOS Terminal\" | tail -n 1); xdotool windowactivate \"$win\" windowfocus \"$win\"'",
            "YlvaOS:~$",
            30,
        )
        paste_body = "~/Import /dev/vdb1 [test] {ok} | ; : \" ' \\\\ $ ` !\n"
        paste_command = "cat >/tmp/ylva-host-paste.txt <<'EOF'\n" + paste_body + "EOF\n"
        expected_paste_hash = hashlib.sha256(paste_body.encode("utf-8")).hexdigest()
        host_input.send_paste(paste_command)
        time.sleep(4)
        run_command(
            console,
            f"sha256sum /tmp/ylva-host-paste.txt | grep -q '^{expected_paste_hash} ' && printf '\\137\\137YLVA_HOST_PASTE_OK\\137\\137\\n' || (od -An -tx1 /tmp/ylva-host-paste.txt 2>/dev/null; printf '\\137\\137YLVA_HOST_PASTE_BAD\\137\\137\\n')",
            "YlvaOS:~$",
            30,
        )
        snapshot = console_snapshot(console)
        if "__YLVA_HOST_PASTE_OK__" not in snapshot or "__YLVA_HOST_PASTE_BAD__" in snapshot:
            raise RuntimeError("Host clipboard paste was not injected exactly into the desktop terminal")
        run_command(console, "DISPLAY=:0 xdotool search --name 'YlvaOS Terminal' windowkill >/dev/null 2>&1 || true; sleep 1; printf '\\137\\137YLVA_TERMINAL_CLOSED\\137\\137\\n'", "YlvaOS:~$", 30)
        probe.hotkey([0xFFE3, 0xFFE9, ord("t")])
        time.sleep(2)
        run_command(console, "DISPLAY=:0 xdotool search --name 'YlvaOS Terminal' | head -n 1 >/tmp/ylva-terminal-window; test -s /tmp/ylva-terminal-window && printf '\\137\\137YLVA_TERMINAL_REOPENED\\137\\137\\n'", "YlvaOS:~$", 30)
        snapshot = console_snapshot(console)
        if "__YLVA_TERMINAL_REOPENED__" not in snapshot:
            raise RuntimeError("Ctrl+Alt+T did not reopen the desktop terminal")
        if args.elona_dir is not None:
            directmusic_start = console_length(console)
            run_command(
                console,
                "DISPLAY=:0 YLVAOS_INSTALL_DIRECTMUSIC=yes YlvaOS setup wine directmusic >/tmp/ylva-directmusic-setup.log 2>&1; status=$?; if [ \"$status\" -eq 0 ] && [ -f ~/.wine/.ylvaos-directmusic-runtime-v2 ]; then printf '\\137\\137YLVA_DIRECTMUSIC_READY\\137\\137\\n'; else tail -n 120 /tmp/ylva-directmusic-setup.log /tmp/ylva-winetricks-directmusic.log /tmp/ylva-winetricks-apk.log /tmp/ylva-winetricks-download.log 2>/dev/null; printf '\\137\\137YLVA_DIRECTMUSIC_FAILED:%s\\137\\137\\n' \"$status\"; fi",
                "YlvaOS:~$",
                900,
            )
            snapshot = console_snapshot(console)[directmusic_start:]
            if "__YLVA_DIRECTMUSIC_READY__" not in snapshot or "__YLVA_DIRECTMUSIC_FAILED:" in snapshot:
                raise RuntimeError("The DirectMusic runtime could not be installed for the Elona audio test")
            audio.reset_signal()
            run_command(
                console,
                "DISPLAY=:0 sh -c '. /usr/lib/ylvaos/wine-env; cd ~/ElonaTest && cp original/config.txt config.txt && sed -i \"s/^language.*/language.\\t\\\"0\\\"/\" config.txt && WINEDEBUG=+loaddll,+dmusic,+dmime,+dmsynth,+dmband,+midi,+wave,+dsound wine elona.exe >/tmp/ylva-elona.log 2>&1 &' ; sleep 20; pgrep -fa '[e]lona.exe' >/tmp/ylva-elona-processes.txt; if [ -s /tmp/ylva-elona-processes.txt ]; then cat /tmp/ylva-elona-processes.txt; printf '\\137\\137YLVA_ELONA_STARTED\\137\\137\\n'; else tail -n 80 /tmp/ylva-elona.log; printf '\\137\\137YLVA_ELONA_NOT_RUNNING\\137\\137\\n'; fi",
                "YlvaOS:~$",
                90,
            )
            snapshot = console_snapshot(console)
            if "__YLVA_ELONA_STARTED__" not in snapshot:
                raise RuntimeError("Elona did not remain running under Wine long enough for the smoke test")
            try:
                audio.wait_for_signal(64, 30)
                run_command(
                    console,
                    "grep -Eqi 'Loaded .*\\\\(dmband|dmcompos|dmime|dmloader|dmstyle|dmsynth|dmusic|dswave)\\.dll.*native' /tmp/ylva-elona.log && printf '\\137\\137YLVA_ELONA_DIRECTMUSIC_LOADED\\137\\137\\n'",
                    "YlvaOS:~$",
                    30,
                )
                if "__YLVA_ELONA_DIRECTMUSIC_LOADED__" not in console_snapshot(console):
                    raise RuntimeError("Elona produced audio without loading the installed native DirectMusic runtime")
            except TimeoutError:
                run_command(
                    console,
                    "echo __YLVA_ELONA_AUDIO_DIAG__; pactl list short sink-inputs 2>/dev/null || true; aconnect -l 2>/dev/null || true; wine reg query 'HKCU\\Software\\Wine\\DllOverrides' 2>/dev/null || true; for dll in dmband dmcompos dmime dmloader dmscript dmstyle dmsynth dmusic dmusic32 dsound dswave; do ls -l \"$HOME/.wine/drive_c/windows/system32/$dll.dll\" 2>/dev/null || true; done; grep -Eai 'dmband|dmcompos|dmime|dmloader|dmscript|dmstyle|dmsynth|dmusic|dsound|dswave|midi|pulse|audio|err:' /tmp/ylva-elona.log 2>/dev/null | tail -n 240 || true; tail -n 80 /tmp/ylva-midi-bridge.log /tmp/ylva-fluidsynth.log /tmp/ylva-pulseaudio.log 2>/dev/null || true; echo __YLVA_ELONA_AUDIO_DIAG_END__",
                    "YlvaOS:~$",
                    60,
                )
                raise
            finally:
                run_command(console, "wineserver -k >/dev/null 2>&1 || true", "YlvaOS:~$", 30)
        try:
            probe.click(80, 80)
            probe.send_text("kernel\n")
            control.wait_for("mode kernel", 12)
        except TimeoutError:
            qmp.click(80, 80, 1024, 768)
            qmp.send_text("kernel\n")
            try:
                control.wait_for("mode kernel", 30)
            except TimeoutError:
                print_input_diagnostics(console)
                raise
        run_command(
            console,
            "for i in 1 2 3 4 5; do [ ! -S /tmp/.X11-unix/X0 ] && printf '\\137\\137YLVA_X_STOPPED\\137\\137\\n' && break; sleep 1; done",
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
        host_input.close()
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
