import numpy as np

from ict.fvg import detect_fvgs, add_status, ns
from ict.tests.helpers import make_bars


def test_bullish_and_bearish_fvg():
    b = make_bars([(100, 101, 99, 100.5), (100.5, 106, 100.4, 105.5), (105.5, 108, 104, 107.5),   # bull gap
                   (107.5, 107.6, 103, 103.2), (103.2, 103.3, 99, 99.5), (99.5, 100, 98, 98.5)])   # bear gap
    f = detect_fvgs(b)
    bull = f[f.dir == 1].iloc[0]
    assert (bull.i, bull.bot, bull.top, bull.inter) == (2, 101, 104, 100.4)
    assert bull.known_t == ns(b.t1)[2]                       # known only at candle 3's close
    bear = f[(f.dir == -1) & (f.i == 5)].iloc[0]
    assert (bear.bot, bear.top, bear.inter) == (100, 103, 103.3)   # [c3.high, c1.low], inter = c2.high


def test_no_gap_when_wicks_overlap():
    b = make_bars([(100, 101, 99, 100.5), (100.5, 103, 100, 102.5), (102.5, 104, 101, 103.5)])
    assert detect_fvgs(b).empty                               # c1.high 101 == c3.low 101 -> no gap


def test_mitigation_respect_disrespect():
    rows = [(100, 101, 99, 100.5), (100.5, 106, 100.4, 105.5), (105.5, 108, 104, 107.5),
            (107.5, 107.8, 103.5, 105),      # trades into the gap (low 103.5 <= top 104): mitigated, body above bottom
            (105, 106, 102, 102.5),          # still respected (close 102.5 > bottom 101)
            (102.5, 102.6, 100, 100.5)]      # body closes below bottom 101 -> disrespected
    b = make_bars(rows)
    f = add_status(detect_fvgs(b), b, b)
    r = f[f.dir == 1].iloc[0]
    t0, t1 = ns(b.index), ns(b.t1)
    assert r.touch_t == t0[3]
    assert r.disr_t == t1[5]
    assert r.sweep_t == t0[5]                                 # low 100 < intermediate low 100.4
