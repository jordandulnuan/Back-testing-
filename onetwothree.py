"""The 1-2-3 method on NQ: HTF FVG arming -> 1m/2m/3m FVG inversions -> entry.

Run after prep_data.py has built data/nq_1m.parquet:
    python3 onetwothree.py --dev-only   # 2010-11 .. 2020-12, used once for bug checks
    python3 onetwothree.py              # full run, made once with rules frozen
Outputs: results/onetwothree/

Rules are written for SHORTS and mirrored for longs. Every parameter was fixed before any result.
Bars are labelled by their open time and are usable only at their close. All times New York.
"""
import os
import sys
import numpy as np
import pandas as pd
from numba import njit
from scipy.stats import binomtest

from backtest import load_1m, max_dd

TICK = 0.25
POINT_VALUE = 2.0
COMMISSION_RT = 1.24
SLIP = TICK                     # entries, stop exits and the 5-day market exit
HTF_MAX_BARS = 120              # HTF zone valid for 120 bars of its own timeframe
MAX_TRADES_DAY = 2
HOLD_DAYS = 5                   # close at market at the end of the 5th trading day (entry day = day 1)
INV_SCAN = 2000                 # search window (bars) for an LTF FVG's first inversion; arms never outlive a day
NY = (9 * 60 + 30, 16 * 60)
DEV_END = pd.Timestamp("2021-01-01")
OUT = "results/onetwothree"
M = np.int64(60_000_000_000)
BIG = np.iinfo(np.int64).max


# ------------------------------------------------------------------ bars
def bars(df, kind):
    idx = df.index
    if kind == "4H":       # anchored at the 18:00 open: 18, 22, 02, 06, 10, 14
        key, length = (idx + pd.Timedelta(hours=6)).floor("4h") - pd.Timedelta(hours=6), pd.Timedelta("4h")
    elif kind == "D":      # trading day 18:00 -> 17:00
        key, length = pd.DatetimeIndex(df["td"].values - np.timedelta64(6, "h")), pd.Timedelta("23h")
    else:
        key, length = idx.floor(kind), pd.Timedelta(kind)
    g = df.groupby(key)
    b = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                      "close": g["close"].last()})
    b["t1"] = b.index + length
    return b


def ns(x):
    return np.asarray(x).astype("datetime64[ns]").astype(np.int64)


# ------------------------------------------------------------------ HTF zones
def htf_zones(b, tf):
    """Bearish FVG (short zone): low[c1] > high[c3] -> [high3, low1]; bullish (long zone): high[c1] < low[c3].
    Active from c3 close. Invalid at the close of the first candle that closes fully through it
    (short: close > top; long: close < bottom) or after 120 bars of this timeframe."""
    h, l, c, t1 = b.high.values, b.low.values, b.close.values, ns(b.t1)
    rows = []
    n = len(b)
    for i in range(2, n):
        for side, ok, bot, top in ((-1, l[i - 2] > h[i], h[i], l[i - 2]), (1, h[i - 2] < l[i], h[i - 2], l[i])):
            if not ok:
                continue
            end = min(n - 1, i + HTF_MAX_BARS)
            inval = t1[end] if end > i else t1[i]
            for j in range(i + 1, end + 1):
                if (side == -1 and c[j] > top) or (side == 1 and c[j] < bot):
                    inval = t1[j]
                    break
            rows.append((tf, side, bot, top, t1[i], inval))
    return pd.DataFrame(rows, columns=["tf", "side", "bot", "top", "active_t", "inval_t"])


# ------------------------------------------------------------------ LTF FVG inversions
@njit(cache=True)
def _inversions(h, l, c, t1, side):
    """For each LTF FVG that opposes `side` (bullish FVGs for shorts): completion time and the close time
    of the first later candle whose close is beyond its far side (short: close < bottom)."""
    n = len(h)
    comp, inv = [], []
    for i in range(2, n):
        if side == -1 and l[i] > h[i - 2]:          # bullish FVG, bottom = high[i-2]
            edge = h[i - 2]
        elif side == 1 and h[i] < l[i - 2]:         # bearish FVG, top = low[i-2]
            edge = l[i - 2]
        else:
            continue
        it = BIG
        for j in range(i + 1, min(n, i + 1 + INV_SCAN)):
            if (side == -1 and c[j] < edge) or (side == 1 and c[j] > edge):
                it = t1[j]
                break
        comp.append(t1[i])
        inv.append(it)
    return np.array(comp, dtype=np.int64), np.array(inv, dtype=np.int64)


