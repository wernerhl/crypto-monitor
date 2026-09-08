"""Assert that the files staged for commit fall inside a job's write set (config/job_writes.yaml).

Usage: python scripts/check_write_set.py <job> [--staged | --files f1 f2 ...]
Exit 1 with the offending paths when a file outside the set is staged. Work order 3, item 3:
each job writes only its own tables and JSON, so concurrent jobs never conflict."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def allowed(job: str, path: str, cfg: dict) -> bool:
    spec = cfg[job]
    if path.startswith("data/raw/"):
        return bool(spec.get("raw"))
    if path.startswith("data/processed/"):
        return Path(path).stem in spec["tables"]
    if path.startswith("data/archive/"):
        return path.split("/")[2] in spec["tables"]
    return path in spec.get("site", [])


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: check_write_set.py <job> [--files ...]", file=sys.stderr)
        return 2
    job = argv[0]
    cfg = yaml.safe_load((ROOT / "config" / "job_writes.yaml").read_text())
    if job not in cfg:
        print(f"unknown job {job!r}; known: {sorted(cfg)}", file=sys.stderr)
        return 2
    if len(argv) > 1 and argv[1] == "--files":
        files = argv[2:]
    else:
        out = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            check=True,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        files = [f for f in out.stdout.splitlines() if f]
    bad = [f for f in files if not allowed(job, f, cfg)]
    if bad:
        print(
            f"::error::job {job} staged files outside its write set (config/job_writes.yaml):",
            file=sys.stderr,
        )
        for f in bad:
            print(f"  {f}", file=sys.stderr)
        return 1
    print(f"write set ok for {job}: {len(files)} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
