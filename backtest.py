"""Intraday FVG + liquidity-draw strategy backtest on Nasdaq-100 1-minute data.

Run:  python3 prep_data.py && python3 backtest.py
Outputs: results/trades.csv, results/summary.csv, results/yearly.csv, results/*.png

Every time below is America/New_York wall-clock time. A bar is labelled by its OPEN time;
it only becomes usable at its CLOSE time (open + timeframe).
"""
import os
import numpy as np
import pandas as pd
from numba import njit

# ---------------------------------------------------------------- parameters
TICK = 0.25                 # NQ / MNQ tick
POINT_VALUE = 2.0           # MNQ $ per point
COMMISSION_RT = 1.24        # $ per MNQ round turn (commission + exchange/NFA fees)
SLIP_TICKS = 1              # adverse slippage on entry and on stop / flatten exits (TP is a resting limit)
MAX_TRADES = 3              # per session per day
FVG5_MAX_AGE_BARS = 1440    # 5m FVGs older than ~5 trading days are dropped
CONFLUENCE_TOL = 0.0005     # a liquidity level within 0.05% of price of the 5m FVG counts as "at" it
SESSIONS = {                # (start, end) as minutes after midnight; Asia crosses midnight
    "NY": (9 * 60 + 30, 16 * 60),
    "Asia": (19 * 60, 2 * 60),
}
OUT = "results"
M = np.int64(60_000_000_000)  # one minute in ns


def load_1m(path="data/nq_1m.parquet"):
    df = pd.read_parquet(path)
    df.index = pd.DatetimeIndex(df.index).as_unit("ns")      # all int64 timestamps below are ns
    # the source (a Nasdaq-100 CFD feed) quotes to 0.001; snap to the 0.25 futures tick
    for col in ("open", "high", "low", "close"):
        df[col] = (df[col] / TICK).round() * TICK
    # trading day: the CME session that opens 18:00 ET belongs to the next calendar day
    df["td"] = (df.index + pd.Timedelta(hours=6)).normalize()
    return df


def resample(df, kind):
    """Return OHLC bars with open time t0 and close time t1 (nominal bucket end)."""
    idx = df.index
    if kind == "5m":
        key, length = idx.floor("5min"), pd.Timedelta("5min")
    elif kind == "1H":
        key, length = idx.floor("1h"), pd.Timedelta("1h")
    elif kind == "4H":   # 4H buckets anchored at the 18:00 session open: 18,22,02,06,10,14
        key, length = (idx + pd.Timedelta(hours=6)).floor("4h") - pd.Timedelta(hours=6), pd.Timedelta("4h")
    elif kind == "D":    # trading day 18:00 -> 17:00
        key, length = df["td"].values - np.timedelta64(6, "h"), pd.Timedelta("23h")
        key = pd.DatetimeIndex(key)
    g = df.groupby(key)
    bars = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(),
                         "low": g["low"].min(), "close": g["close"].last()})
    bars.index.name = "t0"
    bars["t1"] = bars.index + length
    return bars


def find_fvgs(bars):
    """3-candle imbalances. Valid from the close of the 3rd candle.
    bull: low[i] > high[i-2] -> zone [high[i-2], low[i]] (sits below price)
    bear: high[i] < low[i-2] -> zone [high[i], low[i-2]] (sits above price)"""
    h, l = bars["high"].values, bars["low"].values
    out = []
    bull = np.where(l[2:] > h[:-2])[0] + 2
    bear = np.where(h[2:] < l[:-2])[0] + 2
    for i in bull:
        out.append((i, 1, h[i - 2], l[i]))
    for i in bear:
        out.append((i, -1, h[i], l[i - 2]))
    f = pd.DataFrame(out, columns=["bar", "dir", "bot", "top"]).sort_values("bar").reset_index(drop=True)
    f["valid_from"] = bars["t1"].values[f["bar"].values]
    return f


@njit(cache=True)
def _fill_times(t1m, lo, hi, start_idx, direction, bot, top, limit_t):
    """First 1m bar (open time) at/after start_idx that trades fully through the gap."""
    n = len(t1m)
    res = np.full(len(start_idx), np.iinfo(np.int64).max)
    for k in range(len(start_idx)):
        s = start_idx[k]
        limit = limit_t[k]
        for j in range(s, n):
            if t1m[j] > limit:
                break
            if direction[k] == 1:
                if lo[j] <= bot[k]:
                    res[k] = t1m[j]
                    break
            else:
                if hi[j] >= top[k]:
                    res[k] = t1m[j]
                    break
    return res