def ltf_inversions(b):
    out = {}
    for side in (-1, 1):
        comp, inv = _inversions(b.high.values, b.low.values, b.close.values, ns(b.t1), side)
        out[side] = (comp, inv)
    return out


def first_inversion(tab, ta, death):
    """Earliest inversion time (< death) among FVGs completed in (ta, death)."""
    comp, inv = tab
    a, e = np.searchsorted(comp, ta, side="right"), np.searchsorted(comp, death, side="left")
    if e <= a:
        return BIG
    x = inv[a:e]
    x = x[x < death]
    return x.min() if len(x) else BIG


# ------------------------------------------------------------------ 15m targets
@njit(cache=True)
def _mitigation(t15, lo15, hi15, t1m, lo1, hi1, start15, edge, side):
    """First 1m time price trades into a 15m FVG after it completes. Scans 15m bars, then refines on 1m."""
    res = np.full(len(edge), BIG)
    n15, n1 = len(t15), len(t1m)
    for k in range(len(edge)):
        for j in range(start15[k], n15):
            if (side[k] == 1 and lo15[j] <= edge[k]) or (side[k] == -1 and hi15[j] >= edge[k]):
                # refine inside this 15m bar on 1m data
                a = np.searchsorted(t1m, t15[j])
                b = np.searchsorted(t1m, t15[j] + 15 * 60_000_000_000)
                for q in range(a, min(b, n1)):
                    if (side[k] == 1 and lo1[q] <= edge[k]) or (side[k] == -1 and hi1[q] >= edge[k]):
                        res[k] = t1m[q]
                        break
                break
    return res


def fvg15(b15, m1):
    """15m FVGs: bullish (targets for shorts, near edge = top) and bearish (targets for longs, near edge =
    bottom). Unmitigated until a 1m bar trades into the near edge."""
    h, l, t1 = b15.high.values, b15.low.values, ns(b15.t1)
    i_b = np.where(l[2:] > h[:-2])[0] + 2
    i_s = np.where(h[2:] < l[:-2])[0] + 2
    f = pd.concat([pd.DataFrame({"side": 1, "bot": h[i_b - 2], "top": l[i_b], "bar": i_b}),
                   pd.DataFrame({"side": -1, "bot": h[i_s], "top": l[i_s - 2], "bar": i_s})])
    f["comp_t"] = t1[f.bar.values]
    f["edge"] = np.where(f.side == 1, f.top, f.bot)
    t15 = ns(b15.index)
    f["mit_t"] = _mitigation(t15, l, h, ns(m1.index), m1.low.values, m1.high.values,
                             f.bar.values + 1, f.edge.values.astype(float), f.side.values.astype(np.int64))
    return f.sort_values("comp_t").reset_index(drop=True)


def target_15m(f15, side, entry, t_entry):
    """Nearest unmitigated opposing 15m FVG beyond entry (short: bullish FVG with top below entry)."""
    want = 1 if side == -1 else -1
    s = f15[(f15.side.values == want) & (f15.comp_t.values <= t_entry) & (f15.mit_t.values > t_entry)]
    if side == -1:
        s = s[s.top < entry]
        return s.top.max() if len(s) else None
    s = s[s.bot > entry]
    return s.bot.min() if len(s) else None


