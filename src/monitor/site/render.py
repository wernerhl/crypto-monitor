"""Static site generator (notes Section 12).

Phase 1: renders the placeholder front page with the build timestamp and git sha embedded
as `data-build-sha`, which the daily job's verify step checks on the live URL.
"""

from __future__ import annotations

import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from monitor import __version__
from monitor.meta import git_sha, utc_now
from monitor.paths import SITE, SITE_DATA, TEMPLATES

NOTICE = (
    "This system is a monitoring tool. It is not investment advice and it does not produce "
    "buy or sell recommendations."
)


def render(out_dir: Path = SITE) -> Path:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    built_at = utc_now().isoformat()
    ctx = {
        "built_at": built_at,
        "git_sha": git_sha(),
        "version": __version__,
        "notice": NOTICE,
        "phase": 1,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    SITE_DATA.mkdir(parents=True, exist_ok=True)
    (SITE_DATA / "build.json").write_text(json.dumps(ctx, indent=1))
    for name in ("index.html", "universe.html", "onchain.html", "methods.html", "status.html"):
        html = env.get_template(name).render(**ctx)
        (out_dir / name).write_text(html)
    (out_dir / ".nojekyll").touch()
    return out_dir / "index.html"
