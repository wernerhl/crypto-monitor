"""Snapshot governance (GraphQL, verified 2026-09-06; 100 req/min). Tally is optional (key)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from monitor.fetch.base import Envelope, Record, run_dataset

Q = """query($spaces: [String], $state: String) { proposals(first: 100, where: {state: $state, space_in: $spaces}, orderBy: "end", orderDirection: asc) { id title choices start end state scores scores_total author space { id name } link } }"""


def fetch_proposals(spaces: list[str], ts: datetime | None = None, force: bool = False) -> Path:
    def go(c) -> list[Record]:
        return [
            c.post_json("", {"query": Q, "variables": {"spaces": spaces, "state": st}})
            for st in ("active", "pending")
        ]

    return run_dataset(
        "snapshot", "proposals", "daily", go, ts=ts, force=force, meta={"spaces": spaces}
    )


def parse_proposals(env: Envelope) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    rows = []
    for rec in env.records:
        for p in (rec.body.get("data") or {}).get("proposals") or []:
            rows.append(
                {
                    "id": p["id"],
                    "space": p["space"]["id"],
                    "space_name": p["space"].get("name"),
                    "title": p["title"],
                    "state": p["state"],
                    "start": datetime.fromtimestamp(p["start"], tz=UTC),
                    "end": datetime.fromtimestamp(p["end"], tz=UTC),
                    "scores_total": p.get("scores_total"),
                    "link": p.get("link"),
                    "source": "snapshot",
                    "fetched_at": fetched,
                    "git_sha": env.git_sha,
                }
            )
    return pl.DataFrame(
        rows,
        schema={
            "id": pl.Utf8,
            "space": pl.Utf8,
            "space_name": pl.Utf8,
            "title": pl.Utf8,
            "state": pl.Utf8,
            "start": pl.Datetime("us", "UTC"),
            "end": pl.Datetime("us", "UTC"),
            "scores_total": pl.Float64,
            "link": pl.Utf8,
            "source": pl.Utf8,
            "fetched_at": pl.Datetime("us", "UTC"),
            "git_sha": pl.Utf8,
        },
    )
