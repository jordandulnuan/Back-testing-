"""Build comparison tables (markdown + csv) for the add-on study from results/addons/trades.csv."""
import numpy as np
import pandas as pd

D = "results/addons"
t = pd.read_csv(f"{D}/trades.csv")
LBL = lambda v: v.replace(" | ", ": ")
BASE = "Baseline | FVG-edge stop"
order = list(dict.fromkeys(t.variant))
combos = [("NY", 2), ("NY", 3), ("Asia", 2), ("Asia", 3)]
periods = ["dev 2010-2020", "holdout 2021-2026"]


def tstat(x):
    return x.mean() / (x.std(ddof=1) / np.sqrt(len(x))) if len(x) > 2 else np.nan


rows = []
for v in order:
    for s, rr in combos:
        for p in periods + ["all"]:
            sel = (t.variant == v) & (t.session == s) & (t.rr == rr)
            bsel = (t.variant == BASE) & (t.session == s) & (t.rr == rr)
            if p != "all":
                sel &= t.period == p
                bsel &= t.period == p
            x, bx = t.loc[sel, "r"], t.loc[bsel, "r"]
            diff = x.mean() - bx.mean()
            se = np.sqrt(x.var(ddof=1) / len(x) + bx.var(ddof=1) / len(bx)) if len(x) > 2 else np.nan
            rows.append(dict(variant=v, session=s, rr=rr, period=p, trades=len(x), net_r=x.mean(),
                             t_vs_zero=tstat(x), baseline_net_r=bx.mean(), diff_vs_baseline=diff,
                             t_diff=diff / se if se else np.nan, low_n=len(x) < 300))
cmp_ = pd.DataFrame(rows)
cmp_.to_csv(f"{D}/vs_baseline.csv", index=False)

S = pd.read_csv(f"{D}/summary_by_period.csv")
A = pd.read_csv(f"{D}/summary_all.csv")
Y = pd.read_csv(f"{D}/yearly.csv")
B = pd.read_csv(f"{D}/by_stop_size.csv")
md = []


def fmt_row(r, label):
    flag = " ⚠️" if r.trades < 300 else ""
    return (f"| {label} | {int(r.trades)}{flag} | {r.win_rate:.1%} | {r.gross_r:+.3f} | {r.net_r:+.3f} | "
            f"{r.expectancy_usd:+.2f} | {r.profit_factor:.2f} | {r.max_dd_r:.0f} | {r.max_dd_usd:,.0f} |")


hdr = ("| Variant | Trades | Win | Gross R | Net R | Exp $/trade | PF (R) | Max DD R | Max DD $ |\n"
       "|---|---|---|---|---|---|---|---|---|")
for s, rr in combos:
    md.append(f"\n### {s} 1:{rr} (breakeven win rate {100 / (1 + rr):.1f}%)\n")
    for p in periods + ["all"]:
        src = A if p == "all" else S[S.period == p]
        md.append(f"\n**{'Full 2010-2026' if p == 'all' else p}**\n\n{hdr}")
        for v in order:
            r = src[(src.variant == v) & (src.session == s) & (src.rr == rr)]
            if len(r):
                md.append(fmt_row(r.iloc[0], LBL(v)))

md.append("\n## Each variant vs the baseline (net R per trade; t = Welch t-stat of the difference)\n")
md.append("| Variant | Combo | Dev diff (t) | Hold-out diff (t) | Hold-out net R (t vs 0) |\n|---|---|---|---|---|")
for v in order[1:]:
    for s, rr in combos:
        c = cmp_[(cmp_.variant == v) & (cmp_.session == s) & (cmp_.rr == rr)].set_index("period")
        d, h = c.loc[periods[0]], c.loc[periods[1]]
        flag = " ⚠️" if h.low_n or d.low_n else ""
        md.append(f"| {LBL(v)}{flag} | {s} 1:{rr} | {d.diff_vs_baseline:+.3f} ({d.t_diff:+.1f}) | "
                  f"{h.diff_vs_baseline:+.3f} ({h.t_diff:+.1f}) | {h.net_r:+.3f} ({h.t_vs_zero:+.1f}) |")

