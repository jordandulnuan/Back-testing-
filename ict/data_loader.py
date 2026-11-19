"""Load 1m (or 1s) bars, normalize to America/New_York, build a continuous series, resample, cache.

Every bar frame returned here has: index = bar OPEN time (naive New York wall clock), columns
open, high, low, close, volume, t1 (bar CLOSE time). A bar is only "known" at t1 (left-closed labels).
"""
import glob
import os

import numpy as np
import pandas as pd

TF_ORDER = ["30s", "1min", "2min", "3min", "4min", "5min", "15min", "30min", "1h", "4h", "D"]


def _ny_naive(ts):
    ts = pd.to_datetime(ts, utc=True)
    return ts.tz_convert("America/New_York").tz_localize(None)


def load_proxy(path, tick):
    """HistData CFD 1m parquet (already New York wall-clock with DST, verified by check_timezone.py)."""
    df = pd.read_parquet(path)
    df.index = pd.DatetimeIndex(df.index).as_unit("ns")
    for c in ("open", "high", "low", "close"):
        df[c] = (df[c] / tick).round() * tick
    df["volume"] = np.nan                                   # proxy has no volume
    return df[["open", "high", "low", "close", "volume"]]


def load_futures(futures_dir, symbol_prefix, tick, roll_adjust="difference"):
    """Databento GLBX.MDP3 ohlcv files (dbn.zst, csv or parquet) for one root (NQ or ES).
    Continuous series: if the files hold a continuous symbol (e.g. NQ.v.0, volume roll) the contract change
    is detected from the instrument_id column; otherwise, from raw contracts, the front month each day is
    the contract with the most volume (volume roll). Price jumps at each change are removed by difference
    back-adjustment (older history shifted by the gap between the old contract's last close and the new
    contract's first open at the switch)."""
    files = sorted(glob.glob(os.path.join(futures_dir, "*")))
    frames = []
    for f in files:
        if f.endswith(".dbn.zst") or f.endswith(".dbn"):
            import databento as db                              # optional dependency
            d = db.DBNStore.from_file(f).to_df().reset_index()
        elif f.endswith(".csv") or f.endswith(".csv.zst"):
            d = pd.read_csv(f)
        elif f.endswith(".parquet"):
            d = pd.read_parquet(f).reset_index()
        else:
            continue
        frames.append(d)
    if not frames:
        raise FileNotFoundError(f"no futures files in {futures_dir}")
    d = pd.concat(frames, ignore_index=True)
    d = d[d["symbol"].astype(str).str.startswith(symbol_prefix)]
    d["ts"] = _ny_naive(d["ts_event"])
    key = "instrument_id" if "instrument_id" in d.columns else "symbol"
    d = d.sort_values("ts")
    if d[key].nunique() > 1 and not d["symbol"].astype(str).str.contains(r"\.v\.|\.c\.|\.n\.").any():
        # raw contracts: keep, per trading day, the contract with the most volume
        d["td"] = (d["ts"] + pd.Timedelta(hours=6)).dt.normalize()
        front = d.groupby(["td", key])["volume"].sum().reset_index().sort_values("volume").groupby("td").tail(1)
        d = d.merge(front[["td", key]], on=["td", key])
    d = d.drop_duplicates("ts").set_index("ts").sort_index()
    if roll_adjust == "difference":
        chg = np.where(d[key].values[1:] != d[key].values[:-1])[0] + 1
        adj = np.zeros(len(d))
        for i in chg[::-1]:
            gap = d["open"].iloc[i] - d["close"].iloc[i - 1]
            adj[:i] += gap
        for c in ("open", "high", "low", "close"):
            d[c] = d[c] + adj
    for c in ("open", "high", "low", "close"):
        d[c] = (d[c] / tick).round() * tick
    return d[["open", "high", "low", "close", "volume"]]


def trading_day(index):
    """CME trading day: the session that opens 18:00 ET belongs to the next calendar date."""
    return (pd.DatetimeIndex(index) + pd.Timedelta(hours=6)).normalize()


def resample(df, tf):
    """Left-closed, left-labelled bars. 4h is anchored at the 18:00 open (18/22/02/06/10/14);
    D is the 18:00 -> 17:00 trading day."""
    idx = df.index
    if tf == "4h":
        key = (idx + pd.Timedelta(hours=6)).floor("4h") - pd.Timedelta(hours=6)
        length = pd.Timedelta("4h")
    elif tf == "D":
        key = trading_day(idx) - pd.Timedelta(hours=6)
        length = pd.Timedelta("23h")
    else:
        key = idx.floor(tf)
        length = pd.Timedelta(tf)
    g = df.groupby(key)
    b = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                      "close": g["close"].last(), "volume": g["volume"].sum(min_count=1)})
    b.index.name = "t0"
    b["t1"] = b.index + length
    return b


def load_bars(cfg, instrument="nq", start=None, end=None, use_cache=True, verbose=True):
    """Dict tf -> bars for the configured source, cached to parquet."""
    dc = cfg["data"]
    tag = f"{dc['source']}_{instrument}_{start or 'all'}_{end or 'all'}".replace(":", "")
    cache = os.path.join(dc["cache_dir"], tag)
    tfs = [t for t in TF_ORDER if t != "30s" or dc.get("use_30s")]
    if use_cache and all(os.path.exists(os.path.join(cache, f"{t}.parquet")) for t in tfs):
        return {t: pd.read_parquet(os.path.join(cache, f"{t}.parquet")) for t in tfs}
    if dc["source"] == "proxy":
        base = load_proxy(dc["nq_path"] if instrument == "nq" else dc["es_path"], dc["tick"])
    else:
        base = load_futures(dc["futures_dir"], dc["nq_symbol"] if instrument == "nq" else dc["es_symbol"],
                            dc["tick"], dc.get("roll_adjust", "difference"))
    if start:
        base = base[base.index >= pd.Timestamp(start)]
    if end:
        base = base[base.index < pd.Timestamp(end)]
    step = base.index.to_series().diff().median()
    out = {}
    for t in tfs:
        if t == "1min" and step == pd.Timedelta("1min"):
            b = base.copy()
            b.index.name = "t0"
            b["t1"] = b.index + pd.Timedelta("1min")
        else:
            b = resample(base, t)
        out[t] = b
        if verbose:
            print(f"  [{instrument}] {t:>5s}: {len(b):>9,d} bars", flush=True)
    if use_cache:
        os.makedirs(cache, exist_ok=True)
        for t, b in out.items():
            b.to_parquet(os.path.join(cache, f"{t}.parquet"))
    return out