def add_fill_times(fvg, m1, max_age=None, bars=None):
    """fill_t = first 1m time the gap is traded through. With max_age (in bars of `bars`), the scan
    stops at the close of bar (creation bar + max_age): the engine never looks at the zone after that,
    so the expiry and the fill scan use the same clock (bar count, not calendar minutes)."""
    t = m1.index.values.astype(np.int64)
    start = np.searchsorted(t, fvg["valid_from"].values.astype(np.int64))
    if max_age:
        t1b = bars["t1"].values.astype(np.int64)
        limit_t = t1b[np.minimum(fvg["bar"].values + max_age, len(t1b) - 1)]
    else:
        limit_t = np.full(len(fvg), np.iinfo(np.int64).max)
    fvg["fill_t"] = _fill_times(t, m1["low"].values, m1["high"].values, start,
                                fvg["dir"].values.astype(np.int64), fvg["bot"].values, fvg["top"].values,
                                limit_t.astype(np.int64))
    return fvg


@njit(cache=True)
def _simulate_exit(t, o, h, l, c, i0, end_t, direction, stop, tp, slip):
    """Walk 1m bars from i0 until stop / target / session end.
    Stop and target in the same 1m bar -> stop (conservative).
    Returns exit price, exit-bar index, reason (0 stop, 1 target, 2 session flatten)."""
    n = len(t)
    last = i0 - 1           # if no 1m bar remains before session end, flatten at the entry bar's close
    for j in range(i0, n):
        if t[j] >= end_t:
            break
        last = j
        if direction == 1:
            if l[j] <= stop:
                return min(stop, o[j]) - slip, j, 0
            if h[j] >= tp:
                return tp, j, 1
        else:
            if h[j] >= stop:
                return max(stop, o[j]) + slip, j, 0
            if l[j] <= tp:
                return tp, j, 1
    return c[last] - direction * slip, last, 2


def session_windows(m5, name):
    """Session id (date the session opens) for every 5m bar, and the session end time."""
    s, e = SESSIONS[name]
    tod = m5.index.hour * 60 + m5.index.minute
    day = m5.index.normalize()
    if s < e:
        mask = (tod >= s) & (tod < e)
        sess_day = day
        end = day + pd.Timedelta(minutes=e)
    else:
        mask = (tod >= s) | (tod < e)
        sess_day = day.where(tod >= s, day - pd.Timedelta(days=1))
        end = sess_day + pd.Timedelta(days=1, minutes=e)
    return np.asarray(mask), pd.DatetimeIndex(sess_day), pd.DatetimeIndex(end)


def build_context(m1):
    m5 = resample(m1, "5m")
    h1 = resample(m1, "1H")
    h4 = resample(m1, "4H")
    d1 = resample(m1, "D")

    # ---- 1H bias: EMA9/EMA21 of 1H closes; VWAP anchored at the 18:00 trading-day open.
    # Source has no volume, so VWAP = running mean of 1m typical price since the anchor.
    h1["ema9"] = h1["close"].ewm(span=9, adjust=False).mean()
    h1["ema21"] = h1["close"].ewm(span=21, adjust=False).mean()
    tp = (m1["high"] + m1["low"] + m1["close"]) / 3
    vw = tp.groupby(m1["td"]).cumsum() / tp.groupby(m1["td"]).cumcount().add(1)
    h1["vwap"] = vw.groupby(m1.index.floor("1h")).last().reindex(h1.index).values
    bull = (h1.close > h1.ema9) & (h1.close > h1.ema21) & (h1.ema9 > h1.ema21) & (h1.close > h1.vwap)
    bear = (h1.close < h1.ema9) & (h1.close < h1.ema21) & (h1.ema9 < h1.ema21) & (h1.close < h1.vwap)
    h1["bias"] = np.where(bull, 1, np.where(bear, -1, 0))

    # Everything evaluated at a 5m bar's CLOSE (decision time) uses only HTF bars closed by then.
    dec = m5["t1"].values
    def last_closed(htf):
        k = np.searchsorted(htf["t1"].values, dec, side="right") - 1
        return k
    k1, k4, kd = last_closed(h1), last_closed(h4), last_closed(d1)
    m5["bias"] = np.where(k1 >= 0, h1["bias"].values[np.maximum(k1, 0)], 0)
    levels = []
    for htf, k in ((h1, k1), (h4, k4), (d1, kd)):
        for col in ("high", "low"):
            levels.append(np.where(k >= 0, htf[col].values[np.maximum(k, 0)], np.nan))
    m5_levels = np.vstack(levels).T          # (n5, 6) prev 1H/4H/D high & low

    # ---- FVGs
    f5 = add_fill_times(find_fvgs(m5), m1, max_age=FVG5_MAX_AGE_BARS, bars=m5)
    htf_f = []
    for name, b in (("1H", h1), ("4H", h4), ("D", d1)):
        f = add_fill_times(find_fvgs(b), m1)
        f["tf"] = name
        htf_f.append(f)
    htf_f = pd.concat(htf_f, ignore_index=True)
    return m5, m5_levels, f5, htf_f


