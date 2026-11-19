"""Fair value gaps.

Bullish FVG: candle1.high < candle3.low, gap = [candle1.high, candle3.low]; intermediate = candle2.low.
Bearish FVG: candle1.low > candle3.high, gap = [candle3.high, candle1.low]; intermediate = candle2.high.
Known at candle3's close (t1 of candle3).
"""
import numpy as np
import pandas as pd
from numba import njit

BIG = np.iinfo(np.int64).max


def ns(x):
    return np.asarray(x).astype("datetime64[ns]").astype(np.int64)


def detect_fvgs(bars):
    """Return one row per FVG: i (candle3 index), dir (+1 bull / -1 bear), bot, top, inter,
    t_c1 (candle1 open, ns), known_t (candle3 close, ns)."""
    h, l = bars["high"].values, bars["low"].values
    t0, t1 = ns(bars.index), ns(bars["t1"])
    n = len(h)
    if n < 3:
        return pd.DataFrame(columns=["i", "dir", "bot", "top", "inter", "t_c1", "known_t"])
    i_bull = np.where(h[:-2] < l[2:])[0] + 2
    i_bear = np.where(l[:-2] > h[2:])[0] + 2
    bull = pd.DataFrame({"i": i_bull, "dir": 1, "bot": h[i_bull - 2], "top": l[i_bull], "inter": l[i_bull - 1]})
    bear = pd.DataFrame({"i": i_bear, "dir": -1, "bot": h[i_bear], "top": l[i_bear - 2], "inter": h[i_bear - 1]})
    f = pd.concat([bull, bear], ignore_index=True).sort_values(["i", "dir"]).reset_index(drop=True)
    f["t_c1"] = t0[f.i.values - 2]
    f["known_t"] = t1[f.i.values]
    return f


@njit(cache=True)
def _first_cross(t, lo, hi, start_idx, level, direction, strict, limit_t):
    """First bar (open time) at/after start_idx where price reaches `level`:
    direction +1 -> low <= level (or < if strict); -1 -> high >= level (or >)."""
    out = np.full(len(start_idx), BIG)
    n = len(t)
    for k in range(len(start_idx)):
        for j in range(start_idx[k], n):
            if t[j] > limit_t[k]:
                break
            if direction[k] == 1:
                hit = lo[j] < level[k] if strict else lo[j] <= level[k]
            else:
                hit = hi[j] > level[k] if strict else hi[j] >= level[k]
            if hit:
                out[k] = t[j]
                break
    return out


def first_cross_times(m1, start_ns, level, direction, strict=False, limit_ns=None):
    """Vectorized helper over 1m bars. Returns the OPEN time (ns) of the first bar at/after start_ns
    trading to the level; the event is known at that bar's close (open + 1 bar)."""
    t = ns(m1.index)
    start = np.searchsorted(t, np.asarray(start_ns, dtype=np.int64))
    lim = np.full(len(start), BIG) if limit_ns is None else np.asarray(limit_ns, dtype=np.int64)
    return _first_cross(t, m1["low"].values, m1["high"].values, start, np.asarray(level, dtype=np.float64),
                        np.asarray(direction, dtype=np.int64), strict, lim)


@njit(cache=True)
def _first_body_through(c, t1, start, bot, top, direction, max_bars):
    out = np.full(len(start), BIG)
    n = len(c)
    for k in range(len(start)):
        for j in range(start[k], min(n, start[k] + max_bars)):
            if (direction[k] == 1 and c[j] < bot[k]) or (direction[k] == -1 and c[j] > top[k]):
                out[k] = t1[j]
                break
    return out


def add_status(f, bars, m1, max_bars=100000):
    """touch_t: first 1m bar after known_t trading into the gap (bull: low <= top) -> 1m open time.
    disr_t: close time of the first candle of the FVG's own TF whose close is through the whole gap
    (bull: close < bot; bear: close > top) -> disrespected from then on.
    sweep_t: first 1m bar after the touch trading beyond the intermediate (bull: low < inter)."""
    f = f.copy()
    edge = np.where(f.dir == 1, f.top, f.bot)
    f["touch_t"] = first_cross_times(m1, f.known_t.values, edge, f.dir.values)
    after = np.where(f.touch_t.values == BIG, BIG - 1, f.touch_t.values)
    sw = first_cross_times(m1, after, f.inter.values, f.dir.values, strict=True)
    f["sweep_t"] = np.where(f.touch_t.values == BIG, BIG, sw)
    f["disr_t"] = _first_body_through(bars["close"].values, ns(bars["t1"]), f.i.values + 1,
                                      f.bot.values, f.top.values, f.dir.values.astype(np.int64), max_bars)
    return f


def is_respected(f_row, at_ns):
    """Touched by at_ns (touch bar closed: touch_t + 1m <= at) and not disrespected by at_ns."""
    return f_row.touch_t <= at_ns - 60_000_000_000 and f_row.disr_t > at_ns
