"""Add-on confluence study: baseline vs OTE, IFVG (A/B) and rejection-block variants.

Run after prep_data.py:   python3 addons.py [--dev-only]
Outputs go to results/addons/.

All rules and parameters below were fixed BEFORE any results were seen and were not tuned.
Development window 2010-11..2020-12, confirmation (hold-out) 2021-01..2026-11.
`--dev-only` restricts the run to the development window (used once for bug-checking);
the full run is then made once and the hold-out numbers are reported as they came out.

Re-uses the data, resampling, 1H bias, FVG detection, fill logic, session windows and
1m exit simulator from backtest.py so the baseline is identical across studies.
"""
import os
import sys
import numpy as np
import pandas as pd
from numba import njit

from backtest import (TICK, POINT_VALUE, COMMISSION_RT, SLIP_TICKS, MAX_TRADES, FVG5_MAX_AGE_BARS,
                      SESSIONS, M, load_1m, build_context, session_windows, _simulate_exit, max_dd)

OUT = "results/addons"
SLIP = SLIP_TICKS * TICK
AGE = FVG5_MAX_AGE_BARS          # every zone (FVG, IFVG, block) expires 1440 5m bars after it becomes valid
SWING_N = 2                      # fractal swing: extreme of 2 bars either side; confirmed 2 bars later
OTE_LO, OTE_HI = 0.62, 0.79      # retracement band of the impulse leg
IFVG_B_WINDOW = 12               # IFVG-B: new FVG's first candle within 12 bars (1h) after the inversion close
RB_MIN_WICK = 0.5                # rejection block: wick >= 50% of candle range
DEV_END = pd.Timestamp("2021-01-01")
RRS = (2, 3)


# ------------------------------------------------------------------ zone builders
# A "zone" is a price band [bot, top] that a later 5m bar can tap.
#   dir   : trade direction it supports (+1 long, -1 short)
#   vbar  : 5m bar index at whose close the zone becomes known; taps allowed from vbar+1
#   dead_t: first 1m time (ns) at which the zone is invalidated; tap bar k needs dead_t >= close(k)
#   far   : far edge (the "original" FVG stop): bot for longs, top for shorts

def fvg_zones(f5):
    z = pd.DataFrame({"dir": f5["dir"].values, "bot": f5["bot"].values, "top": f5["top"].values,
                      "vbar": f5["bar"].values, "dead_t": f5["fill_t"].values})
    z["far"] = np.where(z.dir == 1, z.bot, z.top)
    z["c1"] = z.vbar - 2                         # first candle of the 3-candle pattern
    return z


@njit(cache=True)
def _first_close_through(close, vbar, direction, bot, top, age):
    """Bar index of the first 5m CLOSE beyond the far side of an FVG (inversion), or -1."""
    out = np.full(len(vbar), -1)
    n = len(close)
    for i in range(len(vbar)):
        for j in range(vbar[i] + 1, min(n, vbar[i] + 1 + age)):
            if direction[i] == 1 and close[j] < bot[i]:
                out[i] = j
                break
            if direction[i] == -1 and close[j] > top[i]:
                out[i] = j
                break
    return out


@njit(cache=True)
def _dead_time(t1m, lo, hi, start_idx, direction, edge, limit_t):
    """First 1m bar time at/after start where price trades to/beyond `edge`
    (long zone: low <= edge, short zone: high >= edge). Scan stops at limit_t (zone expiry)."""
    n = len(t1m)
    res = np.full(len(start_idx), np.iinfo(np.int64).max)
    for k in range(len(start_idx)):
        s = start_idx[k]
        if s >= n:
            continue
        limit = limit_t[k]
        for j in range(s, n):
            if t1m[j] > limit:
                break
            if (direction[k] == 1 and lo[j] <= edge[k]) or (direction[k] == -1 and hi[j] >= edge[k]):
                res[k] = t1m[j]
                break
    return res


