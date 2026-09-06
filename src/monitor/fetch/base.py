"""Shared fetch infrastructure.

* `Client` wraps httpx with per-source minimum spacing, exponential backoff on 429/5xx and
  network errors, and the optional key header from `config/sources.yaml`.
* `RawStore` writes immutable raw responses to `data/raw/YYYY/MM/DD/<name>_<HHMM>.json.gz`
  before any parsing. A fetch is idempotent per bucket: if the file exists it is reused
  (pass `force=True` to refetch), so re-running a job for the same timestamp cannot duplicate
  or corrupt data.
* `Envelope` is the raw-file format: one file holds every request an adapter made for a
  dataset in that bucket, each with url, status, fetched_at and the untouched body.
"""

from __future__ import annotations

import gzip
import json
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from monitor.meta import git_sha, utc_now
from monitor.paths import CONFIG, RAW

UA = "crypto-monitor/0.1 (+https://github.com/wernerhl/crypto-monitor)"


class SourceStaleError(RuntimeError):
    """Raised when a source's `verified_on` is older than the allowed age."""


class SanityError(RuntimeError):
    """Raised when a parsed table fails its sanity check (row count, freshness, ranges)."""


def load_sources() -> dict[str, Any]:
    return yaml.safe_load((CONFIG / "sources.yaml").read_text())


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError | httpx.TimeoutException)


@dataclass
class Client:
    """HTTP client for one source, with spacing and backoff."""

    source: str
    cfg: dict[str, Any] = field(default_factory=dict)
    timeout: float = 60.0
    _last: float = 0.0
    _http: httpx.Client | None = None

    def __post_init__(self) -> None:
        sources = load_sources()
        self.cfg = {**sources["sources"][self.source], **self.cfg}
        age = (date.today() - self.cfg["verified_on"]).days
        if (
            age > sources["max_verification_age_days"]
            and os.environ.get("FORCE_STALE_SOURCE") != "1"
        ):
            raise SourceStaleError(
                f"{self.source} verified {age} days ago; re-run scripts/verify_sources.py and update config/sources.yaml"
            )
        headers = {"User-Agent": UA, "Accept": "application/json, text/csv, */*"}
        key_env = self.cfg.get("key_env")
        self.has_key = bool(key_env and os.environ.get(key_env))
        if self.has_key and self.cfg.get("key_header"):
            headers[self.cfg["key_header"]] = os.environ[key_env]
        self._http = httpx.Client(base_url=self.cfg["base"], headers=headers, timeout=self.timeout)

    @property
    def min_interval(self) -> float:
        # a demo key lifts CoinGecko to 30/min; otherwise use the observed safe spacing
        if self.source == "coingecko" and self.has_key:
            return 2.1
        return float(self.cfg.get("min_interval_s", 0.15))

    def _space(self) -> None:
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    @retry(
        retry=retry_if_exception(_retryable),
        wait=wait_exponential(multiplier=2, min=2, max=90),
        stop=stop_after_attempt(6),
        reraise=True,
    )
    def _request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        self._space()
        assert self._http is not None
        r = self._http.request(method, url, **kw)
        if r.status_code == 429:
            ra = r.headers.get("retry-after")
            time.sleep(min(float(ra), 120) if ra and ra.isdigit() else 10)
        r.raise_for_status()
        return r

    def get(self, url: str, params: dict[str, Any] | None = None) -> Record:
        r = self._request("GET", url, params=params)
        return Record.from_response(r)

    def post_json(self, url: str, body: dict[str, Any]) -> Record:
        r = self._request("POST", url, json=body)
        return Record.from_response(r)

    def close(self) -> None:
        if self._http:
            self._http.close()


@dataclass
class Record:
    """One HTTP exchange, stored verbatim."""

    url: str
    status: int
    fetched_at: str
    body: Any  # parsed JSON when the response was JSON, else the text
    is_json: bool = True
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_response(cls, r: httpx.Response) -> Record:
        keep = {
            k: v
            for k, v in r.headers.items()
            if k.lower()
            in {
                "x-mbx-used-weight-1m",
                "ratelimit-remaining",
                "x-ratelimit-remaining",
                "retry-after",
            }
        }
        try:
            body, is_json = r.json(), True
        except ValueError:
            body, is_json = r.text, False
        return cls(str(r.url), r.status_code, utc_now().isoformat(), body, is_json, keep)


