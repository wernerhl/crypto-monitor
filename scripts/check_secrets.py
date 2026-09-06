"""Fail if any given file contains something that looks like an API key or token.

Used by the pre-commit hook and by CI (`python scripts/check_secrets.py $(git ls-files)`).
Patterns are deliberately broad; add an allow-comment `# not-a-secret` on the line to bypass.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PATTERNS = [
    re.compile(
        r"(?i)(api[_-]?key|secret|token|passwd|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}"
    ),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),  # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS
    re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),
    re.compile(r"\b[a-f0-9]{32}\b(?=.*(?i:fred))"),  # FRED keys are 32 hex chars
]
SKIP_SUFFIXES = {".gz", ".parquet", ".png", ".jpg", ".pdf", ".lock"}


def scan(path: Path) -> list[str]:
    if path.suffix in SKIP_SUFFIXES or not path.is_file():
        return []
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return []
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        if "not-a-secret" in line:
            continue
        for p in PATTERNS:
            if p.search(line):
                hits.append(f"{path}:{n}: matches {p.pattern[:40]}…")
                break
    return hits


def main(argv: list[str]) -> int:
    files = [Path(a) for a in argv] or [p for p in Path(".").rglob("*") if ".git" not in p.parts]
    hits = [h for f in files for h in scan(f)]
    if hits:
        print("Possible secrets found:\n" + "\n".join(hits), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