def dead_times(m1, m5, vbar, direction, edge):
    """Expiry uses the same bar clock as the engine: scan up to the close of bar vbar + AGE."""
    t = m1.index.values.astype(np.int64)
    t1b = m5["t1"].values.astype(np.int64)
    start = np.searchsorted(t, t1b[vbar])
    limit_t = t1b[np.minimum(vbar + AGE, len(t1b) - 1)]
    return _dead_time(t, m1["low"].values, m1["high"].values, start, direction.astype(np.int64),
                      edge.astype(np.float64), limit_t.astype(np.int64))


def inversions(m5, f5):
    """Inversion events: a 5m close through the far side of an FVG.
    Returns the FVG table with column inv_bar (-1 if never inverted within AGE bars)."""
    f = f5.copy()
    f["inv_bar"] = _first_close_through(m5["close"].values, f["bar"].values, f["dir"].values.astype(np.int64),
                                        f["bot"].values, f["top"].values, AGE)
    return f


def tapped_older_same_dir(m5, f5):
    """For each FVG: did the move that created it start by tapping an older, still-unfilled FVG of the
    SAME direction? (bullish FVG whose candle-1/2 low touched an older bullish FVG = 'price dropped,
    tapped an FVG and bounced'.)"""
    lo, hi, t0 = m5["low"].values, m5["high"].values, m5.index.values.astype(np.int64)
    bar, d, bot, top, fill = (f5[c].values for c in ("bar", "dir", "bot", "top", "fill_t"))
    out = np.zeros(len(f5), bool)
    for i in range(len(f5)):
        c1 = bar[i] - 2
        lo_i = np.searchsorted(bar, c1 - AGE)
        hi_i = np.searchsorted(bar, c1)            # older FVGs completed before candle 1 opened
        if hi_i <= lo_i:
            continue
        s = slice(lo_i, hi_i)
        same = d[s] == d[i]
        alive = fill[s] >= t0[c1]
        if d[i] == 1:
            touch = min(lo[c1], lo[c1 + 1]) <= top[s]
        else:
            touch = max(hi[c1], hi[c1 + 1]) >= bot[s]
        out[i] = np.any(same & alive & touch)
    return out


def ifvg_a_zones(m1, m5, finv, pre_tap):
    """IFVG-A. Shorts: a bullish FVG (created by a bounce that first tapped an older bullish FVG)
    is closed through to the downside -> bearish IFVG [bot, top]; short on a retest.
    Longs mirror. Zone dies when price trades through its far side (above top for shorts)."""
    sel = (finv.inv_bar >= 0) & pre_tap
    f = finv[sel]
    z = pd.DataFrame({"dir": -f["dir"].values, "bot": f["bot"].values, "top": f["top"].values,
                      "vbar": f["inv_bar"].values})
    z["far"] = np.where(z.dir == 1, z.bot, z.top)
    z["dead_t"] = dead_times(m1, m5, z.vbar.values, z.dir.values, z.far.values)
    z["c1"] = z.vbar
    return z.sort_values("vbar").reset_index(drop=True)


def ifvg_b_zones(f5z, finv):
    """IFVG-B. Shorts: an existing bullish FVG is closed through (inversion at bar j); then a NEW
    bearish FVG whose first candle opens at bar j..j+12 is the entry zone. Longs mirror."""
    keep = np.zeros(len(f5z), bool)
    for d in (1, -1):
        inv_bars = np.sort(finv.loc[(finv.dir == -d) & (finv.inv_bar >= 0), "inv_bar"].values)
        idx = np.where(f5z.dir.values == d)[0]
        c1 = f5z.c1.values[idx]
        pos = np.searchsorted(inv_bars, c1, side="right") - 1       # latest inversion at or before candle 1
        ok = pos >= 0
        ok[ok] = c1[ok] - inv_bars[pos[ok]] <= IFVG_B_WINDOW
        keep[idx[ok]] = True
    return f5z[keep].reset_index(drop=True)