def run(m1, m5, m5_levels, f5, htf_f, session, rr, dir_mode, confluence):
    t1m = m1.index.values.astype(np.int64)
    o1, h1_, l1, c1 = (m1[c].values for c in ("open", "high", "low", "close"))
    t0 = m5.index.values.astype(np.int64)
    t1 = m5["t1"].values.astype(np.int64)
    o, h, l, c, bias = (m5[x].values for x in ("open", "high", "low", "close", "bias"))
    mask, sess_day, sess_end = session_windows(m5, session)
    sess_end_i = sess_end.values.astype(np.int64)
    f_bar, f_dir, f_bot, f_top = (f5[x].values for x in ("bar", "dir", "bot", "top"))
    f_fill = f5["fill_t"].values
    hv_from = htf_f["valid_from"].values.astype(np.int64)
    h_fill, h_bot, h_top = htf_f["fill_t"].values, htf_f["bot"].values, htf_f["top"].values

    trades = []
    cur_day, n_today, next_ok = None, 0, np.int64(0)
    for k in np.where(mask & (bias != 0))[0]:
        if t1[k] >= sess_end_i[k]:              # bar closes at session end -> no new entry
            continue
        if sess_day[k] != cur_day:
            cur_day, n_today = sess_day[k], 0
        if n_today >= MAX_TRADES or t0[k] < next_ok:
            continue
        b = bias[k]
        if (b == 1 and c[k] <= o[k]) or (b == -1 and c[k] >= o[k]):
            continue                            # entry candle must close in trade direction
        lo_i = np.searchsorted(f_bar, k - FVG5_MAX_AGE_BARS)
        hi_i = np.searchsorted(f_bar, k)       # FVGs whose 3rd candle closed before this bar
        if hi_i <= lo_i:
            continue
        sl = slice(lo_i, hi_i)
        d, bot, top, fill = f_dir[sl], f_bot[sl], f_top[sl], f_fill[sl]
        alive = fill >= t1[k]                   # not traded through before or during this bar
        tapped = np.where(d == 1, l[k] <= top, h[k] >= bot)
        ok = alive & tapped
        if dir_mode == "match":
            ok &= d == b
        entry = c[k] + b * SLIP_TICKS * TICK
        stop = np.where(b == 1, bot, top)       # far edge of the gap
        ok &= (entry - stop) * b > 0
        cand = np.where(ok)[0][::-1]            # most recent first
        chosen = None
        for j in cand:
            if confluence:
                zlo, zhi = bot[j], top[j]
                tol = CONFLUENCE_TOL * c[k]
                lv = m5_levels[k]
                hit = np.any((lv >= zlo - tol) & (lv <= zhi + tol))
                if not hit:
                    act = (hv_from <= t1[k]) & (h_fill >= t1[k])
                    hit = np.any(act & (h_bot <= zhi + tol) & (h_top >= zlo - tol))
                if not hit:
                    continue
            chosen = j
            break
        if chosen is None:
            continue
        stp = stop[chosen]
        risk = (entry - stp) * b
        tgt = entry + b * rr * risk
        i0 = np.searchsorted(t1m, t1[k])        # first 1m bar after the entry close
        px, jx, why = _simulate_exit(t1m, o1, h1_, l1, c1, i0, sess_end_i[k], b, stp, tgt, SLIP_TICKS * TICK)
        pts = (px - entry) * b
        pnl = pts * POINT_VALUE - COMMISSION_RT
        raw_exit = px if why == 1 else px + b * SLIP_TICKS * TICK
        gross_r = (raw_exit - c[k]) * b / risk          # before slippage and commission
        trades.append(dict(session=session, rr=rr, fvg_mode=dir_mode, confluence=confluence,
                           session_date=sess_day[k].date(), entry_time=pd.Timestamp(t1[k]),
                           exit_time=pd.Timestamp(t1m[jx]) + pd.Timedelta(minutes=1),
                           direction="long" if b == 1 else "short", entry=entry, stop=stp, target=tgt,
                           exit=px, exit_reason=("stop", "target", "session_end")[why],
                           fvg_bot=bot[chosen], fvg_top=top[chosen], fvg_dir=int(d[chosen]),
                           risk_pts=risk, pnl_pts=pts, pnl_usd=pnl, r=pnl / (risk * POINT_VALUE), gross_r=gross_r))
        n_today += 1
        next_ok = t1m[jx] + M
    return pd.DataFrame(trades)


