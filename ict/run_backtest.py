"""Entry point.

    python3 -m ict.run_backtest --verify          # phase 1: one month (config verify.month), plots trades
    python3 -m ict.run_backtest --full            # phase 2: all variants (only after the plots are confirmed)

All parameters come from ict/config.yaml.
"""
import argparse
import itertools
import os
import time

import numpy as np
import pandas as pd
import yaml

from .backtester import build, day_signals, execute, session_days
from .data_loader import load_bars

MIN = 60_000_000_000


def wilson(k, n, z=1.96):
    if n == 0:
        return np.nan, np.nan
    p = k / n
    d = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / d
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return mid - half, mid + half


def summarize(t):
    n = len(t)
    w = int((t.outcome == "win").sum()) if n else 0
    l = int((t.outcome == "loss").sum()) if n else 0
    be = int((t.outcome == "breakeven").sum()) if n else 0
    sc = int((t.outcome == "scratch").sum()) if n else 0
    te = int((t.exit_reason == "time").sum()) if n else 0
    lo, hi = wilson(w, w + l)
    lo2, hi2 = wilson(w, n)
    gp = t.r[t.r > 0].sum() if n else 0
    gl = -t.r[t.r < 0].sum() if n else 0
    return dict(trades=n, wins=w, losses=l, breakevens=be, scratches=sc, time_exits=te,
                win_rate=w / (w + l) if w + l else np.nan, wr_ci_lo=lo, wr_ci_hi=hi,
                win_rate_all=w / n if n else np.nan, wr_all_ci_lo=lo2, wr_all_ci_hi=hi2,
                expectancy_r=t.r.mean() if n else np.nan, profit_factor=gp / gl if gl > 0 else np.nan,
                net_usd=t.pnl_usd.sum() if n else 0.0)


def load_all(cfg, start=None, end=None):
    print("loading NQ bars...", flush=True)
    bars = load_bars(cfg, "nq", start, end)
    es = None
    try:
        print("loading ES 1m for SMT...", flush=True)
        es = load_bars(cfg, "es", start, end)["1min"]
    except Exception as e:  # noqa
        print("  no ES data:", e)
    return bars, es


def to_frame(trades):
    t = pd.DataFrame(trades)
    if t.empty:
        return t
    for c in ("touch_t", "signal_t", "entry_t", "exit_t", "leg_start_t"):
        t[c] = pd.to_datetime(t[c])
    t["direction_s"] = np.where(t.direction == 1, "long", "short")
    return t


def verify(cfg):
    month = pd.Period(cfg["verify"]["month"])
    start = (month.start_time - pd.Timedelta(days=150)).strftime("%Y-%m-%d")
    end = (month.end_time + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    bars, es = load_all(cfg, start, end)
    st = build(bars, cfg, es)
    log = []
    days = [d for d in session_days(st, cfg, log) if d.to_period("M") == month]
    print(f"{month}: {len(days)} session days; skipped (whole loaded range): {len(log)}")
    os.makedirs("results/ict/verify", exist_ok=True)
    pd.DataFrame(log, columns=["day", "reason"]).to_csv("results/ict/verify/skipped_days.csv", index=False)
    for d, why in log:
        if pd.Timestamp(d).to_period("M") == month:
            print(f"  skipped {d}: {why}")
    mv = cfg["main_variant"]
    sigs, infos = {}, []
    for d in days:
        s, info = day_signals(d, st, cfg, mv["level_types"], mv["ifg_rule"], mv["bias_rule"])
        infos.append(info)
        if info["bias"] == 0:
            print(f"  {d.date()} no-trade day (bias score {info['bias_score']:+d})")
        sigs[d] = s
    ec = {k: mv[k] for k in ("entry", "stop", "target", "breakeven", "max_trades_per_day", "hard_exit")}
    trades, skips = execute(sigs, st, cfg, ec)
    t = to_frame(trades)
    out = "results/ict/verify"
    os.makedirs(out, exist_ok=True)
    pd.DataFrame(infos).to_csv(f"{out}/days_{month}.csv", index=False)
    if t.empty:
        print("no trades in the verification month", skips)
        return
    # leg-extreme time for plotting (bar where the extreme was made, between touch and signal)
    m1 = st["m1"]
    ext_t = []
    for r in t.itertuples():
        seg = m1.loc[r.touch_t: r.signal_t - pd.Timedelta(minutes=1)]
        col = "low" if r.direction == 1 else "high"
        ext_t.append(seg[col].idxmin() if r.direction == 1 else seg[col].idxmax())
    t["leg_ext_t"] = ext_t
    t.to_csv(f"{out}/trades_{month}.csv", index=False)
    print(t[["day", "direction_s", "level_type", "level_tf", "ifg_tf", "signal_t", "entry", "stop", "target",
             "exit_reason", "outcome", "r", "smt"]].to_string(index=False))
    print("skips:", skips)
    print("summary:", {k: (round(v, 3) if isinstance(v, float) else v) for k, v in summarize(t).items()})
    from .plot_trades import plot_trade
    pick = t.sort_values("signal_t").iloc[np.linspace(0, len(t) - 1, min(cfg["verify"]["n_plots"], len(t))).astype(int)]
    for i, r in enumerate(pick.to_dict("records")):
        p = f"{out}/trade_{i + 1}_{r['day']}.png"
        plot_trade(r, m1, p)
        print("plot:", p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="ict/config.yaml")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--full", action="store_true")
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.config))
    if a.verify:
        verify(cfg)
    elif a.full:
        raise SystemExit("full run is gated until the verification plots are confirmed (see README)")


if __name__ == "__main__":
    main()
