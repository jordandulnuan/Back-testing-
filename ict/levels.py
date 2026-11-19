"""Key levels (Step 2) for all level timeframes, both directions, built once over the whole history.

Each level: id, type (fvg / fvg_inter / cisd / rb), tf, dir (+1 long level below price, -1 short level above),
zb, zt (zone), mid (rejection-block 50%), active_t (ns, the moment it is known), formed_t (for the age
limit), cisd_with_fvg, and first_touch_t = open time of the first 1m bar at/after active_t that trades
into the zone (long: low <= zt); for intermediate-low/high point levels, only after price has closed back
on the trade side of the level.
Per day, the backtester keeps levels in the bias direction formed within `lookback_days`, untouched before
09:30, and only lets the `max_active` closest ones (to the previous 1m close) trigger.
"""
import numpy as np
import pandas as pd
from numba import njit

from .fvg import BIG, ns
from .cisd import detect_cisd
from .rejection import detect_rejection_blocks


@njit(cache=True)
def _first_touch(t, lo, hi, cl, start, zb, zt, direction, point):
    """Zone levels: first 1m bar at/after activation that trades into the zone (long: low <= zt).
    Point levels (an FVG's intermediate low/high, activated while price is beyond it): the first bar that
    trades back to it after price has closed on the trade side of it (long: a prior close above it)."""
    out = np.full(len(start), BIG)
    n = len(t)
    for k in range(len(start)):
        armed = not point[k]
        for j in range(max(start[k], 1), n):
            if direction[k] == 1:
                if armed and lo[j] <= zt[k]:
                    out[k] = t[j]
                    break
                if cl[j] > zt[k]:
                    armed = True
            else:
                if armed and hi[j] >= zb[k]:
                    out[k] = t[j]
                    break
                if cl[j] < zb[k]:
                    armed = True
    return out


def first_touch(m1, active_t, zb, zt, direction, point):
    t = ns(m1.index)
    start = np.searchsorted(t, np.asarray(active_t, dtype=np.int64))
    return _first_touch(t, m1["low"].values, m1["high"].values, m1["close"].values, start,
                        np.asarray(zb, float), np.asarray(zt, float), np.asarray(direction, np.int64),
                        np.asarray(point, np.bool_))


def build_levels(bars, fvgs, m1, cfg, verbose=True):
    lc = cfg["levels"]
    rows = []
    for tf in lc["timeframes"]:
        f = fvgs[tf]
        b = bars[tf]
        # A) unmitigated FVG zone, active at candle3 close, consumed at first touch
        a = pd.DataFrame({"type": "fvg", "tf": tf, "dir": f.dir.values, "zb": f.bot.values, "zt": f.top.values,
                          "mid": np.nan, "active_t": f.known_t.values, "formed_t": f.known_t.values,
                          "cisd_with_fvg": False})
        rows.append(a)
        # A') mitigated FVG -> its intermediate low/high becomes the level once swept
        sw = f[f.sweep_t < BIG]
        a2 = pd.DataFrame({"type": "fvg_inter", "tf": tf, "dir": sw.dir.values, "zb": sw.inter.values,
                           "zt": sw.inter.values, "mid": np.nan, "active_t": sw.sweep_t.values + 60_000_000_000,
                           "formed_t": sw.known_t.values, "cisd_with_fvg": False})
        rows.append(a2)
        if lc["types"].get("cisd", True):
            cz = detect_cisd(b, f, lc["cisd_max_bars"])
            rows.append(pd.DataFrame({"type": "cisd", "tf": tf, "dir": cz.dir.values, "zb": cz.zb.values,
                                      "zt": cz.zt.values, "mid": np.nan, "active_t": cz.known_t.values,
                                      "formed_t": cz.known_t.values, "cisd_with_fvg": cz.cisd_with_fvg.values}))
        if lc["types"].get("rb", True):
            per_day = {"3min": 460, "5min": 276, "15min": 92, "30min": 46, "1h": 23, "4h": 6}[tf]
            rb = detect_rejection_blocks(b, f, per_day * lc["lookback_days"])
            rows.append(pd.DataFrame({"type": "rb", "tf": tf, "dir": rb.dir.values, "zb": rb.zb.values,
                                      "zt": rb.zt.values, "mid": rb.mid.values, "active_t": rb.known_t.values,
                                      "formed_t": rb.known_t.values, "cisd_with_fvg": False}))
        if verbose:
            print(f"  levels {tf:>5s}: " + ", ".join(f"{r.type.iloc[0]}={len(r)}" for r in rows[-4:] if len(r)), flush=True)
    lv = pd.concat(rows, ignore_index=True)
    lv = lv[lv.zt >= lv.zb].reset_index(drop=True)
    lv["first_touch_t"] = first_touch(m1, lv.active_t.values, lv.zb.values, lv.zt.values, lv.dir.values,
                                      (lv.type == "fvg_inter").values)
    lv["id"] = np.arange(len(lv))
    return lv