def swings(m5):
    h, l = m5["high"].values, m5["low"].values
    n, N = len(h), SWING_N
    sh = np.zeros(n, bool)
    sl = np.zeros(n, bool)
    for s in range(N, n - N):
        if h[s] > h[s - N:s].max() and h[s] >= h[s + 1:s + N + 1].max():
            sh[s] = True
        if l[s] < l[s - N:s].min() and l[s] <= l[s + 1:s + N + 1].min():
            sl[s] = True
    return np.where(sh)[0], np.where(sl)[0]


def rb_zones(m1, m5, f5, sh_idx, sl_idx):
    """Rejection blocks. Longs: confirmed swing-low candle whose lower wick >= 50% of its range.
    Block = [low, min(open, close)]. Must sit fully inside an unfilled bullish FVG at confirmation.
    Dies when price trades below the wick tip. Shorts mirror on swing highs / upper wicks."""
    o, h, l, c = (m5[x].values for x in ("open", "high", "low", "close"))
    t1 = m5["t1"].values.astype(np.int64)
    fbar, fd, fbot, ftop, ffill = (f5[x].values for x in ("bar", "dir", "bot", "top", "fill_t"))
    rows = []
    for d, idxs in ((1, sl_idx), (-1, sh_idx)):
        for s in idxs:
            rng = h[s] - l[s]
            if rng <= 0:
                continue
            if d == 1:
                wick, zb, zt, tip = min(o[s], c[s]) - l[s], l[s], min(o[s], c[s]), l[s]
            else:
                wick, zb, zt, tip = h[s] - max(o[s], c[s]), max(o[s], c[s]), h[s], h[s]
            if wick < RB_MIN_WICK * rng:
                continue
            conf = s + SWING_N                     # known at the close of this bar
            if conf >= len(h):
                continue
            lo_i, hi_i = np.searchsorted(fbar, conf - AGE), np.searchsorted(fbar, conf, side="right")
            sl_ = slice(lo_i, hi_i)
            inside = (fd[sl_] == d) & (ffill[sl_] >= t1[conf]) & (fbot[sl_] <= zb) & (ftop[sl_] >= zt)
            if not inside.any():
                continue
            rows.append((d, zb, zt, conf, tip))
    z = pd.DataFrame(rows, columns=["dir", "bot", "top", "vbar", "tip"]).sort_values("vbar").reset_index(drop=True)
    z["far"] = z.tip - z.dir * TICK              # stop just beyond the wick tip
    # invalidated once price trades beyond the tip (the stop level)
    z["dead_t"] = dead_times(m1, m5, z.vbar.values, z.dir.values, z.far.values)
    z["c1"] = z.vbar
    return z


# ------------------------------------------------------------------ OTE filter
class OTE:
    """Most recent 5m impulse leg in the bias direction, using swings confirmed before the entry bar.
    Long: last confirmed swing high SH, and the last swing low SL before it; leg intact if no low since
    SH has broken SL. Zone = 62-79% retracement. FVG midpoint must lie inside it."""

    def __init__(self, m5, sh_idx, sl_idx):
        self.h, self.l = m5["high"].values, m5["low"].values
        self.sh, self.sl = sh_idx, sl_idx

    def zone(self, k, d):
        N = SWING_N
        a, b = (self.sh, self.sl) if d == 1 else (self.sl, self.sh)
        p = np.searchsorted(a + N, k - 1, side="right") - 1      # confirmed at bar <= k-1
        if p < 0:
            return None
        e = a[p]                                                 # leg end (swing high for longs)
        q = np.searchsorted(b, e) - 1                            # leg start before it
        if q < 0:
            return None
        s = b[q]
        if d == 1:
            hi, lo = self.h[e], self.l[s]
            if hi <= lo or self.l[e:k].min() <= lo:
                return None
            r = hi - lo
            return hi - OTE_HI * r, hi - OTE_LO * r
        lo, hi = self.l[e], self.h[s]
        if hi <= lo or self.h[e:k].max() >= hi:
            return None
        r = hi - lo
        return lo + OTE_LO * r, lo + OTE_HI * r