@dataclass
class Envelope:
    source: str
    dataset: str
    bucket: str
    fetched_at: str
    git_sha: str
    records: list[Record]
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "dataset": self.dataset,
            "bucket": self.bucket,
            "fetched_at": self.fetched_at,
            "git_sha": self.git_sha,
            "meta": self.meta,
            "records": [r.__dict__ for r in self.records],
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Envelope:
        recs = [Record(**r) for r in d["records"]]
        return cls(
            d["source"],
            d["dataset"],
            d["bucket"],
            d["fetched_at"],
            d["git_sha"],
            recs,
            d.get("meta", {}),
        )


def bucket_for(ts: datetime, freq: str) -> tuple[str, str]:
    """Return (YYYY/MM/DD, HHMM) for a fetch timestamp. Hourly jobs floor to the hour,
    daily jobs use 0000 so a re-run on the same day hits the same file."""
    ts = ts.astimezone(UTC)
    hhmm = f"{ts:%H}00" if freq == "hourly" else "0000"
    return f"{ts:%Y/%m/%d}", hhmm


class RawStore:
    def __init__(self, root: Path = RAW) -> None:
        self.root = root

    def path(self, name: str, ts: datetime, freq: str) -> Path:
        day, hhmm = bucket_for(ts, freq)
        return self.root / day / f"{name}_{hhmm}.json.gz"

    def exists(self, name: str, ts: datetime, freq: str) -> bool:
        return self.path(name, ts, freq).exists()

    def write(self, env: Envelope, ts: datetime, freq: str) -> Path:
        p = self.path(f"{env.source}_{env.dataset}", ts, freq)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(env.to_json(), separators=(",", ":"), ensure_ascii=False).encode()
        p.write_bytes(gzip.compress(payload, mtime=0))
        return p

    def read(self, p: Path) -> Envelope:
        return Envelope.from_json(json.loads(gzip.decompress(p.read_bytes())))

    def latest(self, name: str, before: datetime | None = None) -> Path | None:
        """Most recent raw file for `<source>_<dataset>`; optionally not after `before`."""
        files = sorted(self.root.glob(f"*/*/*/{name}_*.json.gz"))
        if before is not None:
            day, hhmm = bucket_for(before, "hourly")
            cutoff = self.root / day / f"{name}_{hhmm}.json.gz"
            files = [f for f in files if str(f) <= str(cutoff)]
        return files[-1] if files else None

    def all(self, name: str) -> list[Path]:
        return sorted(self.root.glob(f"*/*/*/{name}_*.json.gz"))


def run_dataset(
    source: str,
    dataset: str,
    freq: str,
    fetcher,
    *,
    ts: datetime | None = None,
    force: bool = False,
    meta: dict[str, Any] | None = None,
    store: RawStore | None = None,
) -> Path:
    """Idempotent wrapper: reuse the raw file for this bucket unless `force`; else call
    `fetcher(client) -> list[Record]`, write the envelope and return its path."""
    store = store or RawStore()
    ts = ts or utc_now()
    name = f"{source}_{dataset}"
    if not force and store.exists(name, ts, freq):
        return store.path(name, ts, freq)
    client = Client(source)
    try:
        records = fetcher(client)
    finally:
        client.close()
    env = Envelope(
        source,
        dataset,
        bucket_for(ts, freq)[1],
        utc_now().isoformat(),
        git_sha(),
        records,
        meta or {},
    )
    return store.write(env, ts, freq)


# --------------------------------------------------------------------------- size control
def trim_book(bids: list, asks: list, pct: float = 0.025) -> tuple[list, list, bool]:
    """Keep only levels within ±pct of mid. Books are the largest hourly payloads (a full
    Coinbase L2 book is > 1 MB); the pipeline uses ±2 %, so ±3 % keeps every number
    traceable while cutting the raw file ~10×. Returns (bids, asks, trimmed_flag)."""
    if not bids or not asks:
        return bids, asks, False
    mid = (float(bids[0][0]) + float(asks[0][0])) / 2.0
    b = [x for x in bids if float(x[0]) >= mid * (1 - pct)]
    a = [x for x in asks if float(x[0]) <= mid * (1 + pct)]
    return b, a, (len(b) < len(bids) or len(a) < len(asks))
