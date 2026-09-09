"""Syntax-check the inline scripts of the rendered site with `node --check` (CI and local).

A broken front-page script leaves the panels empty while the JSON is fine; this catches it
before deploy. Usage: python scripts/check_site_js.py [site_dir]"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main(argv: list[str]) -> int:
    site = Path(argv[0]) if argv else Path("site")
    node = shutil.which("node")
    if not node:
        print("node not found; skipping site JS syntax check")
        return 0
    bad = 0
    for html in sorted(site.glob("*.html")):
        for i, js in enumerate(re.findall(r"<script>(.*?)</script>", html.read_text(), flags=re.S)):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                f.write(js)
            r = subprocess.run([node, "--check", f.name], capture_output=True, text=True)
            if r.returncode != 0:
                bad += 1
                print(
                    f"::error::{html.name} script #{i + 1}: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else 'syntax error'}"
                )
    print(f"site JS check: {'ok' if not bad else f'{bad} broken script(s)'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
