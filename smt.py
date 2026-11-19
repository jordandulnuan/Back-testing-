"""NQ/ES SMT-divergence intraday model.

Run after prep_data.py has built data/nq_1m.parquet and data/es_1m.parquet:
    python3 smt.py --dev-only      # 2010-11 .. 2020-12, used once for bug checks
    python3 smt.py                 # full 2010-11 .. 2026-11 run, made once with rules frozen
Outputs: results/smt/

Rules are written for SHORTS; longs are mirrored. Every parameter below was fixed before any result.
Bars are labelled by their open time and are usable only at their close. All times New York.
"""
import os
import sys
import numpy as np
import pandas as pd

from backtest import load_1m, _fill_times, max_dd

# ------------------------------------------------------------------ fixed parameters
TICK = 0.25
POINT_VALUE = 2.0            # MNQ $/point
COMMISSION_RT = 1.24
SLIP = 1 * TICK              # entries, stop exits (incl. break-even stop) and the 16:00 flatten
MAX_TRADES = 2               # per day, one at a time
FLAT_T = 16 * 60             # flatten at 16:00
OPEN_T = 9 * 60 + 30
WINDOW_CAP_T = 12 * 60       # setup window closes at 12:00 or 90 min after the SMT bar, whichever first
WINDOW_MIN = 90
DISP_MULT = 1.5              # 2 closed 1H bodies >= 1.5 x average body of the 14 before them
DISP_LOOKBACK = 14
MIN_RR = 2.0                 # DOL must be >= 2R from entry (main run)
FVG5_AGE = 1440              # internal 5m FVGs considered up to 1440 5m bars old (~5 trading days)
DEV_END = pd.Timestamp("2021-01-01")
OUT = "results/smt"
NS_MIN = np.int64(60_000_000_000)

VARIANTS = {   # name: (break-even rule on?, 2R minimum on?)
    "main": (True, True),
    "no_breakeven": (False, True),
    "no_min_2R": (True, False),
}


# ------------------------------------------------------------------ data
def load_joined(dev_only):
    """Returns (nq, joined). `joined` holds only minutes present in BOTH feeds and is used solely for the
    SMT comparison, so NQ and ES are always measured over identical timestamps. Everything else (NDOG,
    1H, 3m, internal 5m FVGs, exits) uses the complete NQ 1m series so a missing ES minute can never
    hide an NQ stop or target."""
    nq, es = load_1m("data/nq_1m.parquet"), load_1m("data/es_1m.parquet")
    if dev_only:
        nq, es = nq[nq.index < DEV_END], es[es.index < DEV_END]
    idx = nq.index.intersection(es.index)
    j = nq.loc[idx, ["open", "high", "low", "close", "td"]].join(
        es.loc[idx, ["open", "high", "low", "close"]].add_prefix("es_"))
    return nq, j, len(es)


def bars(j, freq, cols=("open", "high", "low", "close"), prefix=""):
    key = j.index.floor(freq)
    g = j.groupby(key)
    b = pd.DataFrame({c: getattr(g[prefix + c], {"open": "first", "high": "max", "low": "min", "close": "last"}[c])()
                      for c in cols})
    b["t1"] = b.index + pd.Timedelta(freq)
    return b


