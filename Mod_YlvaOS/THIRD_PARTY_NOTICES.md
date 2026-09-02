# Third-Party Notices for YlvaOS

YlvaOS bundles and provisions a real Linux VM for use inside the Elin MOD
runtime. The VM is branded as YlvaOS, but the current userspace image is based
on Alpine Linux.

## QEMU

- Component: QEMU for Windows
- Bundled path: `Tools/qemu/`
- Detected version: `11.1.0` (`qemu-system-x86_64.exe --version` reports
  `v11.1.0-12130-ge470268ff4`)
- Primary license: GNU GPL version 2, with files and subcomponents under
  additional compatible licenses.
- Included license files:
  - `Tools/qemu/COPYING`
  - `Tools/qemu/COPYING.LIB`
  - `Tools/qemu/share/doc/about/license.html`
  - `LICENSES/GPL-2.0.txt`
  - `LICENSES/LGPL-2.1.txt`
- Upstream project: https://www.qemu.org/
- Upstream source repository: https://gitlab.com/qemu-project/qemu

## Alpine Linux Based YlvaOS Image

- Component: Alpine Linux virtual ISO and packages
- Base release used to extract boot assets: Alpine Linux `3.24.1` virt x86_64
- Source ISO:
  `https://dl-cdn.alpinelinux.org/alpine/latest-stable/releases/x86_64/alpine-virt-3.24.1-x86_64.iso`
- Source ISO SHA-256:
  `e73a6241bd5f3c5c2d4d38c02cc52c378c0415a7c888bd292066bf36e0f41a39`
- Bundled boot assets:
  - `vm/assets/vmlinuz`
  - `vm/assets/initrd.img`
- Bundled preinstalled root disk archive:
  - `vm/disk.qcow2.gz`
- Exact generated image hashes:
  - `LEGAL/YLVAOS_IMAGE_MANIFEST.json`
- Installed package list:
  - `LEGAL/alpine-installed-packages.txt`
- Upstream project: https://alpinelinux.org/
- Upstream package repositories:
  - https://dl-cdn.alpinelinux.org/alpine/v3.24/main
  - https://dl-cdn.alpinelinux.org/alpine/v3.24/community
- Upstream aports source tree:
  https://gitlab.alpinelinux.org/alpine/aports

The root disk includes, among other dependencies, the Linux kernel package,
BusyBox, musl libc, OpenRC, apk-tools, util-linux, doas, less, Vim, Wine,
PulseAudio, ALSA components, FluidSynth, a GM soundfont package, X desktop
packages, CJK fonts, and musl locale data. These packages are distributed under their own
upstream licenses. The package list in `LEGAL/alpine-installed-packages.txt` is
the authoritative list for this image build.

## Linux Kernel

- Component: Linux kernel, distributed through Alpine's `linux-virt` packaging
  and Alpine virt boot assets.
- License: GPL-2.0-only, with Linux kernel syscall exception and additional
  notices as documented by the Linux kernel project.
- Included license text: `LICENSES/GPL-2.0.txt`
- Upstream source: https://git.kernel.org/
- Alpine packaging source:
  https://gitlab.alpinelinux.org/alpine/aports

## Vim

- Component: Vim
- Installed package: see `vim-*` entries in `LEGAL/alpine-installed-packages.txt`
- License: Vim license / charityware terms as distributed by the Vim project.
- Upstream source: https://github.com/vim/vim
- Alpine packaging source:
  https://gitlab.alpinelinux.org/alpine/aports

## Wine

- Component: Wine
- Installed package: see `wine-*` entries in
  `LEGAL/alpine-installed-packages.txt`
- Current package family: Wine `11.0` from Alpine v3.24 community
- License: LGPL version 2.1 or later for Wine, with package licenses for helper
  libraries varying by component.
- Upstream project: https://www.winehq.org/
- Upstream source: https://source.winehq.org/git/wine.git
- Alpine packaging source:
  https://gitlab.alpinelinux.org/alpine/aports

## Audio Stack

- Components: ALSA libraries/tools/plugins, PulseAudio, PulseAudio ALSA
  integration, PulseAudio utilities, FluidSynth, and a GM soundfont package.
- Installed packages: see `LEGAL/alpine-installed-packages.txt`
- Licenses: package licenses vary by component. Alpine package metadata and
  APKBUILD files are the authoritative source for package-specific license
  tags.
- ALSA upstream: https://alsa-project.org/
- PulseAudio upstream: https://www.freedesktop.org/wiki/Software/PulseAudio/
- FluidSynth upstream: https://www.fluidsynth.org/
- Alpine packaging source:
  https://gitlab.alpinelinux.org/alpine/aports

## Fonts and Soundfont

- Components: DejaVu fonts, Noto CJK fonts, and the GM soundfont package used
  by FluidSynth.
- Installed packages: see `LEGAL/alpine-installed-packages.txt`
- Noto CJK upstream source: https://github.com/notofonts/noto-cjk
- Alpine packaging source:
  https://gitlab.alpinelinux.org/alpine/aports

## Locale Data

- Components: musl locale data used for Japanese Wine application behavior.
- Installed packages: see `LEGAL/alpine-installed-packages.txt`
- musl-locales project: https://git.adelielinux.org/adelie/musl-locales/
- Alpine packaging source:
  https://gitlab.alpinelinux.org/alpine/aports

## Notes for Redistribution

This notice file is a practical index for the MOD package; it is not legal
advice and may not be exhaustive for every transitive dependency. Before public
redistribution, verify that the package includes either the corresponding
source code or a compliant written offer where required by the relevant
licenses.
