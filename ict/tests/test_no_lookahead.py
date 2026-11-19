"""Truncation invariance: every decision must be identical when all data after the decision time is
removed. Bias is checked against data cut at 09:25; every signal against data cut at its signal time."""
import copy

import numpy as np
import pandas as pd
import pytest
import yaml

from ict.backtester import build, day_signals, session_days
from ict.data_loader import resample, TF_ORDER
from ict.tests.helpers import synthetic_1m

KEYS = ("signal_t", "entry_ref", "ifg_tf", "ifg_bot", "ifg_top", "leg_extreme", "leg_far", "level_type",
        "level_tf", "level_zb", "level_zt", "bias_score", "direction")


def bars_from(df):
    out = {}
    for tf in TF_ORDER:
        if tf == "30s":
            continue
        if tf == "1min":
            b = df.copy()
            b.index.name = "t0"
            b["t1"] = b.index + pd.Timedelta("1min")
        else:
            b = resample(df, tf)
        out[tf] = b
    return out


@pytest.fixture(scope="module")
def setup():
    cfg = yaml.safe_load(open("ict/config.yaml"))
    cfg["bias"]["threshold"] = 1                     # random data rarely scores 2; still exercises everything
    cfg["levels"]["lookback_days"] = 3
    df = synthetic_1m(days=40)
    st = build(bars_from(df), cfg, None, verbose=False)
    log = []
    days = session_days(st, cfg, log)[15:]           # leave history for the HTF lookbacks
    return cfg, df, st, days


def sig_key(s):
    return tuple(s[k] for k in KEYS)


def test_signals_do_not_depend_on_future_bars(setup):
    cfg, df, st, days = setup
    checked = 0
    for d in days:
        sigs, info = day_signals(d, st, cfg, "all", "highest", "threshold")
        # bias: recompute with data cut at 09:25
        cut_b = df[df.index < d + pd.Timedelta("09:25:00")]
        st_b = build(bars_from(cut_b), cfg, None, verbose=False)
        _, info_b = day_signals(d, st_b, cfg, "all", "highest", "threshold")
        assert (info["bias"], info["bias_score"]) == (info_b["bias"], info_b["bias_score"])
        for s in sigs:
            cut = df[df.index < pd.Timestamp(s["signal_t"])]
            st_c = build(bars_from(cut), cfg, None, verbose=False)
            sigs_c, _ = day_signals(d, st_c, cfg, "all", "highest", "threshold")
            assert sig_key(s) in [sig_key(x) for x in sigs_c], f"signal at {pd.Timestamp(s['signal_t'])} changed"
            checked += 1
            if checked >= 12:
                break
        if checked >= 12:
            break
    print(f"lookahead test: {checked} signals re-derived from truncated data")
    assert checked >= 5, "synthetic data produced too few signals to check"
