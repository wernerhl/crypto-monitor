"""Websocket liquidation collector (work order B1; notes §3 mode (b)).

Binance USDⓈ-M publishes every forced order on `!forceOrder@arr`; Bybit publishes
`allLiquidation.<symbol>` on the v5 linear stream. Neither venue serves liquidation history
over REST, so a resident process is the only way to build a sample. The collector buffers
messages and, at every hour boundary and on shutdown, writes the completed hour as one raw
envelope per venue into the immutable raw store (`binance_liquidations_ws_HH00`,
`bybit_liquidations_ws_HH00`), merging with an envelope already present for that hour (a
restart inside the hour). The hourly compute job parses these into the `liquidations` table
next to the OKX REST sample, and the positioning row labels its `liq_source` by the venues
present.

Run:  python -m monitor.fetch.liq_ws            (from the collector clone; launchd keeps it up)
Requires the `websockets` package (declared in pyproject)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
from datetime import UTC, datetime, timedelta

from monitor.fetch.base import Envelope, RawStore, Record, bucket_for
from monitor.meta import git_sha, utc_now

log = logging.getLogger("monitor.liq_ws")
BINANCE_URL = "wss://fstream.binance.com/ws/!forceOrder@arr"
BYBIT_URL = "wss://stream.bybit.com/v5/public/linear"
BYBIT_DEFAULT = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "BNBUSDT",
    "ADAUSDT",
    "LINKUSDT",
    "AVAXUSDT",
    "SUIUSDT",
]


def bybit_symbols() -> list[str]:
    """Tier 1+2 perps as listed on Bybit in the latest snapshot; a fixed default otherwise."""
    try:
        import polars as pl

        from monitor import archive

        ps = archive.read("perp_snapshot")
        uni = archive.read("universe")
        if ps is None or uni is None or not ps.height or not uni.height:
            return BYBIT_DEFAULT
        u = uni.filter((pl.col("as_of") == uni["as_of"].max()) & pl.col("tier").is_in([1, 2]))
        bases = set(u["symbol"].to_list())
        syms = (
            ps.filter(
                (pl.col("venue") == "bybit")
                & (pl.col("ts") == ps["ts"].max())
                & pl.col("base").is_in(bases)
            )
            .select("symbol")
            .unique()["symbol"]
            .to_list()
        )
        return sorted(syms) or BYBIT_DEFAULT
    except Exception as e:  # the collector must start even if the archive is unreadable
        log.warning("bybit symbol list fell back to the default: %s", e)
        return BYBIT_DEFAULT


class HourBuffer:
    """Per-venue buffer of raw messages for the hour that is currently open."""

    def __init__(self, store: RawStore, venue: str, dataset: str = "liquidations_ws") -> None:
        self.store, self.venue, self.dataset = store, venue, dataset
        self.hour: datetime | None = None
        self.msgs: list[dict] = []

    def add(self, msg: dict, now: datetime | None = None) -> None:
        now = now or utc_now()
        h = now.replace(minute=0, second=0, microsecond=0)
        if self.hour is not None and h != self.hour:
            self.flush()
        self.hour = h
        self.msgs.append(msg)

    def flush(self) -> None:
        if self.hour is None or not self.msgs:
            return
        p = self.store.path(f"{self.venue}_{self.dataset}", self.hour, "hourly")
        records: list[Record] = []
        if p.exists():
            records = self.store.read(p).records
        records.append(
            Record(
                url=BINANCE_URL if self.venue == "binance" else BYBIT_URL,
                status=101,
                fetched_at=utc_now().isoformat(),
                body=self.msgs,
                is_json=True,
            )
        )
        env = Envelope(
            self.venue,
            self.dataset,
            "/".join(bucket_for(self.hour, "hourly")),
            utc_now().isoformat(),
            git_sha(),
            records,
            {"kind": "websocket", "hour": self.hour.isoformat()},
        )
        self.store.write(env, self.hour, "hourly")
        log.info("%s: wrote %d messages for %s", self.venue, len(self.msgs), self.hour.isoformat())
        self.msgs = []
        self.hour = None


async def _binance(buf: HourBuffer, stop: asyncio.Event) -> None:
    import websockets

    while not stop.is_set():
        try:
            async with websockets.connect(BINANCE_URL, ping_interval=180, max_queue=4096) as ws:
                log.info("binance connected")
                async for raw in ws:
                    buf.add(json.loads(raw))
                    if stop.is_set():
                        break
        except Exception as e:
            log.warning("binance stream: %s; reconnecting in 5 s", e)
            await asyncio.sleep(5)


async def _bybit(buf: HourBuffer, stop: asyncio.Event, symbols: list[str]) -> None:
    import websockets

    while not stop.is_set():
        try:
            async with websockets.connect(BYBIT_URL, ping_interval=None, max_queue=4096) as ws:
                for i in range(0, len(symbols), 10):  # ≤ 10 topics per subscribe message
                    await ws.send(
                        json.dumps(
                            {
                                "op": "subscribe",
                                "args": [f"allLiquidation.{s}" for s in symbols[i : i + 10]],
                            }
                        )
                    )
                log.info("bybit connected, %d symbols", len(symbols))

                async def ping() -> None:
                    while True:
                        await asyncio.sleep(20)
                        await ws.send(json.dumps({"op": "ping"}))

                pt = asyncio.create_task(ping())
                try:
                    async for raw in ws:
                        m = json.loads(raw)
                        if m.get("topic", "").startswith("allLiquidation."):
                            buf.add(m)
                        if stop.is_set():
                            break
                finally:
                    pt.cancel()
        except Exception as e:
            log.warning("bybit stream: %s; reconnecting in 5 s", e)
            await asyncio.sleep(5)


async def _hourly_flush(bufs: list[HourBuffer], stop: asyncio.Event) -> None:
    """Flush a quiet hour too (no message may arrive to trigger the roll-over)."""
    while not stop.is_set():
        now = utc_now()
        nxt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1, seconds=5)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=(nxt - now).total_seconds())
        for b in bufs:
            if b.hour is not None and b.hour < utc_now().replace(minute=0, second=0, microsecond=0):
                b.flush()


async def run(
    store: RawStore | None = None, venues: tuple[str, ...] = ("binance", "bybit")
) -> None:
    store = store or RawStore()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    bufs = {v: HourBuffer(store, v) for v in venues}
    tasks = [asyncio.create_task(_hourly_flush(list(bufs.values()), stop))]
    if "binance" in bufs:
        tasks.append(asyncio.create_task(_binance(bufs["binance"], stop)))
    if "bybit" in bufs:
        tasks.append(asyncio.create_task(_bybit(bufs["bybit"], stop, bybit_symbols())))
    await stop.wait()
    for t in tasks:
        t.cancel()
    for b in bufs.values():
        b.flush()
    log.info("collector stopped at %s", datetime.now(UTC).isoformat())


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run())