# ------------------------------------------------------------------ arms and triggers
def candidates(zones, m1, inv):
    """One arm per zone per trading day: the first 1m bar that trades into the active zone from the far side
    (short: high >= bottom while the previous 1m close was below the bottom). The arm dies at the earliest of:
    trading-day end (17:00), zone invalidation, or a 1m bar trading to the stop level (top + 1 tick).
    The trigger is the first time all of 1m, 2m and 3m have an inversion of an opposing FVG formed after
    arming; entry is the open of the 1m bar starting then."""
    t = ns(m1.index)
    o, h, l, c = (m1[x].values for x in ("open", "high", "low", "close"))
    td = ns(m1.td)
    prev_c = np.r_[np.nan, c[:-1]]
    out, n_arms = [], 0
    for z in zones.itertuples():
        a, e = np.searchsorted(t, z.active_t), np.searchsorted(t, z.inval_t)
        if e <= a:
            continue
        if z.side == -1:
            arm = (h[a:e] >= z.bot) & (prev_c[a:e] < z.bot)
            stop = z.top + TICK
        else:
            arm = (l[a:e] <= z.top) & (prev_c[a:e] > z.top)
            stop = z.bot - TICK
        idx = np.where(arm)[0] + a
        if not len(idx):
            continue
        _, first = np.unique(td[idx], return_index=True)          # first arm of each trading day
        for k in idx[first]:
            n_arms += 1
            day = td[k]
            day_end = day + 17 * 60 * M                              # 17:00 of the trading date
            ta = t[k] + M                                           # known at the arming bar's close
            stop_hit = (h[k:] >= stop) if z.side == -1 else (l[k:] <= stop)
            de = np.searchsorted(t, min(day_end, z.inval_t))
            sh = np.where(stop_hit[:de - k])[0]
            death = min(day_end, z.inval_t, t[k + sh[0]] if len(sh) else BIG)
            trig = max(first_inversion(inv[tf][z.side], ta, death) for tf in ("1min", "2min", "3min"))
            if trig >= death:
                continue
            ei = np.searchsorted(t, trig)                           # 1m bar opening at/after the trigger
            if ei >= len(t) or t[ei] >= death:
                continue
            out.append(dict(zone_tf=z.tf, side=z.side, zone_bot=z.bot, zone_top=z.top, zone_active=z.active_t,
                            arm_t=t[k], trigger_t=trig, entry_i=ei, entry_t=t[ei], open=o[ei], stop=stop,
                            day=day))
    return pd.DataFrame(out), n_arms


# ------------------------------------------------------------------ exits
@njit(cache=True)
def _exit(o, h, l, c, i0, last_i, side, stop, tgt, slip):
    """Entry at the open of bar i0. Stop and target in the same bar -> stop. Time exit at close of last_i."""
    for j in range(i0, last_i + 1):
        if side == -1:
            if h[j] >= stop:
                return max(stop, o[j]) + slip, j, 0
            if l[j] <= tgt:
                return tgt, j, 1
        else:
            if l[j] <= stop:
                return min(stop, o[j]) - slip, j, 0
            if h[j] >= tgt:
                return tgt, j, 1
    return c[last_i] - side * slip, last_i, 2


