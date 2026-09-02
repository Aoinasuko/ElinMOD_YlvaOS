# YlvaOS

YlvaOS is an Elin MOD that turns the in-game computer furniture into a usable in-game Linux VM window. It launches a QEMU-backed Alpine Linux environment branded as YlvaOS, with text console, lightweight desktop mode, clipboard paste, optional networking, Wine, and an Elona helper workflow.

## GitHub Pages

User-facing commands and usage are summarized in:

- `Docs/index.html`

When GitHub Pages is enabled from the repository root, `index.html` redirects to this help page.

## Public Repository Policy

This repository tracks the MOD source, build scripts, documentation, and license/source notices. It intentionally does not track generated or third-party runtime binaries:

- `Mod_YlvaOS/Tools/qemu/`
- `Mod_YlvaOS/vm/assets/vmlinuz`
- `Mod_YlvaOS/vm/assets/initrd.img`
- `Mod_YlvaOS/vm/disk.qcow2.gz`
- `Docs/elona/`

Use `Tools/build_ylvaos_image.py` to rebuild the Linux boot assets and root disk, and provide QEMU/runtime payloads through a release package when distributing a ready-to-run MOD.

## Build

```powershell
dotnet build .\YlvaOS\YlvaOS.csproj -c Release -v:minimal
```

The built DLL is written to `Mod_YlvaOS/` for local Elin package testing.

## Legal

See `Mod_YlvaOS/LEGAL/`, `Mod_YlvaOS/LICENSES/`, `Mod_YlvaOS/THIRD_PARTY_NOTICES.md`, and `Mod_YlvaOS/SOURCE_OFFER.md` for third-party notices and source access notes.
