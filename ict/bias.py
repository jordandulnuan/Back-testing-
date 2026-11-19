"""Daily bias and draw on liquidity, computed ONCE per day at bias_time (09:25 ET) from closed candles only.

Per TF (D, 4h, 1h), among FVGs whose candle3 is one of the last `lookback` candles closed by 09:25:
  bull_ok = the most recent TESTED bullish FVG (traded into by 09:25) is respected (no candle of that TF
            has closed below its bottom by 09:25)
  bear_bad = the most recent tested bearish FVG is disrespected (a candle closed above its top by 09:25)
  score +1 if bull_ok and bear_bad; -1 if the mirror (bearish respected and bullish disrespected); else 0.
Bias = sign(total) if |total| >= threshold (variant: all three agree), else no-trade day.
"""
import numpy as np
import pandas as pd

from .fvg import ns, BIG
from .swings import pivots

MIN = 60_000_000_000


def tf_score(bars, fvgs, at_ns, lookback):
    t1 = ns(bars["t1"])
    last = np.searchsorted(t1, at_ns, side="right") - 1          # last candle closed by at_ns
    if last < 2:
        return 0, {}
    first = last - lookback + 1
    f = fvgs[(fvgs.i >= first) & (fvgs.i <= last)]
    tested = f[f.touch_t <= at_ns - MIN]                           # touch bar closed by at_ns (no overflow on "never")
    info = {}

    def latest(d):
        x = tested[tested.dir == d]
        return None if x.empty else x.iloc[-1]

    b, s = latest(1), latest(-1)
    b_resp = b is not None and b.disr_t > at_ns
    b_disr = b is not None and b.disr_t <= at_ns
    s_resp = s is not None and s.disr_t > at_ns
    s_disr = s is not None and s.disr_t <= at_ns
    info = dict(bull_resp=b_resp, bull_disr=b_disr, bear_resp=s_resp, bear_disr=s_disr)
    if b_resp and s_disr:
        return 1, info
    if s_resp and b_disr:
        return -1, info
    return 0, info


def daily_bias(bars, fvgs, at_ns, cfg):
    bc = cfg["bias"]
    scores = {tf: tf_score(bars[tf], fvgs[tf], at_ns, bc["lookback"][tf])[0] for tf in bc["timeframes"]}
    total = sum(scores.values())
    if bc.get("require_all_three"):
        direction = int(np.sign(total)) if abs(total) == len(bc["timeframes"]) else 0
    else:
        direction = int(np.sign(total)) if abs(total) >= bc["threshold"] else 0
    return direction, total, scores


def draw_on_liquidity(bars, m1, at_ns, direction, cfg):
    """Nearest unswept 1h/4h swing high above price (bullish) / swing low below (bearish), preferring
    relative equal highs/lows (two unswept swings within `equal_tolerance` points). Uses pivots confirmed
    by at_ns and 1m bars closed by at_ns."""
    if direction == 0:
        return None
    n = cfg["swings"]["n"]
    tol = cfg["dol"]["equal_tolerance"]
    t1m = ns(m1["t1"])
    last1m = np.searchsorted(t1m, at_ns, side="right") - 1
    if last1m < 0:
        return None
    price = m1["close"].values[last1m]
    cands = []
    for tf in cfg["dol"]["timeframes"]:
        b = bars[tf]
        t1 = ns(b["t1"])
        last = np.searchsorted(t1, at_ns, side="right") - 1
        if last < 2 * n:
            continue
        sh, sl = pivots(b["high"].values[:last + 1], b["low"].values[:last + 1], n)
        idx = sh if direction == 1 else sl
        idx = idx[idx + n <= last]
        lv = (b["high"] if direction == 1 else b["low"]).values[idx]
        for i, p in zip(idx, lv):
            if (direction == 1 and p <= price) or (direction == -1 and p >= price):
                continue
            # unswept: nothing traded beyond it from the pivot candle's close to at_ns
            a = np.searchsorted(t1m, t1[i], side="right") - 1
            seg = m1["high"].values[a + 1:last1m + 1] if direction == 1 else m1["low"].values[a + 1:last1m + 1]
            if len(seg) and ((direction == 1 and seg.max() > p) or (direction == -1 and seg.min() < p)):
                continue
            cands.append((p, tf))
    if not cands:
        return None
    cands.sort(key=lambda x: abs(x[0] - price))
    prices = np.array([p for p, _ in cands])
    for p, tf in cands:
        if (np.abs(prices - p) <= tol).sum() >= 2:
            grp = prices[np.abs(prices - p) <= tol]
            return dict(price=float(grp.max() if direction == 1 else grp.min()), kind="equal", tf=tf, ref=price)
    return dict(price=float(cands[0][0]), kind="single", tf=cands[0][1], ref=price)