def ndog(j):
    """New Day Opening Gap for trading day D: the last 1m close before 17:00 ET on the previous trading
    day (bars quoted after 17:00 by the CFD feed are ignored, futures are closed then) and the first
    1m open at 18:00-18:59 ET that starts day D. Requires the pre-break close to be at/after 16:00."""
    t = j.index
    m = t.hour * 60 + t.minute
    g = j.groupby("td")
    first = g.head(1)
    first = first[(first.index.hour == 18)]
    pre = j[(m < 17 * 60) & (m >= 16 * 60) & (t.normalize() == j["td"])]   # same calendar date as its td
    last = pre.groupby("td").tail(1)
    close17 = pd.Series(last["close"].values, index=last["td"].values)
    open18 = pd.Series(first["open"].values, index=first["td"].values)
    tds = pd.Index(sorted(set(j["td"])))
    prev_td = pd.Series(tds[:-1], index=tds[1:])                           # previous trading day
    d = pd.DataFrame({"open18": open18}).dropna()
    d["prev_td"] = prev_td.reindex(d.index)
    d["prev_close17"] = close17.reindex(d["prev_td"].values).values
    d = d.dropna()
    d = d[(d.index - d.prev_td) <= pd.Timedelta(days=4)]
    d["gap_hi"] = d[["open18", "prev_close17"]].max(axis=1)
    d["gap_lo"] = d[["open18", "prev_close17"]].min(axis=1)
    return d[["prev_close17", "open18", "gap_lo", "gap_hi"]]


def fvgs5(b5, j):
    """5m NQ FVGs with the first 1m time price trades INTO them (bullish: low <= top; bearish: high >= bot)."""
    h, l = b5["high"].values, b5["low"].values
    rows = [(i, 1, h[i - 2], l[i]) for i in np.where(l[2:] > h[:-2])[0] + 2] + \
           [(i, -1, h[i], l[i - 2]) for i in np.where(h[2:] < l[:-2])[0] + 2]
    f = pd.DataFrame(rows, columns=["bar", "dir", "bot", "top"]).sort_values("bar").reset_index(drop=True)
    t1 = b5["t1"].values.astype(np.int64)
    f["valid_t"] = t1[f.bar.values]
    tm = j.index.values.astype(np.int64)
    start = np.searchsorted(tm, f.valid_t.values)
    limit = t1[np.minimum(f.bar.values + FVG5_AGE, len(t1) - 1)]
    # reuse the fill scanner with the edge that means "traded into": top for bullish, bot for bearish
    edge_bot = np.where(f.dir == 1, f.top, f.bot)
    edge_top = np.where(f.dir == 1, f.top, f.bot)
    f["touch_t"] = _fill_times(tm, j["low"].values, j["high"].values, start, f.dir.values.astype(np.int64),
                               edge_bot.astype(float), edge_top.astype(float), limit.astype(np.int64))
    return f


# ------------------------------------------------------------------ per-day setup detection
def tod(ts):
    return ts.hour * 60 + ts.minute