# ------------------------------------------------------------------ engines
def _trade_row(name, session, rr, sess_day, entry_t, exit_t, b, entry, raw_entry, stp, tgt, px, why, zone):
    risk = (entry - stp) * b
    pts = (px - entry) * b
    pnl = pts * POINT_VALUE - COMMISSION_RT
    raw_exit = px if why == 1 else px + b * SLIP
    return dict(variant=name, session=session, rr=rr, session_date=sess_day.date(), entry_time=entry_t,
                exit_time=exit_t, direction="long" if b == 1 else "short", entry=entry, stop=stp,
                target=tgt, exit=px, exit_reason=("stop", "target", "session_end")[why],
                zone_bot=zone[0], zone_top=zone[1], zone_known_at=zone[2], risk_pts=risk, pnl_pts=pts, pnl_usd=pnl,
                r=pnl / (risk * POINT_VALUE), gross_r=(raw_exit - raw_entry) * b / risk)


def run_tapclose(ctx, zones, name, session, rr, stop_type, ote=None, one_shot=False):
    """Tap-and-close entry: 5m bar touches a live zone in the bias direction and closes in the trade
    direction. stop_type 'zone' = zone far edge; 'candle' = entry candle wick extreme +/- 1 tick."""
    m1, m5, bias = ctx["m1"], ctx["m5"], ctx["bias"]
    t1m, o1, h1, l1, c1 = ctx["m1arr"]
    t0, t1 = ctx["t0"], ctx["t1"]
    o, h, l, c = ctx["ohlc"]
    mask, sess_day, sess_end = ctx["sess"][session]
    zd, zb, zt, zv, zdead, zfar = (zones[x].values for x in ("dir", "bot", "top", "vbar", "dead_t", "far"))
    used = np.zeros(len(zones), bool)
    trades, cur, n_today, next_ok = [], None, 0, np.int64(0)
    for k in np.where(mask & (bias != 0))[0]:
        if t1[k] >= sess_end[k]:
            continue
        if sess_day[k] != cur:
            cur, n_today = sess_day[k], 0
        if n_today >= MAX_TRADES or t0[k] < next_ok:
            continue
        b = bias[k]
        if (b == 1 and c[k] <= o[k]) or (b == -1 and c[k] >= o[k]):
            continue
        lo_i, hi_i = np.searchsorted(zv, k - AGE), np.searchsorted(zv, k)   # zones known before bar k
        if hi_i <= lo_i:
            continue
        s = slice(lo_i, hi_i)
        ok = (zd[s] == b) & (zdead[s] >= t1[k]) & np.where(b == 1, l[k] <= zt[s], h[k] >= zb[s])
        if one_shot:
            ok &= ~used[s]
        cand = np.where(ok)[0][::-1] + lo_i
        if len(cand) == 0:
            continue
        entry = c[k] + b * SLIP
        if ote is not None:
            zone = ote.zone(k, b)
            if zone is None:
                continue
            mid = (zb[cand] + zt[cand]) / 2
            cand = cand[(mid >= zone[0]) & (mid <= zone[1])]
        chosen = None
        for j in cand:
            stp = zfar[j] if stop_type == "zone" else (l[k] - TICK if b == 1 else h[k] + TICK)
            if (entry - stp) * b > 0:
                chosen = j
                break
        if chosen is None:
            continue
        used[chosen] = True
        risk = (entry - stp) * b
        tgt = entry + b * rr * risk
        i0 = np.searchsorted(t1m, t1[k])
        px, jx, why = _simulate_exit(t1m, o1, h1, l1, c1, i0, sess_end[k], b, stp, tgt, SLIP)
        trades.append(_trade_row(name, session, rr, sess_day[k], pd.Timestamp(t1[k]),
                                 pd.Timestamp(t1m[jx]) + pd.Timedelta(minutes=1), b, entry, c[k], stp, tgt,
                                 px, why, (zb[chosen], zt[chosen], pd.Timestamp(t1[zv[chosen]]))))
        n_today += 1
        next_ok = t1m[jx] + M
    return trades


