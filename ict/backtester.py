"""Signals (bias -> key level touch -> manipulation leg -> IFG) and execution with daily limits.

Phase A  build(): bars for every TF, FVGs with status, 1m pivots, key levels, IFG inversions (vectorized).
Phase B  day_signals(): per trading day, using only data closed by each decision time.
Phase C  execute(): one execution config over the day's signals, with costs and daily limits.
"""
import time

import numpy as np
import pandas as pd

from .bias import daily_bias, draw_on_liquidity
from .fvg import BIG, add_status, detect_fvgs, ns
from .ifg import find_signal, precompute_inversions
from .levels import build_levels
from .swings import pivots

MIN = 60_000_000_000


def hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


# ------------------------------------------------------------------ phase A
def build(bars, cfg, es_m1=None, verbose=True):
    t = time.time()
    m1 = bars["1min"]
    need = set(cfg["levels"]["timeframes"]) | set(cfg["bias"]["timeframes"]) | set(cfg["ifg"]["timeframes"])
    if cfg["data"].get("use_30s") and "30s" in bars:
        need.add("30s")
    fvgs = {}
    for tf in sorted(need, key=lambda x: list(bars).index(x)):
        f = detect_fvgs(bars[tf])
        fvgs[tf] = add_status(f, bars[tf], m1)
        if verbose:
            print(f"  FVGs {tf:>5s}: {len(f):>8,d}", flush=True)
    ifg_tfs = [tf for tf in cfg["ifg"]["timeframes"]]
    if cfg["data"].get("use_30s") and "30s" in bars:
        ifg_tfs = ["30s"] + ifg_tfs
    ifg_f = {tf: precompute_inversions(bars[tf], fvgs[tf]) for tf in ifg_tfs}
    n = cfg["swings"]["n"]
    piv = pivots(m1["high"].values, m1["low"].values, n)
    levels = build_levels(bars, fvgs, m1, cfg, verbose)
    m1a = dict(t0=ns(m1.index), t1=ns(m1["t1"]), open=m1["open"].values, high=m1["high"].values,
               low=m1["low"].values, close=m1["close"].values)
    st = dict(bars=bars, fvgs=fvgs, ifg_f=ifg_f, ifg_tfs=ifg_tfs, piv=piv, levels=levels, m1=m1, m1a=m1a,
              es=None)
    if es_m1 is not None:
        st["es"] = dict(t0=ns(es_m1.index), low=es_m1["low"].values, high=es_m1["high"].values,
                        piv=pivots(es_m1["high"].values, es_m1["low"].values, n))
    # 5m pivots for structural targets
    b5 = bars[cfg["execution"]["structural_tf"]]
    st["piv5"] = pivots(b5["high"].values, b5["low"].values, n)
    st["b5"] = dict(t1=ns(b5["t1"]), high=b5["high"].values, low=b5["low"].values)
    if verbose:
        print(f"  build done in {time.time() - t:.0f}s: {len(levels):,d} key levels", flush=True)
    return st


# ------------------------------------------------------------------ phase B
def session_days(st, cfg, log):
    """Weekdays whose 09:30-11:00 1m window is (nearly) complete; others are logged with a reason."""
    m1 = st["m1"]
    idx = m1.index
    mins = idx.hour * 60 + idx.minute
    s, e = hm(cfg["session"]["entry_start"]), hm(cfg["session"]["entry_end"])
    win = idx[(mins >= s) & (mins < e)]
    counts = pd.Series(1, index=win.normalize()).groupby(level=0).sum()
    days = []
    all_days = pd.date_range(idx.min().normalize(), idx.max().normalize(), freq="B")
    for d in all_days:
        c = int(counts.get(d, 0))
        if c == 0:
            log.append((d.date(), "no 09:30-11:00 data (holiday/closed or missing)"))
        elif c < cfg["data"]["min_session_bars"]:
            log.append((d.date(), f"missing data: {c}/90 1m bars in 09:30-11:00"))
        else:
            days.append(d)
    return days