def find_setups(day, b5, b5n, b3, h1, gap, f5, funnel):
    """Return candidate trades (one per side at most) for one trading day, before concurrency rules."""
    out = []
    d0 = pd.Timestamp(day)
    sess = b5[(b5.index >= d0 + pd.Timedelta(minutes=OPEN_T)) & (b5.index < d0 + pd.Timedelta(minutes=WINDOW_CAP_T))]
    if len(sess) < 5:
        return out
    funnel["days"] += 1
    nh, nl, eh, el = (sess[c].values for c in ("high", "low", "es_high", "es_low"))
    nc = sess["close"].values
    t1 = sess["t1"].values
    for side in (-1, 1):                    # -1 short (swing high / bearish SMT), +1 long
        tag = "short" if side == -1 else "long"
        ref_n, ref_e = (nh, eh) if side == -1 else (nl, el)
        # first 3-bar pivot on NQ 5m after 9:30: high[i] > high[i-1] and high[i] >= high[i+1]
        piv = None
        for i in range(1, len(sess) - 1):
            a, b_, c = ref_n[i - 1], ref_n[i], ref_n[i + 1]
            if (side == -1 and b_ > a and b_ >= c) or (side == 1 and b_ < a and b_ <= c):
                piv = i
                break
        if piv is None:
            continue
        funnel[f"{tag}_pivot"] += 1
        rn, re_ = ref_n[piv], ref_e[piv]
        smt = None
        for k in range(piv + 1, len(sess)):
            if side == -1:
                bn, be = nh[k] > rn, eh[k] > re_
            else:
                bn, be = nl[k] < rn, el[k] < re_
            if bn or be:
                if bn != be:
                    smt = k
                break
        if smt is None:
            continue
        funnel[f"{tag}_smt"] += 1
        t_smt = pd.Timestamp(t1[smt])
        if tod(t_smt) >= WINDOW_CAP_T:
            continue
        price = nc[smt]
        # draw on liquidity: nearest NDOG edge below (short) / above (long) price
        if day not in gap.index:
            continue
        g = gap.loc[day]
        edges = [g.gap_lo, g.gap_hi]
        cand = [e for e in edges if (e < price if side == -1 else e > price)]
        if not cand:
            continue
        dol = max(cand) if side == -1 else min(cand)
        funnel[f"{tag}_dol"] += 1
        # 1H displacement toward the DOL: last two closed 1H candles in trade direction, big bodies
        k1 = np.searchsorted(h1["t1"].values, t_smt.to_datetime64(), side="right") - 1
        if k1 < DISP_LOOKBACK + 1:
            continue
        o, c = h1["open"].values, h1["close"].values
        body = np.abs(c - o)
        two_dir = all((c[x] < o[x]) if side == -1 else (c[x] > o[x]) for x in (k1 - 1, k1))
        avg = body[k1 - 1 - DISP_LOOKBACK:k1 - 1].mean()
        if not (two_dir and avg > 0 and body[k1 - 1] + body[k1] >= DISP_MULT * avg):
            continue
        funnel[f"{tag}_bias"] += 1
        w_end = min(d0 + pd.Timedelta(minutes=WINDOW_CAP_T), t_smt + pd.Timedelta(minutes=WINDOW_MIN))
        # 3m entry: first close through the far side of a 3m FVG (bullish for shorts) formed after the SMT
        w3 = b3[(b3.t1 > t_smt - pd.Timedelta(minutes=9)) & (b3.t1 <= w_end)]
        ho, lo3, cl3, t13 = w3["high"].values, w3["low"].values, w3["close"].values, w3["t1"].values
        active = []
        entry_k, inv = None, None
        for m in range(len(w3)):
            if t13[m] > t_smt.to_datetime64():
                for (fb, ft) in active:                     # inversion of an earlier FVG
                    if (side == -1 and cl3[m] < fb) or (side == 1 and cl3[m] > ft):
                        entry_k, inv = m, (fb, ft)
                        break
                if entry_k is not None:
                    break
            if m >= 2 and t13[m] > t_smt.to_datetime64():   # FVG completed by this candle, after the SMT
                if side == -1 and lo3[m] > ho[m - 2]:
                    active.append((ho[m - 2], lo3[m]))
                if side == 1 and ho[m] < lo3[m - 2]:
                    active.append((ho[m], lo3[m - 2]))
        if entry_k is None:
            continue
        funnel[f"{tag}_inversion"] += 1
        te = pd.Timestamp(t13[entry_k])
        raw_entry = cl3[entry_k]
        entry = raw_entry + side * SLIP              # short: sells 1 tick lower
        stop = ho[entry_k] + TICK if side == -1 else lo3[entry_k] - TICK
        risk = (entry - stop) * side
        reward = (dol - entry) * side
        if risk <= 0 or reward <= 0:
            continue
        # internal liquidity: nearest unfilled opposing 5m FVG between price and the DOL, known by te
        te_ns = te.value
        sel = (f5.dir.values == -side) & (f5.valid_t.values <= te_ns) & (f5.touch_t.values >= te_ns)
        lo_bar = np.searchsorted(b5n["t1"].values.astype(np.int64), te_ns) - FVG5_AGE
        sel &= f5.bar.values >= lo_bar
        if side == -1:
            sel &= (f5.top.values < raw_entry) & (f5.bot.values > dol)
        else:
            sel &= (f5.bot.values > raw_entry) & (f5.top.values < dol)
        if not sel.any():
            continue
        cands = f5[sel]
        ifvg = cands.loc[cands.top.idxmax()] if side == -1 else cands.loc[cands.bot.idxmin()]
        funnel[f"{tag}_internal_fvg"] += 1
        out.append(dict(side=side, t_smt=t_smt, entry_time=te, raw_entry=raw_entry, entry=entry, stop=stop,
                        dol=dol, risk_pts=risk, rr_planned=reward / risk, be_level=ifvg.top if side == -1 else ifvg.bot,
                        ifvg_bot=ifvg.bot, ifvg_top=ifvg.top, smt_leader="NQ" if (
                            (nh[smt] > rn) if side == -1 else (nl[smt] < rn)) else "ES",
                        inv_fvg_bot=inv[0], inv_fvg_top=inv[1]))
    return out


