"""Verify the 1m timestamps are New York wall-clock time with DST.

If the cash open (9:30 ET) volatility spike sits at 09:30 local in BOTH January (EST) and July (EDT)
of every year, the clock follows DST. A fixed-EST feed would show the July spike at 08:30, a
fixed-UTC feed at 14:30/13:30. Output: results/tz_check.csv
"""
import os
import pandas as pd

import sys
path = sys.argv[1] if len(sys.argv) > 1 else "data/nq_1m.parquet"
tag = sys.argv[2] if len(sys.argv) > 2 else "nq"
df = pd.read_parquet(path)
rng = df.high - df.low
tod = df.index.hour * 60 + df.index.minute
rows = []
for y in sorted(set(df.index.year)):
    for mon in (1, 7):
        sel = (df.index.year == y) & (df.index.month == mon)
        if sel.sum() < 1000:
            continue
        s = rng[sel].groupby(tod[sel]).median()
        win = s.loc[7 * 60: 11 * 60]           # search window 07:00-11:00 local
        peak = int(win.idxmax())
        rows.append(dict(year=y, month=mon, peak_minute=f"{peak // 60:02d}:{peak % 60:02d}",
                         median_range_0929=s.get(569), median_range_0930=s.get(570),
                         median_range_0830=s.get(510), spike_ratio_0930_vs_0929=s.get(570) / s.get(569)))
out = pd.DataFrame(rows)
os.makedirs("results", exist_ok=True)
out.to_csv("results/tz_check.csv" if tag == "nq" else f"results/tz_check_{tag}.csv", index=False)
print(out.to_string(index=False))
print("peak at 09:30 in", (out.peak_minute == "09:30").sum(), "of", len(out), "year-months")
