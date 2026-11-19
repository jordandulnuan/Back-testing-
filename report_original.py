"""Markdown tables for the original 16-run study (results/summary.csv, results/yearly.csv)."""
import pandas as pd

s = pd.read_csv("results/summary.csv")
s["variant"] = s.fvg_mode.map({"match": "bias-matched FVG", "any": "any FVG"}) + " / " + \
    s.confluence.map({True: "confluence", False: "no confluence"})
s = s.sort_values(["session", "rr", "fvg_mode", "confluence"], ascending=[False, True, False, False])
out = []
for (se, rr), g in s.groupby(["session", "rr"], sort=False):
    out.append(f"\n### {se} session, 1:{rr} (breakeven win rate {100 / (1 + rr):.1f}%)\n")
    out.append("| Variant | Trades | Win rate | vs breakeven | Avg R gross | Avg R net | Exp $/trade | PF (R) | Max DD R | Max DD $ |")
    out.append("|---|---|---|---|---|---|---|---|---|---|")
    for _, r in g.iterrows():
        out.append(f"| {r.variant} | {int(r.trades)} | {r.win_rate:.1%} | {100 * (r.win_rate - r.breakeven_wr):+.1f} pts | "
                   f"{r.gross_avg_r:+.3f} | {r.avg_r:+.3f} | {r.expectancy_usd:+.2f} | {r.profit_factor:.2f} | "
                   f"{r.max_dd_r:.0f} | {r.max_dd_usd:,.0f} |")
y = pd.read_csv("results/yearly.csv")
y = y[(y.fvg_mode == "match") & (y.confluence)]
out.append("\n### Year by year, bias-matched FVG + liquidity confluence (n / win / avg net R)\n")
out.append("| Year | Asia 1:2 | Asia 1:3 | NY 1:2 | NY 1:3 |\n|---|---|---|---|---|")
for yr, g in y.groupby("year"):
    cells = []
    for se in ("Asia", "NY"):
        for rr in (2, 3):
            r = g[(g.session == se) & (g.rr == rr)].iloc[0]
            cells.append(f"{int(r.trades)} / {r.win_rate:.1%} / {r.avg_r:+.2f}")
    out.append(f"| {yr} | " + " | ".join(cells) + " |")
open("results/tables.md", "w").write("\n".join(out))
