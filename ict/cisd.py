"""Change in state of delivery (CISD) key levels.

Bullish (shorts mirror): when a candle trades into a bullish FVG (low <= top, the first touch) or, after
the FVG was mitigated, trades below its intermediate low (the first sweep), take the run of consecutive
down-close candles that made the move into the level: it ends at the touch candle if that candle closed
down, otherwise at the candle before it. Mark the OPEN of the first candle in the run. The CISD is
confirmed by the first later candle whose close is above that open, provided no candle trades below the
run's extreme before confirmation. Zone = [run extreme, run open]; known at the confirming candle's close.
cisd_with_fvg = the displacement from the run to the confirming candle (+1 candle) left a bullish FVG
on the same TF whose range overlaps the zone.
"""
import numpy as np
import pandas as pd

from .fvg import ns, BIG


def detect_cisd(bars, fvgs, max_bars=50):
    o, h, l, c = (bars[x].values for x in ("open", "high", "low", "close"))
    t0, t1 = ns(bars.index), ns(bars["t1"])
    n = len(c)
    rows = []
    # events: (fvg row, candle index of the event)
    for f in fvgs.itertuples():
        d = f.dir
        events = []
        start = f.i + 1
        if start >= n:
            continue
        seg_h, seg_l = h[start:start + 5000], l[start:start + 5000]
        touch = np.where(seg_l <= f.top)[0] if d == 1 else np.where(seg_h >= f.bot)[0]
        if len(touch):
            k = start + touch[0]
            events.append(k)
            seg2 = l[k:k + 5000] if d == 1 else h[k:k + 5000]
            sw = np.where(seg2 < f.inter)[0] if d == 1 else np.where(seg2 > f.inter)[0]
            if len(sw):
                events.append(k + sw[0])
        for k in events:
            down = (lambda j: c[j] < o[j]) if d == 1 else (lambda j: c[j] > o[j])
            e = k if down(k) else k - 1
            if e < 0 or not down(e):
                continue
            s = e
            while s - 1 >= 0 and down(s - 1):
                s -= 1
            run_open = o[s]
            ext = l[s:k + 1].min() if d == 1 else h[s:k + 1].max()
            conf = -1
            j0 = k if not down(k) else k + 1
            for j in range(j0, min(n, k + 1 + max_bars)):
                if j > k and ((d == 1 and l[j] < ext) or (d == -1 and h[j] > ext)):
                    break                                       # new extreme before confirmation
                if (d == 1 and c[j] > run_open) or (d == -1 and c[j] < run_open):
                    conf = j
                    break
            if conf < 0:
                continue
            zb, zt = (ext, run_open) if d == 1 else (run_open, ext)
            # displacement FVG overlapping the zone
            disp = fvgs[(fvgs.dir == d) & (fvgs.i > e) & (fvgs.i <= conf + 1)]
            with_fvg = bool(((disp.bot <= zt) & (disp.top >= zb)).any())
            rows.append((d, zb, zt, t1[conf], t0[s], k, conf, with_fvg, f.Index))
    cols = ["dir", "zb", "zt", "known_t", "run_start_t", "touch_i", "conf_i", "cisd_with_fvg", "fvg_row"]
    out = pd.DataFrame(rows, columns=cols)
    return out.drop_duplicates(["dir", "zb", "zt", "known_t"]).reset_index(drop=True)
