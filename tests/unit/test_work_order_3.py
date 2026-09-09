"""Work order 3 (2026-09-08): Rule 5.1 as calendar (1), Rule 4.3 drivers (2), write sets and no
automatic merge resolution (3), Φ validation after shocks (5), robust-z guard."""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import yaml

from monitor import rules as rules_mod
from monitor.compute import fragility_validation as fv
from monitor.compute import hitrates, rule43
from monitor.compute.positioning import robust_z

ROOT = Path(__file__).resolve().parents[2]
TH = {
    "cliff": {"single_unlock_float_share_min": 0.01, "single_unlock_days_of_volume_min": 2.0},
    "vol_underpricing": {"vrp_max": 0.0, "phi_min": 1.0},
}


def test_rule_51_is_a_calendar_not_a_trigger():
    f = rules_mod.cliff("ARB", datetime(2026, 9, 8, tzinfo=UTC), 0.03, 0.5, TH, "2026-09-20")
    assert f.status == "calendar" and f.fired is True and f.rule_id == "5.1"
    assert "5.1" not in hitrates.HORIZONS  # a calendar has no forward claim
    t = rules_mod.vol_underpricing("BTC", datetime(2026, 9, 8, tzinfo=UTC), -0.05, 1.2, TH)
    assert t.status == "reading" and t.fired is True  # review decision 2 (work order 4)
    c = rules_mod.crowded_long.__name__
    assert c == "crowded_long"


def test_rule_43_carries_its_driver_without_using_it():
    drv = {
        "driver": "post-shock",
        "text": "post-shock, RV at the 92nd percentile",
        "iv_pctile_250": 0.4,
        "rv_pctile_250": 0.92,
    }
    a = rules_mod.vol_underpricing("BTC", datetime(2026, 9, 8, tzinfo=UTC), -0.05, 1.2, TH, drv)
    b = rules_mod.vol_underpricing("BTC", datetime(2026, 9, 8, tzinfo=UTC), -0.05, 1.2, TH, None)
    assert a.fired is b.fired is True and a.inputs["driver_text"].startswith("post-shock")
    assert (
        rules_mod.vol_underpricing(
            "BTC", datetime(2026, 9, 8, tzinfo=UTC), None, 1.2, TH, drv
        ).fired
        is None
    )


def test_rule43_driver_classification():
    start = date(2024, 1, 1)
    n = 400
    rng = np.random.default_rng(1)
    dates = [start + timedelta(days=i) for i in range(n)]
    iv = 0.5 + 0.05 * rng.standard_normal(n)
    rv_var = (0.45 + 0.05 * rng.standard_normal(n)) ** 2
    iv[350:] = 0.3  # complacency: implied vol far below its median
    rv_var[370:] = 1.2**2  # post-shock: realised vol far above its 90th percentile
    vh = pl.DataFrame(
        {
            "date": dates,
            "currency": ["BTC"] * n,
            "iv30": iv,
            "rv30_var": rv_var,
            "vrp": iv**2 - rv_var,
        }
    )
    ds = rule43.driver_series(vh)
    d = dict(zip(ds["date"], ds["driver"], strict=True))
    assert d[dates[360]] == "complacency" and d[dates[380]] == "both"
    assert rule43.driver_label(ds.filter(pl.col("date") == dates[380]).to_dicts()[0]).startswith(
        "post-shock"
    )
    fires = pl.DataFrame(
        {
            "rule_id": ["4.3"] * 3,
            "asset": ["BTC"] * 3,
            "fired": [True] * 3,
            "ts": [
                datetime.combine(dates[i], datetime.min.time(), tzinfo=UTC) for i in (355, 360, 395)
            ],
        }
    )
    px = pl.DataFrame(
        {
            "date": dates + [dates[-1] + timedelta(days=i) for i in range(1, 40)],
            "base": ["BTC"] * (n + 39),
            "close": list(100 * np.exp(np.cumsum(0.01 * rng.standard_normal(n + 39)))),
        }
    )
    fl = rule43.classify_flags(fires, vh, px, dates[-1] + timedelta(days=39))
    assert fl.height == 3 and fl["episode"].to_list() == [1, 1, 2]  # 355 and 360 share an episode
    tab = rule43.driver_table(fl)
    assert tab.filter(pl.col("driver") == "all")["n_episodes"][0] == 2


