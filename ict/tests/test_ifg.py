import numpy as np
import pandas as pd

from ict.fvg import detect_fvgs, ns
from ict.ifg import find_signal, precompute_inversions
from ict.swings import pivots
from ict.tests.helpers import make_bars

ROWS = [(110, 111, 109, 110), (110, 112, 109.5, 111.5), (111.5, 113, 111, 112.5),
        (112.5, 115, 112, 114),        # 3: 1m swing high 115 (confirmed at bar 6)
        (114, 114.5, 111, 111.5),
        (111.5, 112, 108, 108.5),
        (108.5, 109, 106, 106.5),      # 6: bearish 1m FVG [109, 111] (c1 = bar 4)
        (106.5, 107, 104, 104.5),      # 7: touches the key level (k = 7); bearish FVG [107, 108]
        (104.5, 105, 103, 104.8),      # 8: leg low 103; bearish FVG [105, 106]
        (104.8, 108, 104.5, 107.8),    # 9: closes above 106 -> inverts the most recent 1m gap
        (107.8, 111.8, 107.5, 111.5)]  # 10: closes above 111


def _setup():
    b = make_bars(ROWS)
    m1 = dict(t0=ns(b.index), t1=ns(b.t1), low=b.low.values, high=b.high.values, close=b.close.values)
    piv = pivots(b.high.values, b.low.values, 3)
    f1 = precompute_inversions(b, detect_fvgs(b))
    return b, m1, piv, f1


def test_manipulation_leg_and_1m_ifg():
    b, m1, piv, f1 = _setup()
    assert 3 in piv[0]
    empty = f1.iloc[0:0]
    fv = {"1min": f1, "2min": empty, "3min": empty, "4min": empty, "5min": empty}
    tfs = ["1min", "2min", "3min", "4min", "5min"]
    s = find_signal(7, 1, m1, piv, 3, fv, tfs, "highest", ns(b.t1)[-1] + 1)
    assert s["leg_far"] == 115 and s["leg_start_i"] == 3
    assert s["signal_i"] == 9 and s["ifg_tf"] == "1min"
    assert (s["ifg_bot"], s["ifg_top"]) == (105, 106)        # most recent 1m gap inside the leg
    assert s["leg_extreme"] == 103                            # lowest low at/after the touch
    assert s["entry_ref"] == 107.8                            # close of the signal candle


def test_highest_timeframe_wins():
    b, m1, piv, f1 = _setup()
    t1 = ns(b.t1)
    g2 = pd.DataFrame({"i": [3], "dir": [-1], "bot": [107.0], "top": [110.0], "inter": [np.nan],
                       "t_c1": [ns(b.index)[4]], "known_t": [t1[6]], "inv_t": [t1[10]]})
    empty = f1.iloc[0:0]
    fv = {"1min": f1, "2min": g2, "3min": empty, "4min": empty, "5min": empty}
    tfs = ["1min", "2min", "3min", "4min", "5min"]
    s = find_signal(7, 1, m1, piv, 3, fv, tfs, "highest", t1[-1] + 1)
    assert s["ifg_tf"] == "2min" and s["signal_i"] == 10     # the 1m inversion at bar 9 is ignored
    s_any = find_signal(7, 1, m1, piv, 3, fv, tfs, "any", t1[-1] + 1)
    assert s_any["signal_i"] == 9 and s_any["ifg_tf"] == "1min"
    s_1m = find_signal(7, 1, m1, piv, 3, fv, tfs, "1m", t1[-1] + 1)
    assert s_1m["ifg_tf"] == "1min"


def test_no_signal_before_end():
    b, m1, piv, f1 = _setup()
    empty = f1.iloc[0:0]
    fv = {"1min": f1, "2min": empty, "3min": empty, "4min": empty, "5min": empty}
    s = find_signal(7, 1, m1, piv, 3, fv, ["1min", "2min", "3min", "4min", "5min"], "highest", ns(b.t1)[8])
    assert s is None                                         # deadline before the inverting close
