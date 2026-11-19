import numpy as np
import pandas as pd


def make_bars(rows, start="2020-01-06 09:20", freq="1min"):
    """rows: list of (open, high, low, close). Returns a bars frame with t1."""
    idx = pd.date_range(start, periods=len(rows), freq=freq)
    b = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx, dtype=float)
    b.index.name = "t0"
    b["volume"] = np.nan
    b["t1"] = b.index + pd.Timedelta(freq)
    return b


def synthetic_1m(days=45, seed=7, start="2020-01-01"):
    """Random-walk 1m bars on CME hours (Sun-Fri 18:00-17:00, no 17:00-18:00), ticks of 0.25."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=days * 1440, freq="1min")
    m = idx.hour * 60 + idx.minute
    keep = (m < 17 * 60) | (m >= 18 * 60)
    wd = idx.weekday
    keep &= ~((wd == 5) | ((wd == 4) & (m >= 17 * 60)) | ((wd == 6) & (m < 18 * 60)))
    idx = idx[keep]
    steps = rng.normal(0, 2.0, len(idx)) * np.where((idx.hour >= 9) & (idx.hour < 12), 1.8, 1.0)
    close = 10000 + np.cumsum(steps)
    open_ = np.r_[close[0], close[:-1]]
    hi = np.maximum(open_, close) + np.abs(rng.normal(0, 1.2, len(idx)))
    lo = np.minimum(open_, close) - np.abs(rng.normal(0, 1.2, len(idx)))
    q = lambda x: np.round(x / 0.25) * 0.25
    df = pd.DataFrame({"open": q(open_), "high": q(hi), "low": q(lo), "close": q(close)}, index=idx)
    df["high"] = df[["open", "high", "close"]].max(axis=1)
    df["low"] = df[["open", "low", "close"]].min(axis=1)
    df["volume"] = np.nan
    return df
