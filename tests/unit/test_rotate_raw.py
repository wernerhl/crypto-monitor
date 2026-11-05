"""scripts/rotate_raw.sh (2026-11-04): raw rotation writes size-capped tar parts and never rewrites a
tar; a failed archive push leaves every raw file in place. Throwaway git repositories with a local
bare origin; nothing leaves the temp directory."""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "rotate_raw.sh"
pytestmark = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("git") and shutil.which("tar")),
    reason="needs bash, git, tar",
)


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout


def _setup(tmp: Path, reject_push: bool = False) -> tuple[Path, Path]:
    origin = tmp / "origin.git"
    _git(tmp, "init", "-q", "--bare", str(origin))
    repo = tmp / "repo"
    _git(tmp, "clone", "-q", str(origin), str(repo))
    (repo / "README").write_text("x")
    _git(repo, "add", "README")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "push", "-q", "origin", "HEAD:main")
    if reject_push:  # what GitHub's GH001 does: refuse the archive push
        hook = origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'error: GH001: Large files detected' >&2\nexit 1\n")
        hook.chmod(0o755)
    return origin, repo


def _raw(repo: Path, day: datetime, name: str, size: int) -> Path:
    """An hourly raw file unless the name ends in _0000 (the daily file, kept 90 days)."""
    p = repo / "data" / "raw" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(os.urandom(size))  # incompressible, like the gzip payloads
    return p


def _run(repo: Path, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=repo,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            **env,
        },
        capture_output=True,
        text=True,
    )


def _archive_files(origin: Path, tmp: Path) -> dict[str, int]:
    chk = tmp / "chk"
    _git(tmp, "clone", "-q", "--branch", "data-archive", str(origin), str(chk))
    return {str(p.relative_to(chk)): p.stat().st_size for p in chk.rglob("*.tar")}


def test_parts_are_capped_and_hold_every_file(tmp_path):
    origin, repo = _setup(tmp_path)
    old = datetime.now(UTC) - timedelta(days=40)  # hourly files older than 10 days rotate
    files = [
        _raw(repo, old + timedelta(days=i % 3), f"src_x_{i + 1:04d}.json.gz", 300_000)
        for i in range(12)
    ]
    keep = _raw(
        repo, datetime.now(UTC) - timedelta(days=1), "src_x_0101.json.gz", 1000
    )  # too recent
    r = _run(repo, MAX_PART_MB="1")
    assert r.returncode == 0, r.stderr + r.stdout
    assert all(not f.exists() for f in files) and keep.exists()
    tars = _archive_files(origin, tmp_path)
    assert len(tars) >= 4, tars  # 12 x 0.3 MB under a 1 MB cap: several parts
    assert all(s <= 1024 * 1024 for s in tars.values()), tars
    assert all("/rot-" in k and k.startswith("raw-") for k in tars), tars
    names = []
    for k in tars:
        with tarfile.open(tmp_path / "chk" / k) as t:
            names += [m.name for m in t.getmembers() if m.isfile()]
    assert sorted(Path(n).name for n in names) == sorted(f.name for f in files)


def test_an_existing_month_tar_is_never_rewritten(tmp_path):
    origin, repo = _setup(tmp_path)
    old = datetime.now(UTC) - timedelta(days=40)
    _raw(repo, old, "a_x_0001.json.gz", 10_000)
    assert _run(repo).returncode == 0
    # simulate the legacy layout: a monthly tar already on the branch
    wt = tmp_path / "wt"
    _git(tmp_path, "clone", "-q", "--branch", "data-archive", str(origin), str(wt))
    legacy = wt / f"raw-{old:%Y-%m}.tar"
    legacy.write_bytes(b"legacy")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "legacy tar")
    _git(wt, "push", "-q", "origin", "data-archive")
    _raw(repo, old, "a_x_0002.json.gz", 10_000)
    r = _run(repo)
    assert r.returncode == 0, r.stderr
    tars = _archive_files(origin, tmp_path)
    assert tars[f"raw-{old:%Y-%m}.tar"] == len(b"legacy")  # untouched
    assert sum(1 for k in tars if k.startswith(f"raw-{old:%Y-%m}/")) == 2


def test_a_rejected_push_restores_every_raw_file(tmp_path):
    _, repo = _setup(tmp_path, reject_push=True)
    old = datetime.now(UTC) - timedelta(days=40)
    files = [_raw(repo, old, f"b_x_{i + 1:04d}.json.gz", 5_000) for i in range(5)]
    r = _run(repo)
    assert r.returncode != 0
    assert all(f.exists() for f in files), (
        "moved files must be put back when the archive push fails"
    )
    assert "restored" in r.stderr
    assert not _git(repo, "worktree", "list").count("\n") > 1  # the temporary worktree is gone