md.append("\n## Net R per trade by stop size (full period)\n")
for s, rr in combos:
    sub = B[(B.session == s) & (B.rr == rr)].pivot_table(index="variant", columns="stop_bucket", values="net_r", sort=False)
    n = B[(B.session == s) & (B.rr == rr)].pivot_table(index="variant", columns="stop_bucket", values="trades", sort=False)
    cols = [c for c in ["<=2.5pt", "2.5-5", "5-10", "10-20", ">20"] if c in sub.columns]
    md.append(f"\n**{s} 1:{rr}** (net R, trade count in brackets)\n\n| Variant | " + " | ".join(cols) + " |\n|" + "---|" * (len(cols) + 1))
    for v in order:
        if v in sub.index:
            cells = [f"{sub.loc[v, c]:+.2f} ({int(n.loc[v, c])})" if pd.notna(sub.loc[v, c]) else "-" for c in cols]
            md.append(f"| {LBL(v)} | " + " | ".join(cells) + " |")

md.append("\n## Year by year: net R per trade (trades)\n")
for s, rr in combos:
    sub = Y[(Y.session == s) & (Y.rr == rr)]
    nr = sub.pivot_table(index="year", columns="variant", values="net_r", sort=False)
    nn = sub.pivot_table(index="year", columns="variant", values="trades", sort=False)
    short = {v: v.replace(" | ", "<br>") for v in order}
    md.append(f"\n**{s} 1:{rr}**\n\n| Year | " + " | ".join(short[v] for v in order) + " |\n|" + "---|" * (len(order) + 1))
    for y in nr.index:
        cells = [f"{nr.loc[y, v]:+.2f} ({int(nn.loc[y, v])})" if v in nr.columns and pd.notna(nr.loc[y, v]) else "-" for v in order]
        md.append(f"| {y} | " + " | ".join(cells) + " |")

open(f"{D}/tables.md", "w").write("\n".join(md))
print("\n".join(md[: md.index("\n## Net R per trade by stop size (full period)\n")]))


# ---- rate chart: net R per trade by year (compares variants on a per-trade basis)
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

lines = [(BASE, "#7d7c76", "Baseline (FVG-edge stop)"),
         ("V1 OTE | FVG-edge stop", "#2a78d6", "V1 OTE"),
         ("V2A IFVG retest | IFVG-edge stop", "#eb6834", "V2A IFVG retest"),
         ("V2B post-inversion FVG | FVG-edge stop", "#1baf7a", "V2B post-inversion FVG"),
         ("V3 RB | tap-and-close", "#eda100", "V3 RB tap-and-close"),
         ("V3 RB | limit at edge", "#e87ba4", "V3 RB limit")]
fig, axes = plt.subplots(2, 2, figsize=(14, 8.5), dpi=130, sharex=True)
for ax, (s, rr) in zip(axes.flat, combos):
    for v, col, lab in lines:
        y = Y[(Y.variant == v) & (Y.session == s) & (Y.rr == rr)].sort_values("year")
        y = y[y.trades >= 20]
        ax.plot(y.year, y.net_r, color=col, lw=2 if v == BASE else 1.4, marker="o", ms=3, label=lab)
    ax.axhline(0, color="#52514e", lw=0.8)
    ax.axvline(2020.5, color="#52514e", lw=0.8, ls="--")
    ax.set_title(f"{s} 1:{rr}: net R per trade by year (years with ≥20 trades)", loc="left", fontsize=10)
    ax.set_ylim(-1.6, 0.6)
    ax.grid(axis="y", color="#e6e6e3", lw=0.6)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
axes[0, 0].legend(fontsize=8, frameon=False, loc="lower right")
fig.suptitle("Add-on variants vs baseline, per trade after costs. Dashed line = start of hold-out (2021).", x=0.01, ha="left")
fig.tight_layout()
fig.savefig(f"{D}/net_r_per_trade_by_year.png")
