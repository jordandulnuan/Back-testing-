"""N-bar pivots. A swing high at i: high[i] > max(high[i-N:i]) and high[i] >= max(high[i+1:i+N+1]).
It is only known at the close of bar i+N (confirmation delay of N bars)."""
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


def pivots(high, low, n):
    """Return (sh_idx, sl_idx): pivot bar indices (confirmation bar = idx + n)."""
    high, low = np.asarray(high, float), np.asarray(low, float)
    m = len(high)
    if m < 2 * n + 1:
        return np.array([], int), np.array([], int)
    wh = sliding_window_view(high, 2 * n + 1)          # window centred on i = start + n
    wl = sliding_window_view(low, 2 * n + 1)
    c = n
    sh = (wh[:, c] > wh[:, :c].max(axis=1)) & (wh[:, c] >= wh[:, c + 1:].max(axis=1))
    sl = (wl[:, c] < wl[:, :c].min(axis=1)) & (wl[:, c] <= wl[:, c + 1:].min(axis=1))
    return np.where(sh)[0] + n, np.where(sl)[0] + n


def last_confirmed(piv_idx, n, upto_idx):
    """Most recent pivot whose confirmation bar (idx + n) <= upto_idx, or -1."""
    p = np.searchsorted(piv_idx + n, upto_idx, side="right") - 1
    return piv_idx[p] if p >= 0 else -1