def smt_flag(st, direction, touch_t, signal_t, n):
    """Bullish SMT: ES traded below its most recent confirmed 1m swing low (before the touch) during
    [touch, signal] while NQ did not trade below its own. Bearish mirrors. None if ES missing."""
    es = st["es"]
    if es is None:
        return None
    out = []
    for arr in (st["m1a"], es):
        t0 = arr["t0"]
        k = np.searchsorted(t0, touch_t)
        j = np.searchsorted(t0, signal_t, side="left")             # bars that opened before the signal close
        if k >= len(t0) or j <= k:
            return None
        sh, sl = arr.get("piv") if "piv" in arr else st["piv"]
        idx = sl if direction == 1 else sh
        cfm = idx[idx + n <= k]
        if not len(cfm):
            return None
        ref = (arr["low"] if direction == 1 else arr["high"])[cfm[-1]]
        ext = arr["low"][k:j].min() if direction == 1 else arr["high"][k:j].max()
        out.append(ext < ref if direction == 1 else ext > ref)
    nq_swept, es_swept = out
    return bool(es_swept and not nq_swept)


def day_signals(day, st, cfg, level_types, ifg_rule, bias_rule):
    """All candidate signals for one day (before execution limits)."""
    m1a = st["m1a"]
    d0 = pd.Timestamp(day).value
    bias_ns = d0 + hm(cfg["session"]["bias_time"]) * MIN
    s_ns, e_ns = d0 + hm(cfg["session"]["entry_start"]) * MIN, d0 + hm(cfg["session"]["entry_end"]) * MIN
    bcfg = dict(cfg)
    bcfg["bias"] = dict(cfg["bias"], require_all_three=(bias_rule == "all_three"))
    direction, score, per_tf = daily_bias(st["bars"], st["fvgs"], bias_ns, bcfg)
    info = dict(day=pd.Timestamp(day).date(), bias=direction, bias_score=score, **{f"score_{k}": v for k, v in per_tf.items()})
    if direction == 0:
        return [], info
    dol = draw_on_liquidity(st["bars"], st["m1"], bias_ns, direction, cfg)
    info["dol"] = None if dol is None else dol["price"]
    lv = st["levels"]
    types = {"fvg", "fvg_inter"} if level_types == "fvg_only" else {"fvg", "fvg_inter", "cisd", "rb"}
    min_formed = d0 - (cfg["levels"]["lookback_days"] + 2) * 1440 * MIN   # + weekend allowance (calendar)
    L = lv[(lv.dir.values == direction) & lv.type.isin(types).values & (lv.active_t.values < e_ns)
           & (lv.first_touch_t.values >= s_ns) & (lv.formed_t.values >= min_formed)]
    if L.empty:
        return [], info
    t0 = m1a["t0"]
    a, b = np.searchsorted(t0, s_ns), np.searchsorted(t0, e_ns)
    sigs = []
    touched = set()
    ft, act, zb, zt = L.first_touch_t.values, L.active_t.values, L.zb.values, L.zt.values
    for k in range(a, b):
        hit = np.where(ft == t0[k])[0]
        if not len(hit):
            continue
        prev_c = m1a["close"][k - 1]
        live = (act <= t0[k]) & (ft >= t0[k])                       # known, not touched before this bar
        # the level must not already be broken (price beyond its far edge); inside the zone = distance 0
        live &= (prev_c >= zb) if direction == 1 else (prev_c <= zt)
        dist = np.maximum((prev_c - zt) if direction == 1 else (zb - prev_c), 0.0)
        dd = np.where(live, dist, np.inf)
        top = set(np.argsort(dd, kind="stable")[:cfg["levels"]["max_active"]].tolist())
        ok = [x for x in hit if x in top and live[x]]
        if not ok:
            continue
        h = min(ok, key=lambda x: dist[x])                         # closest touched level owns the setup
        row = L.iloc[h]
        sig = find_signal(k, direction, m1a, st["piv"], cfg["swings"]["n"], st["ifg_f"], st["ifg_tfs"],
                          ifg_rule, e_ns)
        if sig is None:
            continue
        sig.update(level_id=int(row.id), level_type=("cisd+fvg" if row.type == "cisd" and row.cisd_with_fvg
                                                      else row.type), level_tf=row.tf, level_zb=row.zb,
                   level_zt=row.zt, level_active_t=int(row.active_t), touch_t=int(t0[k]), direction=direction,
                   bias_score=score, dol=info["dol"], day=info["day"])
        sig["smt"] = smt_flag(st, direction, t0[k], sig["signal_t"], cfg["swings"]["n"])
        sigs.append(sig)
    # several levels can share one leg -> identical signal; keep one per signal time, attributed to the
    # most recently touched (deepest) level
    uniq = {}
    for s in sorted(sigs, key=lambda x: x["touch_t"]):
        uniq[s["signal_t"]] = s
    return sorted(uniq.values(), key=lambda x: x["signal_t"]), info


