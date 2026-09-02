# Source Availability and Rebuild Notes

This file records how to obtain corresponding source material for third-party
software bundled with YlvaOS.

## Rebuilding the YlvaOS VM Image

The boot assets and preinstalled root disk can be regenerated with:

```powershell
python.exe .\Tools\build_ylvaos_image.py --force
```

The script downloads the official Alpine Linux virt ISO, verifies its SHA-256,
extracts `boot/vmlinuz-virt` and `boot/initramfs-virt`, boots the ISO under the
bundled QEMU, installs Alpine packages into a 16 GiB ext4 qcow2 root disk,
including the lightweight X desktop, Wine, PulseAudio/ALSA audio support,
FluidSynth, a GM soundfont, CJK fonts, and Japanese locale data, and compresses the result to
`Mod_YlvaOS/vm/disk.qcow2.gz`.

## QEMU Source

QEMU source code is available from:

- https://gitlab.com/qemu-project/qemu
- https://www.qemu.org/download/

The bundled QEMU executable reports:

```text
QEMU emulator version 11.1.0 (v11.1.0-12130-ge470268ff4)
```

The QEMU license files bundled in this MOD package are:

- `Tools/qemu/COPYING`
- `Tools/qemu/COPYING.LIB`
- `LICENSES/GPL-2.0.txt`
- `LICENSES/LGPL-2.1.txt`

## Alpine Linux and Package Source

The YlvaOS root disk is built from Alpine Linux packages for branch `v3.24`,
architecture `x86_64`.

Package repositories used by the build script:

- https://dl-cdn.alpinelinux.org/alpine/v3.24/main
- https://dl-cdn.alpinelinux.org/alpine/v3.24/community

Alpine packaging source:

- https://gitlab.alpinelinux.org/alpine/aports

Package lookup:

- https://pkgs.alpinelinux.org/packages

Exact installed packages for this image are listed in:

- `LEGAL/alpine-installed-packages.txt`

The generated image provenance and file hashes are listed in:

- `LEGAL/YLVAOS_IMAGE_MANIFEST.json`

## Linux Kernel Source

The Linux kernel source is available from:

- https://git.kernel.org/
- https://www.kernel.org/

The Alpine `linux-virt` package is built from Alpine's kernel packaging in the
aports repository:

- https://gitlab.alpinelinux.org/alpine/aports

## Vim Source

Vim source code is available from:

- https://github.com/vim/vim

Vim's Alpine package metadata can be located through:

- https://pkgs.alpinelinux.org/packages?name=vim&branch=v3.24&arch=x86_64

## Wine Source

Wine source code is available from:

- https://www.winehq.org/
- https://source.winehq.org/git/wine.git

Wine's Alpine package metadata can be located through:

- https://pkgs.alpinelinux.org/packages?name=wine&branch=v3.24&arch=x86_64

## Audio Stack Source

The bundled root disk includes PulseAudio, ALSA components, FluidSynth, and a
GM soundfont package for Wine audio output and MIDI synthesis.

Upstream source locations:

- PulseAudio: https://www.freedesktop.org/wiki/Software/PulseAudio/
- ALSA: https://alsa-project.org/
- FluidSynth: https://www.fluidsynth.org/

Package-specific metadata and patches are available through Alpine aports and
the package list recorded in `LEGAL/alpine-installed-packages.txt`.

## Font and Soundfont Source

The image includes CJK fonts, Japanese locale data, and a GM soundfont package. Use
`LEGAL/alpine-installed-packages.txt` for exact package names and versions.

- Noto CJK upstream: https://github.com/notofonts/noto-cjk
- musl-locales upstream: https://git.adelielinux.org/adelie/musl-locales/
- Alpine packaging source: https://gitlab.alpinelinux.org/alpine/aports

## Distributor Reminder

If you redistribute this MOD package, confirm the source-code obligations for
GPL/LGPL components in the exact binary package you ship. Depending on your
distribution channel, you may need to provide complete corresponding source
alongside the binary package or provide a valid written offer.