def run_limit(ctx, zones, name, session, rr):
    """Rejection-block limit entry: a resting limit at the block edge (top for longs, bottom for shorts)
    while the bias agrees (bias known at the previous 5m close). Filled only when a 1m bar trades
    1 tick THROUGH the edge (touch alone is not assumed to fill). Stop = beyond wick tip.
    If the fill minute also reaches the stop -> stop. Target is not allowed in the fill minute."""
    bias = ctx["bias"]
    t1m, o1, h1, l1, c1 = ctx["m1arr"]
    t0, t1 = ctx["t0"], ctx["t1"]
    o, h, l, c = ctx["ohlc"]
    mask, sess_day, sess_end = ctx["sess"][session]
    zd, zb, zt, zv, zdead, zfar = (zones[x].values for x in ("dir", "bot", "top", "vbar", "dead_t", "far"))
    used = np.zeros(len(zones), bool)
    trades, cur, n_today, next_ok = [], None, 0, np.int64(0)
    for k in np.where(mask)[0]:
        if k == 0 or t0[k] >= sess_end[k]:
            continue
        if sess_day[k] != cur:
            cur, n_today = sess_day[k], 0
        b = bias[k - 1]                                 # state known when the bar opens
        if b == 0 or n_today >= MAX_TRADES or t1[k] <= next_ok:
            continue
        lo_i, hi_i = np.searchsorted(zv, k - AGE), np.searchsorted(zv, k)
        if hi_i <= lo_i:
            continue
        s = slice(lo_i, hi_i)
        edge = np.where(b == 1, zt[s], zb[s])
        ok = (zd[s] == b) & ~used[s] & (zdead[s] >= t0[k]) & np.where(b == 1, l[k] <= edge - TICK, h[k] >= edge + TICK)
        cand = np.where(ok)[0][::-1] + lo_i
        if len(cand) == 0:
            continue
        # find the fill minute for each candidate; take the earliest fill (most recent block on ties)
        a, e = np.searchsorted(t1m, max(t0[k], next_ok)), np.searchsorted(t1m, min(t1[k], sess_end[k]))
        best = None
        for j in cand:
            ed = zt[j] if b == 1 else zb[j]
            hit = np.where(l1[a:e] <= ed - TICK)[0] if b == 1 else np.where(h1[a:e] >= ed + TICK)[0]
            if len(hit) and (best is None or a + hit[0] < best[1]):
                if zdead[j] >= t1m[a + hit[0]]:      # still alive at the fill minute
                    best = (j, a + hit[0])
        if best is None:
            continue
        j, fi = best
        used[j] = True
        entry = zt[j] if b == 1 else zb[j]
        stp = zfar[j]
        risk = (entry - stp) * b
        if risk <= 0:
            continue
        tgt = entry + b * rr * risk
        if (b == 1 and l1[fi] <= stp) or (b == -1 and h1[fi] >= stp):
            px, jx, why = (min(stp, o1[fi]) - SLIP) if b == 1 else (max(stp, o1[fi]) + SLIP), fi, 0
        else:
            px, jx, why = _simulate_exit(t1m, o1, h1, l1, c1, fi + 1, sess_end[k], b, stp, tgt, SLIP)
            if jx < fi:          # no 1m bar left before session end -> flatten at fill-minute close
                px, jx, why = c1[fi] - b * SLIP, fi, 2
        trades.append(_trade_row(name, session, rr, sess_day[k], pd.Timestamp(t1m[fi]),
                                 pd.Timestamp(t1m[jx]) + pd.Timedelta(minutes=1), b, entry, entry, stp, tgt,
                                 px, why, (zb[j], zt[j], pd.Timestamp(t1[zv[j]]))))
        n_today += 1
        next_ok = t1m[jx] + M
    return trades


