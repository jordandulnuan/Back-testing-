"""Rejection blocks.

Bullish (shorts mirror): a candle whose LOWER WICK trades into a still-valid bullish FVG of the same TF
(low <= gap top while the body stays above the gap top) or, after the FVG was mitigated, whose lower wick
sweeps the FVG's intermediate low while the body stays above it. Zone = the wick: [low, min(open, close)],
midpoint stored. Known at the candle's close. The FVG must be known before the candle opens and not yet
disrespected (no body close through the whole gap before the candle closes).
"""
import numpy as np
import pandas as pd
from numba import njit

from .fvg import ns


@njit(cache=True)
def _scan(o, h, l, c, fi, fdir, fbot, ftop, finter, fdisr_i, max_bars):
    out_k, out_f, out_kind = [], [], []
    n = len(c)
    for q in range(len(fi)):
        d = fdir[q]
        for k in range(fi[q] + 1, min(n, fi[q] + 1 + max_bars)):
            if k >= fdisr_i[q]:
                break
            body_lo, body_hi = min(o[k], c[k]), max(o[k], c[k])
            if d == 1:
                if l[k] <= ftop[q] and body_lo > ftop[q] and l[k] < body_lo:
                    out_k.append(k); out_f.append(q); out_kind.append(0)
                elif l[k] < finter[q] and body_lo > finter[q] and l[k] < body_lo:
                    out_k.append(k); out_f.append(q); out_kind.append(1)
            else:
                if h[k] >= fbot[q] and body_hi < fbot[q] and h[k] > body_hi:
                    out_k.append(k); out_f.append(q); out_kind.append(0)
                elif h[k] > finter[q] and body_hi < finter[q] and h[k] > body_hi:
                    out_k.append(k); out_f.append(q); out_kind.append(1)
    return np.array(out_k, dtype=np.int64), np.array(out_f, dtype=np.int64), np.array(out_kind, dtype=np.int64)


def detect_rejection_blocks(bars, fvgs, max_bars):
    o, h, l, c = (bars[x].values for x in ("open", "high", "low", "close"))
    t1 = ns(bars["t1"])
    t1_bars = t1
    # disrespect bar index on the FVG's own TF (disr_t is that candle's close time)
    disr_i = np.searchsorted(t1_bars, fvgs.disr_t.values, side="left")
    disr_i = np.where(fvgs.disr_t.values >= np.iinfo(np.int64).max, len(c) + 10, disr_i + 1)
    k, q, kind = _scan(o, h, l, c, fvgs.i.values.astype(np.int64), fvgs.dir.values.astype(np.int64),
                       fvgs.bot.values, fvgs.top.values, fvgs.inter.values, disr_i.astype(np.int64), max_bars)
    if len(k) == 0:
        return pd.DataFrame(columns=["dir", "zb", "zt", "mid", "known_t", "k", "kind"])
    d = fvgs.dir.values[q]
    body_lo, body_hi = np.minimum(o[k], c[k]), np.maximum(o[k], c[k])
    zb = np.where(d == 1, l[k], body_hi)
    zt = np.where(d == 1, body_lo, h[k])
    rb = pd.DataFrame({"dir": d, "zb": zb, "zt": zt, "mid": (zb + zt) / 2, "known_t": t1[k], "k": k,
                       "kind": np.where(kind == 0, "fvg", "inter")})
    return rb.drop_duplicates(["dir", "k"]).reset_index(drop=True)