# ------------------------------------------------------------------ exit simulation
def simulate(s, j_t, jo, jh, jl, jc, use_be, flat_ns):
    """Walk 1m bars after the entry close. Same-bar stop+target -> stop. Break-even moves the stop to entry
    from the NEXT 1m bar after price first trades into the internal FVG."""
    side, entry, stop, dol, be_lvl = s["side"], s["entry"], s["stop"], s["dol"], s["be_level"]
    i = np.searchsorted(j_t, s["entry_time"].value)
    cur_stop, be_on, last = stop, False, i - 1
    while i < len(j_t) and j_t[i] < flat_ns:
        last = i
        if side == -1:
            if jh[i] >= cur_stop:
                return max(cur_stop, jo[i]) + SLIP, i, ("breakeven" if be_on else "stop"), be_on, max(cur_stop, jo[i])
            if jl[i] <= dol:
                return dol, i, "target", be_on, dol
            if use_be and not be_on and jl[i] <= be_lvl:
                be_on, cur_stop = True, entry
        else:
            if jl[i] <= cur_stop:
                return min(cur_stop, jo[i]) - SLIP, i, ("breakeven" if be_on else "stop"), be_on, min(cur_stop, jo[i])
            if jh[i] >= dol:
                return dol, i, "target", be_on, dol
            if use_be and not be_on and jh[i] >= be_lvl:
                be_on, cur_stop = True, entry
        i += 1
    if last < np.searchsorted(j_t, s["entry_time"].value):
        last = np.searchsorted(j_t, s["entry_time"].value) - 1
    return jc[last] - side * SLIP, last, "session_end", be_on, jc[last]


def run_variant(setups_by_day, j, name, use_be, min_rr):
    j_t = j.index.values.astype(np.int64)
    jo, jh, jl, jc = (j[c].values for c in ("open", "high", "low", "close"))
    rows = []
    for day, cands in setups_by_day.items():
        cands = sorted([c for c in cands if (not min_rr or c["rr_planned"] >= MIN_RR)], key=lambda c: c["entry_time"])
        free_at, n = None, 0
        flat_ns = (pd.Timestamp(day) + pd.Timedelta(minutes=FLAT_T)).value
        for s in cands:
            if n >= MAX_TRADES or (free_at is not None and s["entry_time"].value < free_at):
                continue
            if s["entry_time"].value >= flat_ns:
                continue
            px, xi, why, be_on, raw_exit = simulate(s, j_t, jo, jh, jl, jc, use_be, flat_ns)
            side = s["side"]
            pts = (px - s["entry"]) * side
            pnl = pts * POINT_VALUE - COMMISSION_RT
            risk_usd = s["risk_pts"] * POINT_VALUE
            r = dict(s)
            r.update(variant=name, day=pd.Timestamp(day).date(), direction="short" if side == -1 else "long",
                     exit_time=pd.Timestamp(j_t[xi]) + pd.Timedelta(minutes=1), exit=px, exit_reason=why,
                     be_triggered=be_on, pnl_pts=pts, pnl_usd=pnl, r=pnl / risk_usd,
                     gross_r=(raw_exit - s["raw_entry"]) * side / s["risk_pts"])
            r["outcome"] = ("win" if why == "target" else "breakeven" if why == "breakeven" else
                            "loss" if why == "stop" else ("win" if pnl > 0 else "loss"))
            rows.append(r)
            n += 1
            free_at = j_t[xi] + NS_MIN
    return rows


