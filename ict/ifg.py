"""Manipulation leg and IFG (inverted fair value gap) confirmation.

Longs (shorts mirror):
  * touch bar k = first 1m bar trading into an active key level at/after 09:30.
  * leg high = most recent 1m swing high (N-bar pivot) whose pivot bar is before k and whose confirmation
    bar (pivot + N) is at or before k.
  * leg low = lowest low from bar k up to the current bar; it extends while price makes lower lows and
    freezes at the signal.
  * IFG candidates on 1m..5m (and 30s if enabled): BEARISH FVGs whose candle1 opens at/after the swing-high
    bar and whose range lies inside [leg low, leg high], known (candle3 closed) by the current time and not
    already closed through.
  * rule 'highest': at each 1m close, the IFG is the most recent candidate on the highest TF that has one;
    signal when a candle of that TF closes above the gap top. 'any': first close above the top of any
    candidate on its own TF. '1m': only 1m gaps.
  * entry = close of the signal candle.
"""
import numpy as np
from numba import njit

from .fvg import BIG
from .swings import last_confirmed

MIN = 60_000_000_000


@njit(cache=True)
def _inv(c, t1, i, d, bot, top, max_bars):
    out = np.full(len(i), BIG)
    n = len(c)
    for q in range(len(i)):
        for j in range(i[q] + 1, min(n, i[q] + 1 + max_bars)):
            if (d[q] == -1 and c[j] > top[q]) or (d[q] == 1 and c[j] < bot[q]):
                out[q] = t1[j]
                break
    return out


def precompute_inversions(bars, fvgs, max_bars=3000):
    """inv_t: close time of the first candle (own TF) whose close is through the gap: for a bearish gap
    (a long's IFG) close > top; for a bullish gap (a short's IFG) close < bottom."""
    out = fvgs.copy()
    out["inv_t"] = _inv(bars["close"].values, bars["t1"].values.astype("datetime64[ns]").astype(np.int64),
                        fvgs.i.values.astype(np.int64), fvgs.dir.values.astype(np.int64),
                        fvgs.bot.values.astype(float), fvgs.top.values.astype(float), max_bars)
    return out


def find_signal(k, direction, m1, piv, n, ifg_fvgs, tf_order, rule, end_ns):
    """Scan 1m closes from the touch bar k until a signal or end_ns (signals must close before end_ns).
    m1: dict of numpy arrays (t0, t1, low, high, close). piv: (sh_idx, sl_idx) 1m pivots.
    ifg_fvgs: dict tf -> DataFrame with dir, bot, top, t_c1, known_t, inv_t.
    Returns a dict or None."""
    t0, t1, lo, hi, cl = m1["t0"], m1["t1"], m1["low"], m1["high"], m1["close"]
    sh, sl = piv
    p = last_confirmed(sh if direction == 1 else sl, n, k)
    if p < 0 or p >= k:
        return None
    leg_start_t = t0[p]
    leg_far = hi[p] if direction == 1 else lo[p]                      # swing high (long) / low (short)
    want = -direction                                                  # IFG opposes the trade
    tfs = [tf for tf in tf_order if rule != "1m" or tf == "1min"]
    cand = {}
    for tf in tfs:
        f = ifg_fvgs[tf]
        f = f[(f.dir.values == want) & (f.t_c1.values >= leg_start_t) & (f.known_t.values <= end_ns)]
        cand[tf] = (f.bot.values, f.top.values, f.known_t.values, f.inv_t.values)
    ext = np.inf if direction == 1 else -np.inf
    j = k
    while j < len(t0) and t1[j] < end_ns:
        ext = min(ext, lo[j]) if direction == 1 else max(ext, hi[j])
        tau = t1[j]
        chosen = None
        hits = []
        for tf in reversed(tfs):                                       # highest TF first
            bot, top, kt, inv = cand[tf]
            if direction == 1:
                ok = (kt <= tau) & (inv >= tau) & (top <= leg_far) & (bot >= ext)
            else:
                ok = (kt <= tau) & (inv >= tau) & (bot >= leg_far) & (top <= ext)
            if not ok.any():
                continue
            idx = np.where(ok)[0]
            if rule == "highest":
                q = idx[np.argmax(kt[idx])]                            # most recent on the highest TF
                if inv[q] == tau:
                    chosen = (tf, bot[q], top[q], kt[q])
                break
            fired = idx[inv[idx] == tau]
            if len(fired):
                q = fired[np.argmax(kt[fired])]
                hits.append((tf, bot[q], top[q], kt[q]))
        if rule != "highest" and hits:
            chosen = hits[0]                                           # highest TF among those firing now
        if chosen is not None:
            tf, gb, gt, gk = chosen
            return dict(signal_i=j, signal_t=tau, entry_ref=cl[j], ifg_tf=tf, ifg_bot=gb, ifg_top=gt,
                        ifg_known_t=gk, leg_start_i=p, leg_start_t=leg_start_t, leg_far=leg_far,
                        leg_extreme=ext, touch_i=k)
        j += 1
    return None
