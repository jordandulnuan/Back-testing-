"""Load HistData NSXUSD 1-minute ASCII files into one clean 1m parquet.

HistData timestamps for this instrument are already New York wall-clock time with DST
(verified: the daily maintenance break sits at 17:15/16:15-18:00 local in both January
and July of every year), so bars are kept as naive America/New_York wall-clock times.
"""
import glob
import sys
import pandas as pd

src = sys.argv[1] if len(sys.argv) > 1 else "data/x"
out = sys.argv[2] if len(sys.argv) > 2 else "data/nq_1m.parquet"
sym = sys.argv[3] if len(sys.argv) > 3 else "NSXUSD"   # SPXUSD for the S&P 500 (ES proxy)

frames = []
for f in sorted(glob.glob(f"{src}/DAT_ASCII_{sym}_M1_*.csv")):
    df = pd.read_csv(f, sep=";", header=None, names=["ts", "open", "high", "low", "close", "vol"])
    frames.append(df)
df = pd.concat(frames, ignore_index=True)
df["ts"] = pd.to_datetime(df["ts"], format="%Y%m%d %H%M%S")
df = df.drop(columns="vol").drop_duplicates("ts", keep="first").sort_values("ts").set_index("ts")
# basic sanity: drop bars with impossible OHLC
bad = (df.high < df[["open", "close"]].max(axis=1)) | (df.low > df[["open", "close"]].min(axis=1)) | (df.low <= 0)
df = df[~bad]
df.to_parquet(out)
print(len(df), df.index.min(), df.index.max(), "dropped bad:", int(bad.sum()))