# ------------------------------------------------------------------ stats / output
BUCKETS = [0, 2.5, 5, 10, 20, 1e9]
BLABELS = ["<=2.5pt", "2.5-5", "5-10", "10-20", ">20"]


def stats(t):
    if t.empty:
        return pd.Series(dict(trades=0))
    gw, gl = t.r[t.r > 0].sum(), -t.r[t.r < 0].sum()
    return pd.Series(dict(trades=len(t), win_rate=(t.pnl_usd > 0).mean(), gross_r=t.gross_r.mean(),
                          net_r=t.r.mean(), expectancy_usd=t.pnl_usd.mean(),
                          profit_factor=gw / gl if gl > 0 else np.inf, total_r=t.r.sum(),
                          max_dd_r=max_dd(t.r.values), max_dd_usd=max_dd(t.pnl_usd.values),
                          pct_session_end=(t.exit_reason == "session_end").mean(),
                          median_risk_pts=t.risk_pts.median(), low_n=len(t) < 300))


VARIANTS = [  # (name, family, builder key, engine, stop/entry type)
    ("Baseline | FVG-edge stop", "Baseline", "fvg", "tap", "zone"),
    ("Baseline | candle stop", "Baseline", "fvg", "tap", "candle"),
    ("V1 OTE | FVG-edge stop", "V1 OTE", "fvg_ote", "tap", "zone"),
    ("V1 OTE | candle stop", "V1 OTE", "fvg_ote", "tap", "candle"),
    ("V2A IFVG retest | IFVG-edge stop", "V2A IFVG", "ifvg_a", "tap", "zone"),
    ("V2A IFVG retest | candle stop", "V2A IFVG", "ifvg_a", "tap", "candle"),
    ("V2B post-inversion FVG | FVG-edge stop", "V2B IFVG", "ifvg_b", "tap", "zone"),
    ("V2B post-inversion FVG | candle stop", "V2B IFVG", "ifvg_b", "tap", "candle"),
    ("V3 RB | tap-and-close", "V3 RB", "rb", "tap", "zone"),
    ("V3 RB | limit at edge", "V3 RB", "rb", "limit", "zone"),
]