def run_variant(cands, m1, f15, target, session, name, day_last_idx, td_list):
    t = ns(m1.index)
    o, h, l, c = (m1[x].values for x in ("open", "high", "low", "close"))
    td_pos = {d: i for i, d in enumerate(td_list)}
    cands = cands.sort_values(["entry_t", "zone_tf"]).reset_index(drop=True)
    skips = dict(no_15m_target=0, open_trade=0, day_limit=0, entry_beyond_stop=0, outside_session=0)
    trades, free_at, per_day = [], 0, {}
    for s in cands.itertuples():
        mins = int((s.entry_t // M) % (24 * 60))       # naive local time -> minute of day
        if session == "NY" and not (NY[0] <= mins < NY[1]):
            skips["outside_session"] += 1
            continue
        if s.entry_t < free_at:
            skips["open_trade"] += 1
            continue
        if per_day.get(s.day, 0) >= MAX_TRADES_DAY:
            skips["day_limit"] += 1
            continue
        entry = s.open + s.side * SLIP
        risk = (s.stop - entry) * -s.side
        if risk <= 0:
            skips["entry_beyond_stop"] += 1
            continue
        if target == "15m":
            tgt = target_15m(f15, s.side, s.open, s.entry_t)
            if tgt is None:
                skips["no_15m_target"] += 1
                continue
        else:
            tgt = entry + s.side * float(target[0]) * risk
        reward = (tgt - entry) * s.side
        if reward <= 0:
            skips["no_15m_target"] += 1
            continue
        p = td_pos[s.day]
        last_i = day_last_idx[td_list[min(p + HOLD_DAYS - 1, len(td_list) - 1)]]
        px, xi, why = _exit(o, h, l, c, s.entry_i, last_i, s.side, s.stop, tgt, SLIP)
        pts = (px - entry) * s.side
        pnl = pts * POINT_VALUE - COMMISSION_RT
        raw_exit = px if why == 1 else px + s.side * SLIP
        trades.append(dict(variant=name, zone_tf=s.zone_tf, direction="short" if s.side == -1 else "long",
                           day=pd.Timestamp(s.day).date(), arm_time=pd.Timestamp(s.arm_t),
                           entry_time=pd.Timestamp(s.entry_t), exit_time=pd.Timestamp(t[xi]) + pd.Timedelta(minutes=1),
                           zone_bot=s.zone_bot, zone_top=s.zone_top, entry=entry, stop=s.stop, target=tgt,
                           exit=px, exit_reason=("stop", "target", "time_5d")[why], risk_pts=risk,
                           planned_rr=reward / risk, pnl_pts=pts, pnl_usd=pnl, r=pnl / (risk * POINT_VALUE),
                           gross_r=(raw_exit - s.open) * s.side / risk,
                           win=bool(why == 1 or (why == 2 and pnl > 0))))
        per_day[s.day] = per_day.get(s.day, 0) + 1
        free_at = t[xi] + M
    return trades, skips


# ------------------------------------------------------------------ stats
def wilson(k, n, z=1.96):
    if n == 0:
        return np.nan, np.nan
    p = k / n
    d = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / d
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return mid - half, mid + half


def stats(t):
    n = len(t)
    if n == 0:
        return pd.Series(dict(trades=0))
    w = int(t.win.sum())
    lo, hi = wilson(w, n)
    wins, losses = t[t.win], t[~t.win]
    aw_r, al_r = wins.r.mean() if len(wins) else 0.0, -losses.r.mean() if len(losses) else 0.0
    gp, gl = t.pnl_usd[t.pnl_usd > 0].sum(), -t.pnl_usd[t.pnl_usd < 0].sum()
    return pd.Series(dict(
        trades=n, wins=w, losses=n - w, win_rate=w / n, ci95_lo=lo, ci95_hi=hi,
        binom_p_vs_50=binomtest(w, n, 0.5).pvalue,
        breakeven_win_rate=al_r / (aw_r + al_r) if aw_r + al_r > 0 else np.nan,
        avg_win_r=aw_r, avg_loss_r=-al_r, avg_win_usd=wins.pnl_usd.mean() if len(wins) else 0.0,
        avg_loss_usd=losses.pnl_usd.mean() if len(losses) else 0.0,
        gross_r=t.gross_r.mean(), expectancy_r=t.r.mean(), expectancy_usd=t.pnl_usd.mean(),
        net_usd=t.pnl_usd.sum(), profit_factor=gp / gl if gl > 0 else np.inf,
        max_dd_usd=max_dd(t.pnl_usd.values), max_dd_r=max_dd(t.r.values),
        pct_time_exit=(t.exit_reason == "time_5d").mean(), median_risk_pts=t.risk_pts.median(),
        avg_planned_rr=t.planned_rr.mean(), low_n=n < 300))


def charts(tr, summ):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    colors = {"15m": "#2a78d6", "1R": "#eb6834", "2R": "#1baf7a", "3R": "#eda100"}
    fig, axes = plt.subplots(2, 3, figsize=(20, 9), dpi=120, sharex=True)
    for r, sess in enumerate(("all", "NY")):
        for cidx, htf in enumerate(("D", "4H", "D+4H")):
            ax = axes[r, cidx]
            for tg, col in colors.items():
                name = f"{htf} | {tg} | {sess}"
                t = tr[tr.variant == name].sort_values("entry_time")
                if t.empty:
                    continue
                ax.plot(t.entry_time, t.pnl_usd.cumsum(), color=col, lw=1.4,
                        label=f"target {tg}: n={len(t)}, win {t.win.mean():.0%}, {t.pnl_usd.sum():+,.0f} $")
            ax.axhline(0, color="#52514e", lw=0.8)
            ax.axvline(DEV_END, color="#52514e", lw=0.8, ls="--")
            ax.set_title(f"{htf} FVG, {'all hours' if sess == 'all' else 'NY 9:30-16:00'} entries", loc="left", fontsize=10)
            ax.legend(fontsize=7, frameon=False, loc="lower left")
            ax.grid(axis="y", color="#e6e6e3", lw=0.6)
            for sp in ("top", "right"):
                ax.spines[sp].set_visible(False)
            if cidx == 0:
                ax.set_ylabel("Cumulative net $ (1 MNQ)")
    fig.suptitle("1-2-3 method: cumulative net $ per variant. Dashed = start of 2021-26 hold-out", x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(f"{OUT}/equity_grid.png")
    plt.close(fig)

    s = summ.sort_values("win_rate")
    fig, ax = plt.subplots(figsize=(10, 9), dpi=130)
    y = np.arange(len(s))
    ax.errorbar(s.win_rate * 100, y, xerr=[(s.win_rate - s.ci95_lo) * 100, (s.ci95_hi - s.win_rate) * 100],
                fmt="o", color="#2a78d6", ecolor="#86b6ef", capsize=3, ms=5, label="win rate, 95% CI")
    ax.plot(s.breakeven_win_rate * 100, y, "|", color="#eb6834", ms=14, mew=2, label="breakeven win rate (from avg win/loss)")
    ax.axvline(50, color="#52514e", lw=1, ls="--")
    ax.text(50.5, len(s) - 0.5, "coin flip 50%", fontsize=8, color="#52514e")
    ax.set_yticks(y)
    ax.set_yticklabels([f"{v}  (n={int(n)}{', <300' if n < 300 else ''})" for v, n in zip(s.variant, s.trades)], fontsize=8)
    ax.set_xlabel("%")
    ax.legend(frameon=False, fontsize=8, loc="lower right")
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.set_title("Win rate vs 50% and vs breakeven, full period", loc="left", fontsize=10)
    fig.tight_layout()
    fig.savefig(f"{OUT}/win_rates.png")
    plt.close(fig)


def main():
    dev_only = "--dev-only" in sys.argv
    sfx = "_dev" if dev_only else ""
    os.makedirs(OUT, exist_ok=True)
    m1 = load_1m()
    if dev_only:
        m1 = m1[m1.index < DEV_END]
    b = {k: bars(m1, k) for k in ("2min", "3min", "5min", "15min", "4H", "D")}   # 5m built, unused by rules
    b["1min"] = m1[["open", "high", "low", "close"]].assign(t1=m1.index + pd.Timedelta("1min"))
    inv = {tf: ltf_inversions(b[tf]) for tf in ("1min", "2min", "3min")}
    f15 = fvg15(b["15min"], m1)
    zones = {"D": htf_zones(b["D"], "D"), "4H": htf_zones(b["4H"], "4H")}
    zones["D+4H"] = pd.concat([zones["D"], zones["4H"]], ignore_index=True)
    print({k: len(v) for k, v in zones.items()}, "15m FVGs", len(f15), flush=True)
    td_list = np.array(sorted(set(ns(m1.td))))
    t = ns(m1.index)
    tdv = ns(m1.td)
    last_idx = np.r_[np.where(np.diff(tdv) != 0)[0], len(tdv) - 1]
    day_last_idx = dict(zip(tdv[last_idx], last_idx))
    cand = {}
    arm_counts = {}
    for k in ("D", "4H"):
        cand[k], arm_counts[k] = candidates(zones[k], m1, inv)
        print(k, "arms", arm_counts[k], "triggers", len(cand[k]), flush=True)
    cand["D+4H"] = pd.concat([cand["D"], cand["4H"]], ignore_index=True)
    rows, skip_rows = [], []
    for htf in ("D", "4H", "D+4H"):
        for tg in ("15m", "1R", "2R", "3R"):
            for sess in ("all", "NY"):
                name = f"{htf} | {tg} | {sess}"
                tr, sk = run_variant(cand[htf], m1, f15, tg, sess, name, day_last_idx, td_list)
                rows += tr
                skip_rows.append(dict(variant=name, triggers=len(cand[htf]), taken=len(tr), **sk))
                print(f"{name:22s} trades={len(tr):5d} skips={sk}", flush=True)
    trd = pd.DataFrame(rows)
    trd["year"] = pd.to_datetime(trd.day).dt.year
    trd["period"] = np.where(pd.to_datetime(trd.day) < DEV_END, "dev 2010-2020", "holdout 2021-2026")
    trd.to_csv(f"{OUT}/trades{sfx}.csv", index=False)
    pd.DataFrame(skip_rows).to_csv(f"{OUT}/skips{sfx}.csv", index=False)
    pd.Series({**{f"arms_{k}": v for k, v in arm_counts.items()},
               **{f"zones_{k}": len(v) for k, v in zones.items()}}).to_csv(f"{OUT}/counts{sfx}.csv")
    order = list(dict.fromkeys(trd.variant))
    summ = trd.groupby("variant", sort=False).apply(stats, include_groups=False).reset_index()
    summ.to_csv(f"{OUT}/summary{sfx}.csv", index=False)
    for by, fn in ((["period"], "summary_by_period"), (["year"], "yearly"), (["direction"], "by_direction")):
        trd.groupby(["variant"] + by, sort=False).apply(stats, include_groups=False).reset_index().to_csv(
            f"{OUT}/{fn}{sfx}.csv", index=False)
    if not dev_only:
        charts(trd, summ)
    pd.set_option("display.width", 250)
    print(summ[["variant", "trades", "wins", "win_rate", "ci95_lo", "ci95_hi", "binom_p_vs_50", "breakeven_win_rate",
                "expectancy_r", "net_usd", "profit_factor"]].round(3).to_string(index=False))


if __name__ == "__main__":
    main()
