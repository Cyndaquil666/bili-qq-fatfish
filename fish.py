#!/usr/bin/env python
"""大肥鱼 — one entity, two lives.

    python fish.py init
    python fish.py doctor
    python fish.py login-bili
    python fish.py run

Vendored dependencies live in ./vendor (see tools/setup_vendor.py), so this
works in environments where pip cannot write its temp directory.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for p in (ROOT, ROOT / "vendor"):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

# Windows consoles default to cp936; make Chinese logs and QR output sane.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:
        pass

from fatfish.cli import main  # noqa: E402
from fatfish.util import load_env_file  # noqa: E402

load_env_file(ROOT / ".env")

if __name__ == "__main__":
    raise SystemExit(main())
