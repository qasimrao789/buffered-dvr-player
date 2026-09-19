from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if len(sys.argv) != 2 or not re.fullmatch(r"\d+\.\d+\.\d+", sys.argv[1]):
        raise SystemExit("usage: python scripts/set_version.py X.Y.Z")

    version = sys.argv[1]
    major, minor, patch = (int(part) for part in version.split("."))

    py = ROOT / "dvr_player.py"
    text = py.read_text(encoding="utf-8")
    text, count = re.subn(
        r'APP_VERSION\s*=\s*"[^"]+"',
        f'APP_VERSION = "{version}"',
        text,
        count=1,
    )
    if count != 1:
        raise RuntimeError("Could not update APP_VERSION")
    py.write_text(text, encoding="utf-8")

    iss = ROOT / "installer.iss"
    text = iss.read_text(encoding="utf-8")
    text, count = re.subn(
        r'#define MyAppVersion "[^"]+"',
        f'#define MyAppVersion "{version}"',
        text,
        count=1,
    )
    if count != 1:
        raise RuntimeError("Could not update installer version")
    iss.write_text(text, encoding="utf-8")

    info = ROOT / "version_info.txt"
    text = info.read_text(encoding="utf-8")
    text = re.sub(r"filevers=\([^)]*\)", f"filevers=({major}, {minor}, {patch}, 0)", text, count=1)
    text = re.sub(r"prodvers=\([^)]*\)", f"prodvers=({major}, {minor}, {patch}, 0)", text, count=1)
    text = re.sub(r"StringStruct\(u'FileVersion', u'[^']+'\)", f"StringStruct(u'FileVersion', u'{version}')", text, count=1)
    text = re.sub(r"StringStruct\(u'ProductVersion', u'[^']+'\)", f"StringStruct(u'ProductVersion', u'{version}')", text, count=1)
    info.write_text(text, encoding="utf-8")

    print(f"Version set to {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
