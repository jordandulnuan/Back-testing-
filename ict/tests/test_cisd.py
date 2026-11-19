from ict.cisd import detect_cisd
from ict.fvg import detect_fvgs, add_status, ns
from ict.tests.helpers import make_bars

ROWS = [(100, 101, 99, 100.5), (100.5, 106, 100.4, 105.5), (105.5, 108, 104, 107.5),   # bull FVG [101, 104]
        (107.5, 108, 106, 106.5),            # down (series start, open 107.5)
        (106.5, 106.8, 105, 105.2),          # down
        (105.2, 105.3, 103, 103.5),          # down, trades into the FVG (low 103 <= 104): touch
        (103.5, 106, 103.2, 105.8),          # up, closes below 107.5: not yet
        (105.8, 108.5, 105.5, 108),          # closes above 107.5: CISD confirmed
        (108, 108.2, 107.6, 108.1)]


def test_bullish_cisd_zone_and_confirmation():
    b = make_bars(ROWS, freq="5min")
    f = add_status(detect_fvgs(b), b, b)
    c = detect_cisd(b, f[f.dir == 1], max_bars=50)
    row = c.iloc[0]
    assert (row.zb, row.zt) == (103, 107.5)                   # series extreme -> open of first down candle
    assert row.known_t == ns(b.t1)[7]                         # known at the confirming candle's close
    assert row.touch_i == 5 and row.conf_i == 7
    assert bool(row.cisd_with_fvg)                            # displacement left FVG [105.3, 105.5] in the zone


def test_no_cisd_if_new_low_before_confirmation():
    rows = ROWS[:6] + [(103.5, 104, 102.5, 103.8), (103.8, 108.5, 103.7, 108)]
    b = make_bars(rows, freq="5min")
    f = add_status(detect_fvgs(b), b, b)
    c = detect_cisd(b, f[f.dir == 1], max_bars=50)
    assert c[c.touch_i == 5].empty                            # low 102.5 broke the series extreme first