def test_fragility_validation_shocks_and_outcomes():
    start = date(2024, 1, 1)
    n = 500
    rng = np.random.default_rng(2)
    r = 0.01 * rng.standard_normal(n)
    r[400] = -0.15  # one clear shock
    close = 100 * np.exp(np.cumsum(r))
    btc = pl.DataFrame({"date": [start + timedelta(days=i) for i in range(n)], "close": close})
    sd = fv.shock_days(btc)
    assert bool(sd["shock"][400]) and sd["shock"].fill_null(False).sum() < 40
    frag = pl.DataFrame(
        {
            "date": btc["date"],
            "phi": np.linspace(-1, 1, n),
            "n_components": [5] * n,
            **{c: np.zeros(n) for c in fv.COMPONENTS},
        }
    )
    ev = fv.event_table(btc, frag, start + timedelta(days=n + 10))
    assert ev.height >= 1 and ev.filter(pl.col("date") == btc["date"][400]).height == 1
    row = ev.filter(pl.col("date") == btc["date"][400]).to_dicts()[0]
    assert (
        row["mdd_5"] <= close[400] / close[399] - 1 + 1e-12 and row["days_to_recover"] is not None
    )
    plc = fv.placebo_table(btc, frag, ev, start + timedelta(days=n + 10), k_per_event=3)
    assert plc.height >= 3 and not set(plc["date"]) & set(ev["date"])
    reg = fv.regressions(ev, "shocks")
    assert set(reg["outcome"]) == set(fv.OUTCOMES) and reg.height == 4 * 6
    t, _edges = fv.terciles(ev, "shocks")
    assert t.height == 3 and t["n"].sum() == ev.height
    # Newey–West slope recovers a planted linear relation
    x = np.linspace(-2, 2, 200)
    y = 3.0 * x + 0.1 * rng.standard_normal(200)
    s = fv.newey_west_slope(y, x)
    assert abs(s["slope"] - 3.0) < 0.1 and s["se"] < 0.1


def test_robust_z_degenerate_window_is_undefined():
    hist = np.array([0.01] * 240 + [0.010001, 0.009999] * 5)  # near-constant window
    z, n = robust_z(np.append(hist, 3.9), 250)
    assert z is None and n == 250
    ok = np.random.default_rng(3).standard_normal(300)
    z2, _ = robust_z(np.append(ok, 2.0), 250)
    assert z2 is not None and 1.0 < z2 < 3.0


def test_job_write_sets_are_disjoint_and_checker_rejects_foreign_files():
    cfg = yaml.safe_load((ROOT / "config" / "job_writes.yaml").read_text())
    seen = {}
    for job, spec in cfg.items():
        for t in spec["tables"]:
            assert t not in seen or job == "backfill" or seen[t] == "backfill", (
                f"{t} written by {seen.get(t)} and {job}"
            )
            seen.setdefault(t, job)
        for f in spec.get("site", []):
            assert f not in seen or seen[f] == job, f"{f} in two sets"
            seen[f] = job
    chk = ROOT / "scripts" / "check_write_set.py"
    ok = subprocess.run(
        [
            sys.executable,
            str(chk),
            "hourly",
            "--files",
            "data/processed/positioning.parquet",
            "site/data/hourly.json",
            "data/raw/2026/09/08/x_0100.json.gz",
        ],
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0, ok.stderr
    bad = subprocess.run(
        [sys.executable, str(chk), "hourly", "--files", "data/processed/universe.parquet"],
        capture_output=True,
        text=True,
    )
    assert bad.returncode == 1 and "universe" in bad.stderr


def test_no_workflow_or_script_auto_resolves_merges():
    pat = re.compile(r"-X (theirs|ours)|--strategy-option[= ](theirs|ours)|-X(theirs|ours)")
    for p in list((ROOT / ".github" / "workflows").glob("*.yml")) + list(
        (ROOT / "scripts").glob("*.sh")
    ):
        if p.name == "ci.yml":  # holds the grep pattern that enforces this rule
            continue
        assert not pat.search(p.read_text()), f"{p} auto-resolves merges"
    for wf in ("hourly", "daily", "weekly", "manual"):
        text = (ROOT / ".github" / "workflows" / f"{wf}.yml").read_text()
        assert "group: data-write" in text and "check_write_set.py" in text