# ------------------------------------------------------------------ stats / charts
BUCKETS = [0, 5, 10, 20, 40, 1e9]
BLABELS = ["<=5pt", "5-10", "10-20", "20-40", ">40"]


def stats(t):
    if t.empty:
        return pd.Series(dict(trades=0))
    w, l, b = (t.outcome == "win").sum(), (t.outcome == "loss").sum(), (t.outcome == "breakeven").sum()
    gw, gl = t.r[t.r > 0].sum(), -t.r[t.r < 0].sum()
    return pd.Series(dict(
        trades=len(t), wins=w, losses=l, breakevens=b, flattened_1600=(t.exit_reason == "session_end").sum(),
        win_rate_be_as_nonwin=w / len(t), win_rate_excl_be=w / (w + l) if w + l else np.nan,
        gross_r=t.gross_r.mean(), net_r=t.r.mean(), avg_realized_r=t.r.mean(), total_net_r=t.r.sum(),
        expectancy_usd=t.pnl_usd.mean(), net_usd=t.pnl_usd.sum(),
        profit_factor_r=gw / gl if gl > 0 else np.inf,
        profit_factor_usd=t.pnl_usd[t.pnl_usd > 0].sum() / -t.pnl_usd[t.pnl_usd < 0].sum() if (t.pnl_usd < 0).any() else np.inf,
        max_dd_r=max_dd(t.r.values), max_dd_usd=max_dd(t.pnl_usd.values),
        avg_planned_rr=t.rr_planned.mean(), median_risk_pts=t.risk_pts.median(), low_n=len(t) < 300))


def charts(tr):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    cols = {"main": "#2a78d6", "no_breakeven": "#eb6834", "no_min_2R": "#1baf7a"}
    fig, (a1, a3, a2) = plt.subplots(1, 3, figsize=(20, 5.5), dpi=130)
    for v, col in cols.items():
        t = tr[tr.variant == v].sort_values("entry_time")
        if t.empty:
            continue
        a1.plot(t.entry_time, t.r.cumsum(), color=col, lw=1.6, label=f"{v}: n={len(t)}, {t.r.sum():+.0f}R")
        a3.plot(t.entry_time, t.pnl_usd.cumsum(), color=col, lw=1.6, label=f"{v}: {t.pnl_usd.sum():+,.0f} $")
        y = t.groupby(t.entry_time.dt.year).r.mean()
        a2.plot(y.index, y.values, color=col, lw=1.5, marker="o", ms=3, label=v)
    for ax in (a1, a2, a3):
        ax.axhline(0, color="#52514e", lw=0.8)
        ax.grid(axis="y", color="#e6e6e3", lw=0.6)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        ax.legend(frameon=False, fontsize=8)
    a1.axvline(DEV_END, color="#52514e", lw=0.8, ls="--")
    a3.axvline(DEV_END, color="#52514e", lw=0.8, ls="--")
    a3.set_title("Cumulative net $ (1 MNQ)", loc="left", fontsize=10)
    a2.axvline(2020.5, color="#52514e", lw=0.8, ls="--")
    a1.set_title("Cumulative net R (1 MNQ, after costs); dashed = hold-out start", loc="left", fontsize=10)
    a2.set_title("Net R per trade by year", loc="left", fontsize=10)
    fig.tight_layout()
    fig.savefig(f"{OUT}/equity_and_yearly.png")
    plt.close(fig)