def charts(trades):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    blue, orange, gray = "#2a78d6", "#eb6834", "#a3a29c"
    fams = ["Baseline", "V1 OTE", "V2A IFVG", "V2B IFVG", "V3 RB"]
    combos = [(s, rr) for s in ("NY", "Asia") for rr in RRS]
    fig, axes = plt.subplots(len(combos), len(fams), figsize=(22, 14), dpi=120, sharex=True)
    for r, (sess, rr) in enumerate(combos):
        base = trades[(trades.variant == VARIANTS[0][0]) & (trades.session == sess) & (trades.rr == rr)].sort_values("entry_time")
        for cidx, fam in enumerate(fams):
            ax = axes[r, cidx]
            if fam != "Baseline":
                ax.plot(base.entry_time, base.r.cumsum(), color=gray, lw=1.2, label="Baseline, FVG-edge stop")
            names = [v[0] for v in VARIANTS if v[1] == fam]
            for col, nm in zip((blue, orange), names):
                t = trades[(trades.variant == nm) & (trades.session == sess) & (trades.rr == rr)].sort_values("entry_time")
                if len(t):
                    ax.plot(t.entry_time, t.r.cumsum(), color=col, lw=1.5,
                            label=f"{nm.split('| ')[1]} (n={len(t)}, {t.r.sum():+.0f}R)")
            ax.axvline(DEV_END, color="#52514e", lw=0.8, ls="--")
            ax.axhline(0, color="#c3c2b7", lw=0.6)
            ax.set_title(f"{fam}: {sess} 1:{rr}", fontsize=10, loc="left")
            ax.legend(fontsize=7, frameon=False, loc="lower left")
            for sp in ("top", "right"):
                ax.spines[sp].set_visible(False)
            ax.tick_params(labelsize=7)
            if cidx == 0:
                ax.set_ylabel("Cumulative net R", fontsize=8)
    fig.suptitle("Cumulative net R per variant (1 MNQ, after costs). Dashed line = start of 2021-26 hold-out", x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(f"{OUT}/equity_grid.png")
    plt.close(fig)


def main():
    dev_only = "--dev-only" in sys.argv
    os.makedirs(OUT, exist_ok=True)
    m1 = load_1m()
    if dev_only:
        m1 = m1[m1.index < DEV_END]
    m5, _lv, f5, _hf = build_context(m1)
    print("context built", flush=True)
    sh_idx, sl_idx = swings(m5)
    finv = inversions(m5, f5)
    pre_tap = tapped_older_same_dir(m5, f5)
    zf = fvg_zones(f5)
    zones = {"fvg": zf, "fvg_ote": zf, "ifvg_a": ifvg_a_zones(m1, m5, finv, pre_tap),
             "ifvg_b": ifvg_b_zones(zf, finv), "rb": rb_zones(m1, m5, f5, sh_idx, sl_idx)}
    print({k: len(v) for k, v in zones.items()}, flush=True)
    ctx = dict(m1=m1, m5=m5, bias=m5["bias"].values,
               m1arr=(m1.index.values.astype(np.int64),) + tuple(m1[x].values for x in ("open", "high", "low", "close")),
               t0=m5.index.values.astype(np.int64), t1=m5["t1"].values.astype(np.int64),
               ohlc=tuple(m5[x].values for x in ("open", "high", "low", "close")), sess={})
    for s in SESSIONS:
        mask, sd, se = session_windows(m5, s)
        ctx["sess"][s] = (mask, sd, se.values.astype(np.int64))
    ote = OTE(m5, sh_idx, sl_idx)
    rows = []
    for name, fam, zkey, eng, st in VARIANTS:
        for s in SESSIONS:
            for rr in RRS:
                if eng == "tap":
                    tr = run_tapclose(ctx, zones[zkey], name, s, rr, st, ote=ote if zkey == "fvg_ote" else None,
                                      one_shot=(zkey == "rb"))
                else:
                    tr = run_limit(ctx, zones[zkey], name, s, rr)
                for t in tr:
                    t["family"] = fam
                rows += tr
                print(f"{name:42s} {s:4s} 1:{rr} trades={len(tr)}", flush=True)
    trades = pd.DataFrame(rows)
    trades["year"] = pd.to_datetime(trades.session_date).dt.year
    trades["period"] = np.where(pd.to_datetime(trades.session_date) < DEV_END, "dev 2010-2020", "holdout 2021-2026")
    trades["stop_bucket"] = pd.cut(trades.risk_pts, BUCKETS, labels=BLABELS)
    sfx = "_dev" if dev_only else ""
    trades.to_csv(f"{OUT}/trades{sfx}.csv", index=False)
    keys = ["family", "variant", "session", "rr"]
    for by, fn in ((["period"], "summary_by_period"), ([], "summary_all"), (["year"], "yearly"),
                   (["stop_bucket"], "by_stop_size"), (["period", "stop_bucket"], "by_stop_size_period")):
        out = trades.groupby(keys + by, observed=True, sort=False).apply(stats, include_groups=False).reset_index()
        out.to_csv(f"{OUT}/{fn}{sfx}.csv", index=False)
    if not dev_only:
        charts(trades)
    print("done")


if __name__ == "__main__":
    main()
