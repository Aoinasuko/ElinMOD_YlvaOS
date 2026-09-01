# YlvaOS Legal Metadata

This directory contains generated and hand-written metadata for the Linux VM
image bundled with YlvaOS.

- `YLVAOS_IMAGE_MANIFEST.json` records the Alpine ISO URL, the verified ISO
  SHA-256, and hashes for generated VM artifacts.
- `alpine-installed-packages.txt` lists packages installed into the current
  preinstalled root disk.
- `THIRD_PARTY_NOTICES.md` summarizes bundled third party components and
  license families.
- `SOURCE_ACCESS.md` records source retrieval and rebuild paths for the bundled
  VM artifacts.

Regenerate these files with:

```powershell
python.exe .\Tools\build_ylvaos_image.py --force
```
