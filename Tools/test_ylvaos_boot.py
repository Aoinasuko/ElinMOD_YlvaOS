#!/usr/bin/env python3
"""Boot the generated YlvaOS disk through the same serial-console path as the MOD."""

from __future__ import annotations

import argparse
import base64
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


class Console:
    def __init__(self, process: subprocess.Popen[bytes]):
        self.process = process
        self.text = ""
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.cursor_reports = 0
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        while True:
            data = self.process.stdout.read(1)
            if not data:
                break
            chunk = data.decode("utf-8", errors="replace")
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
            respond_to_cursor_report = False
            with self.lock:
                self.text += chunk
                if len(self.text) > 1_000_000:
                    self.text = self.text[-500_000:]
                count = self.text.count("\x1b[6n")
                if count > self.cursor_reports:
                    self.cursor_reports = count
                    respond_to_cursor_report = True

            if respond_to_cursor_report:
                self.send_raw("\x1b[32;1R")

    def send_raw(self, text: str) -> None:
        assert self.process.stdin is not None
        with self.write_lock:
            self.process.stdin.write(text.encode("utf-8"))
            self.process.stdin.flush()

    def send(self, line: str) -> None:
        for ch in line:
            self.send_raw(ch)
            time.sleep(0.02)
        self.send_raw("\r\n")

    def wait_for_any(self, needles: list[str], timeout: int) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                for needle in needles:
                    if needle in self.text:
                        return needle
            if self.process.poll() is not None:
                raise RuntimeError("QEMU exited early")
            time.sleep(0.2)
        raise TimeoutError("Timed out waiting for " + ", ".join(needles))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--disk", type=Path, default=repo_root() / "_work" / "ylvaos-image" / "disk.qcow2")
    parser.add_argument("--copy-disk", action="store_true", help="boot a temporary copy so the source disk stays pristine")
    parser.add_argument(
        "--simulate-update-notice-version",
        help="expose a minimal newer MOD payload and verify the startup update notice",
    )
    args = parser.parse_args()

    root = repo_root()
    disk = args.disk
    if args.copy_disk:
        temporary = root / "_work" / "ylvaos-boot-test" / "disk.qcow2"
        temporary.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(disk, temporary)
        disk = temporary

    qemu = root / "Mod_YlvaOS" / "Tools" / "qemu" / "qemu-system-x86_64.exe"
    kernel = root / "Mod_YlvaOS" / "vm" / "assets" / "vmlinuz"
    initrd = root / "Mod_YlvaOS" / "vm" / "assets" / "initrd.img"
    update_dir = root / "Mod_YlvaOS" / "vm" / "update"
    if args.simulate_update_notice_version:
        update_dir = root / "_work" / "ylvaos-update-notice-test"
        if update_dir.exists():
            shutil.rmtree(update_dir)
        update_dir.mkdir(parents=True)
        (update_dir / "YLVAOS_UPDATE_SOURCE").write_bytes(b"YlvaOS update source\n")
        (update_dir / "ylvaos-release").write_bytes(
            f'YLVAOS_VERSION="{args.simulate_update_notice_version}"\nALPINE_VERSION="3.24.1"\n'.encode("ascii")
        )
        (update_dir / "rootfs-overlay.tar.gz").write_bytes(b"test payload")
        (update_dir / "update.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
    test_user = "aoi_nasuko"
    test_password = "ylva"
    password_b64 = base64.b64encode(test_password.encode("utf-8")).decode("ascii")
    append = (
        "console=ttyS0 root=/dev/vda rootfstype=ext4 rw "
        "modules=virtio_pci,virtio_blk,ext4 hostname=YlvaOS "
        f"ylva_user={test_user} ylva_password_b64={password_b64} ylva_rows=32 ylva_cols=140"
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
            "-serial",
            "stdio",
            "-monitor",
            "none",
            "-no-reboot",
            "-net",
            "none",
            "-drive",
            f"file={disk},if=virtio,format=qcow2",
            "-drive",
            f"file=fat:ro:{update_dir.as_posix()},if=virtio,format=raw,media=disk,readonly=on",
            "-kernel",
            str(kernel),
            "-initrd",
            str(initrd),
            "-append",
            append,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )

    console = Console(process)
    try:
        matched = console.wait_for_any(["YlvaOS:~$", "can't access tty", "Kernel panic"], 180)
        if matched != "YlvaOS:~$":
            return 1
        with console.lock:
            snapshot = console.text
        if "^           Ylva OS" not in snapshot or "(  * *)   by aoi_nasuko" not in snapshot or "Alpine Linux 3.24.1 base / YlvaOS 0.04" not in snapshot:
            raise RuntimeError("YlvaOS login splash was not printed")
        if args.simulate_update_notice_version:
            expected = f"Bundled:   Alpine Linux 3.24.1 base / YlvaOS {args.simulate_update_notice_version}"
            if (
                "A YlvaOS update is available from the installed MOD." not in snapshot
                or expected not in snapshot
                or 'Run "YlvaOS update" to install it. YlvaOS will restart automatically.' not in snapshot
            ):
                raise RuntimeError("YlvaOS startup update notice was not printed")
        elif "A YlvaOS update is available from the installed MOD." in snapshot:
            raise RuntimeError("YlvaOS startup update notice was printed for the current version")
        time.sleep(1)
        console.send('echo __YLVA_USER__$(whoami)__END__')
        console.wait_for_any([f"__YLVA_USER__{test_user}__END__"], 30)
        console.send("YlvaOS set memory 3072")
        console.wait_for_any(["YlvaOS memory target set to 3072 MiB"], 30)
        if not args.simulate_update_notice_version:
            console.send("YlvaOS update")
            console.wait_for_any(["YlvaOS is already up to date."], 30)
            console.send("command -v SystemMonitor && command -v SnapshotManager && command -v PackageManager && command -v RepairMode && test -x /usr/lib/ylvaos/system-monitor-tui && test -x /usr/lib/ylvaos/snapshot-tui && test -x /usr/lib/ylvaos/package-helper && test -x /usr/lib/ylvaos/repair-mode && YlvaOS monitor --once | grep -q 'YlvaOS System Monitor' && printf '\\137\\137YLVA_MONITOR_ONCE_OK\\137\\137\\n'")
            console.wait_for_any(["__YLVA_MONITOR_ONCE_OK__"], 45)
            console.send("YlvaOS package status >/tmp/ylva-package-status 2>&1 && grep -q 'YlvaOS Package Manager Helper' /tmp/ylva-package-status && YlvaOS repair status >/tmp/ylva-repair-status 2>&1 && grep -q 'YlvaOS Repair Mode' /tmp/ylva-repair-status && printf '\\137\\137YLVA_PACKAGE_REPAIR_STATUS_OK\\137\\137\\n'")
            console.wait_for_any(["__YLVA_PACKAGE_REPAIR_STATUS_OK__"], 45)
            console.send("(YlvaOS package update >/tmp/ylva-package-update-offline 2>&1 || true) && (YlvaOS package install --yes htop >/tmp/ylva-package-install-offline 2>&1 || true) && grep -q 'Network is disabled' /tmp/ylva-package-update-offline && grep -q ConnectNetwork /tmp/ylva-package-update-offline && grep -q 'Network is disabled' /tmp/ylva-package-install-offline && grep -q ConnectNetwork /tmp/ylva-package-install-offline && printf '\\137\\137YLVA_PACKAGE_OFFLINE_OK\\137\\137\\n'")
            console.wait_for_any(["__YLVA_PACKAGE_OFFLINE_OK__"], 45)
            console.send("mkdir -p ~/.config/openbox ~/.config/tint2; echo broken >~/.config/openbox/menu.xml; echo broken >~/.Xresources; YlvaOS repair user-config --yes >/tmp/ylva-repair-user 2>&1 && test -f ~/.profile && test -d ~/.config/openbox && test -d ~/.config/tint2 && grep -q 'Backup written' /tmp/ylva-repair-user && YlvaOS repair packages --check >/tmp/ylva-repair-packages 2>&1 && grep -q 'Checking package state' /tmp/ylva-repair-packages && YlvaOS repair safe-console --yes >/tmp/ylva-repair-safe 2>&1 && test -f ~/.ylvaos-safe-console && (Desktop >/tmp/ylva-desktop-refused 2>&1 || true) && grep -q 'safe-console is enabled' /tmp/ylva-desktop-refused && YlvaOS repair desktop --yes >/tmp/ylva-repair-desktop 2>&1 && test ! -f ~/.ylvaos-safe-console && grep -q 'Desktop configuration was reset' /tmp/ylva-repair-desktop && printf '\\137\\137YLVA_REPAIR_COMMANDS_OK\\137\\137\\n'")
            console.wait_for_any(["__YLVA_REPAIR_COMMANDS_OK__"], 90)
        console.send("vim --version | head -n 1")
        console.wait_for_any(["VIM - Vi IMproved"], 30)
        try:
            console.send("poweroff")
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
