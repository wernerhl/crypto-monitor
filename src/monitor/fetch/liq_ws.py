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
from datetime import UTC, datetime

from monitor.fetch.base import Envelope, RawStore, Record, bucket_for
from monitor.meta import git_sha, utc_now

log = logging.getLogger("monitor.liq_ws")
# Placement test 2026-09-09 (work order 4, item 4): the USDⓈ-M host fstream.binance.com accepts
# the websocket and sends nothing on any stream from the Mac, Frankfurt or Sydney, while
# dstream.binance.com delivers the all-market forced-order feed (USDT and COIN-M symbols)
# everywhere, the Mac included. The collector therefore reads Binance liquidations from dstream.
BINANCE_URL = "wss://dstream.binance.com/ws/!forceOrder@arr"
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
    """Tier 1 perps on Bybit: the Tier 1 symbols of the latest universe with the USDT suffix
    (work order 2, item 5); the fixed default only when the universe table is unreadable."""
    try:
        import polars as pl

        from monitor import archive

        uni = archive.read("universe")
        if uni is None or not uni.height:
            return BYBIT_DEFAULT
        u = uni.filter((pl.col("as_of") == uni["as_of"].max()) & (pl.col("tier") == 1))
        syms = sorted(f"{b}USDT" for b in u["symbol"].to_list())
        return syms or BYBIT_DEFAULT
    except Exception as e:  # the collector must start even if the archive is unreadable
        log.warning("bybit symbol list fell back to the default: %s", e)
        return BYBIT_DEFAULT


class HourBuffer:
    """Per-venue buffer of raw messages for the hour that is currently open, plus the seconds
    the stream was connected during that hour (coverage, work order 2 item 5). A connected but
    silent hour is written too, with zero messages, so silence is visible and not read as a
    quiet market."""

    def __init__(self, store: RawStore, venue: str, dataset: str = "liquidations_ws") -> None:
        self.store, self.venue, self.dataset = store, venue, dataset
        self.hour: datetime | None = None
        self.msgs: list[dict] = []
        self.connected_seconds = 0.0
        self.connected = False

    def _roll(self, now: datetime) -> None:
        h = now.replace(minute=0, second=0, microsecond=0)
        if self.hour is not None and h != self.hour:
            self.flush()
        self.hour = h

    def add(self, msg: dict, now: datetime | None = None) -> None:
        self._roll(now or utc_now())
        self.msgs.append(msg)

    def tick(self, seconds: float, now: datetime | None = None) -> None:
        """Called every few seconds by the flush loop: accrue connected time to the open hour."""
        self._roll(now or utc_now())
        if self.connected:
            self.connected_seconds += seconds

    def flush(self) -> None:
        if self.hour is None or (not self.msgs and self.connected_seconds <= 0):
            return
        p = self.store.path(f"{self.venue}_{self.dataset}", self.hour, "hourly")
        records: list[Record] = []
        prev_cov = 0.0
        if p.exists():
            old = self.store.read(p)
            records = old.records
            prev_cov = float((old.meta.get("coverage") or {}).get("connected_seconds") or 0.0)
        records.append(
            Record(
                url=BINANCE_URL if self.venue == "binance" else BYBIT_URL,
                status=101,
                fetched_at=utc_now().isoformat(),
                body=self.msgs,
                is_json=True,
            )
        )
        cov = {
            "hour": self.hour.isoformat(),
            "connected_seconds": min(3600.0, prev_cov + self.connected_seconds),
            "n_messages": sum(len(r.body or []) for r in records),
        }
        env = Envelope(
            self.venue,
            self.dataset,
            "/".join(bucket_for(self.hour, "hourly")),
            utc_now().isoformat(),
            git_sha(),
            records,
            {"kind": "websocket", "hour": self.hour.isoformat(), "coverage": cov},
        )
        self.store.write(env, self.hour, "hourly")
        log.info(
            "%s: wrote %d messages for %s (connected %.0f s)",
            self.venue,
            len(self.msgs),
            self.hour.isoformat(),
            cov["connected_seconds"],
        )
        self.msgs = []
        self.connected_seconds = 0.0
        self.hour = None


async def _binance(buf: HourBuffer, stop: asyncio.Event) -> None:
    import websockets

    while not stop.is_set():
        try:
            async with websockets.connect(BINANCE_URL, ping_interval=180, max_queue=4096) as ws:
                log.info("binance connected")
                buf.connected = True
                async for raw in ws:
                    buf.add(json.loads(raw))
                    if stop.is_set():
                        break
        except Exception as e:
            log.warning("binance stream: %s; reconnecting in 5 s", e)
            await asyncio.sleep(5)
        finally:
            buf.connected = False


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
                buf.connected = True

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
        finally:
            buf.connected = False


async def _hourly_flush(bufs: list[HourBuffer], stop: asyncio.Event, tick_s: float = 5.0) -> None:
    """Every `tick_s` seconds accrue connected time to the open hour (coverage) and roll the
    hour over, so a quiet hour is written too (no message may arrive to trigger it)."""
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=tick_s)
        now = utc_now()
        for b in bufs:
            b.tick(tick_s, now)


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