# ------------------------------------------------------------------ phase C
def structural_target(st, sig, entry, risk, cfg):
    ex = cfg["execution"]
    n = cfg["swings"]["n"]
    b5 = st["b5"]
    last = np.searchsorted(b5["t1"], sig["signal_t"], side="right") - 1
    sh, sl = st["piv5"]
    d = sig["direction"]
    idx = sh if d == 1 else sl
    idx = idx[idx + n <= last]
    if not len(idx):
        return None
    px = (b5["high"] if d == 1 else b5["low"])[idx]
    m1a = st["m1a"]
    best = None
    for i, p in sorted(zip(idx, px), key=lambda z: abs(z[1] - entry)):
        if (d == 1 and p <= entry) or (d == -1 and p >= entry):
            continue
        a = np.searchsorted(m1a["t0"], b5["t1"][i])
        z = np.searchsorted(m1a["t0"], sig["signal_t"])
        seg = m1a["high"][a:z] if d == 1 else m1a["low"][a:z]
        if len(seg) and ((d == 1 and seg.max() > p) or (d == -1 and seg.min() < p)):
            continue                                                  # already swept
        best = p
        break
    if best is None:
        return None
    r = abs(best - entry) / risk
    return best if ex["structural_min_r"] <= r <= ex["structural_max_r"] else None