def max_dd(x):
    eq = np.cumsum(x)
    return float(np.max(np.maximum.accumulate(np.concatenate([[0], eq]))[1:] - eq)) if len(x) else 0.0


def stats(t):
    if t.empty:
        return dict(trades=0)
    w = t.pnl_usd > 0
    gp, gl = t.r[t.r > 0].sum(), -t.r[t.r < 0].sum()
    return dict(trades=len(t), win_rate=w.mean(), gross_avg_r=t.gross_r.mean(), avg_r=t.r.mean(), expectancy_usd=t.pnl_usd.mean(),
                profit_factor=gp / gl if gl > 0 else np.inf, total_r=t.r.sum(), net_usd=t.pnl_usd.sum(),
                max_dd_r=max_dd(t.r.values), max_dd_usd=max_dd(t.pnl_usd.values),
                pct_target=(t.exit_reason == "target").mean(), pct_stop=(t.exit_reason == "stop").mean(),
                pct_flat=(t.exit_reason == "session_end").mean(), median_risk_pts=t.risk_pts.median())


def charts(trades):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    colors = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]   # validated categorical order
    variants = [("match", True), ("match", False), ("any", True), ("any", False)]
    for session in SESSIONS:
        for rr in (2, 3):
            fig, ax = plt.subplots(figsize=(11, 5.5), dpi=150)
            for col, (fm, cf) in zip(colors, variants):
                t = trades[(trades.session == session) & (trades.rr == rr) &
                           (trades.fvg_mode == fm) & (trades.confluence == cf)].sort_values("entry_time")
                if t.empty:
                    continue
                lab = f"{'bias-matched' if fm == 'match' else 'any'} FVG, {'with' if cf else 'no'} confluence"
                ax.plot(t.entry_time, t.r.cumsum(), color=col, lw=1.6,
                        label=f"{lab}: n={len(t)}, win {t.pnl_usd.gt(0).mean():.1%}, total {t.r.sum():+.0f}R")
            ax.axhline(0, color="#999", lw=0.8)
            ax.set_title(f"{session} session, 1:{rr} R:R: cumulative R (1 MNQ, net of costs)", loc="left", fontsize=11)
            ax.set_ylabel("Cumulative R")
            for s in ("top", "right"):
                ax.spines[s].set_visible(False)
            ax.grid(axis="y", color="#e6e6e3", lw=0.6)
            ax.legend(frameon=False, fontsize=8, loc="best")
            fig.tight_layout()
            fig.savefig(f"{OUT}/equity_{session}_1to{rr}.png")
            plt.close(fig)


def main():
    os.makedirs(OUT, exist_ok=True)
    m1 = load_1m()
    m5, lv, f5, hf = build_context(m1)
    print(f"bars 1m={len(m1)} 5m={len(m5)}  FVG 5m={len(f5)} HTF={len(hf)}")
    all_t = []
    for session in SESSIONS:
        for rr in (2, 3):
            for fm in ("match", "any"):
                for cf in (True, False):
                    t = run(m1, m5, lv, f5, hf, session, rr, fm, cf)
                    print(session, rr, fm, cf, len(t), flush=True)
                    all_t.append(t)
    trades = pd.concat(all_t, ignore_index=True)
    trades.to_csv(f"{OUT}/trades.csv", index=False)
    keys = ["session", "rr", "fvg_mode", "confluence"]
    summ = trades.groupby(keys).apply(lambda g: pd.Series(stats(g))).reset_index()
    summ["breakeven_wr"] = 1 / (1 + summ.rr)
    summ.to_csv(f"{OUT}/summary.csv", index=False)
    trades["year"] = pd.to_datetime(trades.session_date).dt.year
    yearly = trades.groupby(keys + ["year"]).apply(lambda g: pd.Series(stats(g))).reset_index()
    yearly.to_csv(f"{OUT}/yearly.csv", index=False)
    charts(trades)
    # stop-size sensitivity: costs are fixed in $, so tight stops cost far more R
    trades["risk_bucket"] = pd.cut(trades.risk_pts, [0, 1, 2.5, 5, 10, 20, 1e9],
                                   labels=["<=1pt", "1-2.5", "2.5-5", "5-10", "10-20", ">20"])
    rb = trades.groupby(keys + ["risk_bucket"], observed=True).apply(lambda g: pd.Series(stats(g))).reset_index()
    rb.to_csv(f"{OUT}/by_stop_size.csv", index=False)
    pd.set_option("display.width", 250)
    print(summ.round(3).to_string())


if __name__ == "__main__":
    main()
