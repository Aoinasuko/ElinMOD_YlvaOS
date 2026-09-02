# Source Access

This document records how to reproduce or retrieve source materials for the
third party components used by the bundled YlvaOS VM image.

## Rebuilding the generated VM image

From the repository root, run:

```powershell
python.exe .\Tools\build_ylvaos_image.py --force
```

The script downloads the Alpine virt ISO listed in
`YLVAOS_IMAGE_MANIFEST.json`, verifies its SHA-256, extracts the boot assets,
builds a 16 GiB preinstalled root disk, installs the package set recorded in
`alpine-installed-packages.txt` including the lightweight X desktop packages,
Wine, PulseAudio/ALSA audio support, FluidSynth, a GM soundfont, Noto CJK
fonts, and Japanese locale data, and writes:

- `Mod_YlvaOS/vm/assets/vmlinuz`
- `Mod_YlvaOS/vm/assets/initrd.img`
- `Mod_YlvaOS/vm/disk.qcow2.gz`
- `Mod_YlvaOS/vm/update/`
- `Mod_YlvaOS/LEGAL/YLVAOS_IMAGE_MANIFEST.json`
- `Mod_YlvaOS/LEGAL/alpine-installed-packages.txt`

## Alpine package source

The root disk is built from Alpine Linux repositories:

- Main: https://dl-cdn.alpinelinux.org/alpine/v3.24/main
- Community: https://dl-cdn.alpinelinux.org/alpine/v3.24/community

For package-specific source, license tags, patches, and build recipes, use the
Alpine aports repository:

```powershell
git clone https://gitlab.alpinelinux.org/alpine/aports.git
```

Then inspect the relevant `APKBUILD` and patch files for the package version
listed in `alpine-installed-packages.txt`. Alpine package pages can also be used
to navigate to source metadata:

https://pkgs.alpinelinux.org/packages?branch=v3.24

## Linux kernel source

The boot kernel currently distributed as `Mod_YlvaOS/vm/assets/vmlinuz` is
extracted from the Alpine Linux 3.24.1 virt ISO. The boot log reports:

```text
Linux version 6.18.35-0-virt
```

Use Alpine aports for Alpine's kernel configuration and patch set, and the
upstream Linux repository for the corresponding upstream kernel source:

- https://gitlab.alpinelinux.org/alpine/aports
- https://git.kernel.org/

## QEMU source

The bundled QEMU binary reports:

```text
QEMU emulator version 11.1.0 (v11.1.0-12130-ge470268ff4)
```

Source is available from the QEMU project:

```powershell
git clone https://gitlab.com/qemu-project/qemu.git
git checkout e470268ff4
```

If the bundled QEMU was obtained from a third-party Windows build, keep the
original download URL, build recipe, and exact source archive with your release
materials as required by the applicable licenses.

## Wine source

Wine source code is available from the Wine project:

- https://www.winehq.org/
- https://source.winehq.org/git/wine.git

Alpine's Wine package metadata, patches, and build recipe are available in the
Alpine aports repository. Use the package versions listed in
`alpine-installed-packages.txt` as the release reference.

## Audio stack source

The root disk includes PulseAudio, ALSA libraries/tools/plugins, FluidSynth, and
a GM soundfont package so Windows games running through Wine can emit PCM audio
and MIDI playback can be synthesized inside the guest.

Upstream source locations:

- PulseAudio: https://www.freedesktop.org/wiki/Software/PulseAudio/
- ALSA: https://alsa-project.org/
- FluidSynth: https://www.fluidsynth.org/

Package-specific Alpine metadata, license tags, and patches are available from
the Alpine aports repository:

- https://gitlab.alpinelinux.org/alpine/aports

## Font, locale, and soundfont source

The image includes Noto CJK fonts for Japanese text rendering and the Alpine
musl locale packages used by Wine Japanese locale setup, plus the Alpine GM
soundfont package used by FluidSynth. Use `alpine-installed-packages.txt` for
the exact installed package names and versions.

- Noto CJK upstream: https://github.com/notofonts/noto-cjk
- musl-locales upstream: https://git.adelielinux.org/adelie/musl-locales/
- Alpine package metadata: https://gitlab.alpinelinux.org/alpine/aports