def simulate(st, sig, ec, cfg):
    """Returns a trade dict or a skip reason string."""
    m1a = st["m1a"]
    tick = cfg["data"]["tick"]
    slip = cfg["costs"]["slippage_ticks"] * tick
    d = sig["direction"]
    j = sig["signal_i"]
    d0 = pd.Timestamp(sig["day"]).value
    exit_ns = d0 + hm(ec["hard_exit"]) * MIN
    leg_stop = sig["leg_extreme"] - d * tick
    gap_stop = (sig["ifg_bot"] if d == 1 else sig["ifg_top"]) - d * tick
    stop = leg_stop if ec["stop"] == "leg" else gap_stop
    t0, o, h, l, c = m1a["t0"], m1a["open"], m1a["high"], m1a["low"], m1a["close"]
    if ec["entry"] == "market":
        entry = sig["entry_ref"] + d * slip
        start = j + 1
        fill_i = j
    else:
        lim = sig["ifg_top"] if d == 1 else sig["ifg_bot"]           # near edge after the close through
        fill_i = None
        for q in range(j + 1, min(len(t0), j + 1 + cfg["execution"]["limit_expiry_bars"])):
            if t0[q] >= exit_ns:
                break
            if (d == 1 and l[q] <= stop) or (d == -1 and h[q] >= stop):
                if (d == 1 and l[q] <= lim) or (d == -1 and h[q] >= lim):
                    fill_i = q                                       # filled and stopped in the same bar
                break
            if (d == 1 and l[q] <= lim) or (d == -1 and h[q] >= lim):
                fill_i = q
                break
        if fill_i is None:
            return "limit_not_filled"
        entry = lim
        start = fill_i
    risk = (entry - stop) * d
    if risk <= 0:
        return "stop_not_beyond_entry"
    if risk > cfg["execution"]["max_risk"]:
        return "risk_over_max"
    if ec["target"] == "structural":
        tgt = structural_target(st, sig, entry, risk, cfg)
        if tgt is None:
            return "no_structural_target"
    else:
        tgt = entry + d * float(ec["target"]) * risk
    cur_stop, be_armed, be_on = stop, ec["breakeven"], False
    why, px, xi = None, None, None
    q = start
    while q < len(t0) and t0[q] < exit_ns:
        if (d == 1 and l[q] <= cur_stop) or (d == -1 and h[q] >= cur_stop):
            fillp = min(cur_stop, o[q]) if d == 1 else max(cur_stop, o[q])
            if q == fill_i and ec["entry"] == "limit":
                fillp = cur_stop
            why, px, xi = ("breakeven" if be_on else "stop"), fillp - d * slip, q
            break
        if q != fill_i and ((d == 1 and h[q] >= tgt) or (d == -1 and l[q] <= tgt)):
            why, px, xi = "target", tgt, q
            break
        if be_armed and not be_on and ((d == 1 and h[q] >= entry + cfg["execution"]["be_trigger_r"] * risk) or
                                       (d == -1 and l[q] <= entry - cfg["execution"]["be_trigger_r"] * risk)):
            be_on, cur_stop = True, entry                              # effective from the next bar
        q += 1
    if why is None:
        xi = max(start, min(q, len(t0)) - 1)
        why, px = "time", c[xi] - d * slip
    pv = cfg["costs"]["point_value"]
    pnl = (px - entry) * d * pv - cfg["costs"]["commission_rt"]
    r = pnl / (risk * pv)
    if why == "target":
        outcome = "win" if pnl > 0 else "loss"
    elif why == "stop":
        outcome = "loss"
    elif why == "breakeven":
        outcome = "breakeven"
    else:
        tw = cfg["classification"]["time_exit_win_r"]
        outcome = "win" if r >= tw else ("scratch" if r > 0 else "loss")
    return dict(entry_t=int(t0[fill_i] + MIN) if ec["entry"] == "market" else int(t0[fill_i]),
                entry=entry, stop=stop, target=tgt, risk_pts=risk, exit_t=int(t0[xi] + MIN), exit=px,
                exit_reason=why, pnl_usd=pnl, r=r, outcome=outcome)


def execute(signals_by_day, st, cfg, ec):
    """Daily limits: max trades/day; stop after 1 win; after 1 loss a second trade only from a different key
    level; stop after 2 losses; one trade at a time. Break-evens and scratches count as neither."""
    trades, skips = [], {}
    for day, sigs in signals_by_day.items():
        wins = losses = n = 0
        free_at = 0
        lost_levels = set()
        for s in sigs:
            if n >= ec["max_trades_per_day"] or wins >= 1 or losses >= 2:
                break
            if s["signal_t"] < free_at:
                skips["trade_open"] = skips.get("trade_open", 0) + 1
                continue
            if losses >= 1 and s["level_id"] in lost_levels:
                skips["same_level_after_loss"] = skips.get("same_level_after_loss", 0) + 1
                continue
            res = simulate(st, s, ec, cfg)
            if isinstance(res, str):
                skips[res] = skips.get(res, 0) + 1
                continue
            tr = {**{k: s[k] for k in ("day", "direction", "level_type", "level_tf", "level_id", "level_zb",
                                      "level_zt", "ifg_tf", "ifg_bot", "ifg_top", "bias_score", "smt", "dol",
                                      "touch_t", "signal_t", "leg_far", "leg_extreme", "leg_start_t",
                                      "level_active_t", "ifg_known_t")}, **res}
            trades.append(tr)
            n += 1
            wins += tr["outcome"] == "win"
            if tr["outcome"] == "loss":
                losses += 1
                lost_levels.add(s["level_id"])
            free_at = tr["exit_t"]
    return trades, skips