def main():
    dev_only = "--dev-only" in sys.argv
    sfx = "_dev" if dev_only else ""
    os.makedirs(OUT, exist_ok=True)
    nq, j, n_es = load_joined(dev_only)
    print(f"1m bars: NQ {len(nq)}, ES {n_es}, common {len(j)} ({len(j) / len(nq):.1%} of NQ)", flush=True)
    b5 = bars(j, "5min", cols=("open", "high", "low", "close"))          # SMT bars, common minutes
    b5es = bars(j, "5min", cols=("high", "low"), prefix="es_")
    b5["es_high"], b5["es_low"] = b5es["high"], b5es["low"]
    b5n = bars(nq, "5min")            # NQ-only 5m for internal FVGs
    b3 = bars(nq, "3min")
    b15 = bars(nq, "15min")           # built as specified; no rule uses it
    h1 = bars(nq, "1h")
    gap = ndog(nq)
    f5 = fvgs5(b5n, nq)
    print(f"bars 3m={len(b3)} 5m={len(b5)} 15m={len(b15)} 1H={len(h1)}  NDOG days={len(gap)}  5m FVGs={len(f5)}", flush=True)
    from collections import Counter
    funnel = Counter()
    setups = {}
    days = sorted(set(j.index[(j.index.hour * 60 + j.index.minute >= OPEN_T) &
                              (j.index.hour * 60 + j.index.minute < FLAT_T)].normalize()))
    funnel["ndog_missing_days"] = sum(1 for d in days if d not in gap.index)
    for d in days:
        s = find_setups(d, b5, b5n, b3, h1, gap, f5, funnel)
        if s:
            setups[d] = s
    funnel_df = pd.Series(funnel).rename("count")
    funnel_df.to_csv(f"{OUT}/funnel{sfx}.csv")
    print(funnel_df.to_string(), flush=True)
    rows = []
    for v, (be, rr) in VARIANTS.items():
        rows += run_variant(setups, nq, v, be, rr)
    tr = pd.DataFrame(rows)
    tr["year"] = pd.to_datetime(tr.day).dt.year
    tr["period"] = np.where(pd.to_datetime(tr.day) < DEV_END, "dev 2010-2020", "holdout 2021-2026")
    tr["stop_bucket"] = pd.cut(tr.risk_pts, BUCKETS, labels=BLABELS)
    tr.to_csv(f"{OUT}/trades{sfx}.csv", index=False)
    for by, fn in (([], "summary"), (["period"], "summary_by_period"), (["year"], "yearly"),
                   (["stop_bucket"], "by_stop_size"), (["direction"], "by_direction")):
        o = tr.groupby(["variant"] + by, observed=True, sort=False).apply(stats, include_groups=False).reset_index()
        o.to_csv(f"{OUT}/{fn}{sfx}.csv", index=False)
    # break-even diagnostics: main vs no_breakeven on the same entries
    m = tr[tr.variant == "main"].set_index("entry_time")
    nb = tr[tr.variant == "no_breakeven"].set_index("entry_time")
    be = m[m.outcome == "breakeven"]
    later = nb.reindex(be.index)
    diag = dict(main_trades=len(m), be_exits=len(be),
                be_exits_that_would_have_hit_DOL_without_BE=int((later.exit_reason == "target").sum()),
                be_exits_that_would_have_hit_original_stop=int((later.exit_reason == "stop").sum()),
                be_exits_flattened_at_1600_without_BE=int((later.exit_reason == "session_end").sum()),
                be_exits_not_matched=int(later.exit_reason.isna().sum()),
                net_r_of_be_exits_main=float(be.r.sum()), net_r_same_trades_without_be=float(later.r.sum()))
    pd.Series(diag).to_csv(f"{OUT}/breakeven_diagnostics{sfx}.csv")
    print(pd.Series(diag).to_string())
    if not dev_only:
        charts(tr)
    print(tr.groupby("variant", sort=False).apply(stats, include_groups=False).T.to_string())


if __name__ == "__main__":
    main()
