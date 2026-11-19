# NQ FVG + liquidity-draw intraday backtest

**Bottom line: none of the 16 configurations makes money after costs.** Before costs the core setup (bias-matched FVG plus liquidity confluence) is roughly breakeven, at +0.00 to +0.02R per trade. After 1 tick of slippage and $1.24 round-turn commission on 1 MNQ it loses between −0.16R per trade (New York) and −0.38R per trade (Asia). Win rates in the bias-matched variants are close to or slightly above the 33.3% and 25% breakeven lines. They still lose because the stops are tight, so fixed costs are large in R terms, and because session-end exits mean many trades never get the full +2R/+3R or −1R.

## Data (read this first)
- **Source:** HistData.com free `NSXUSD` 1-minute bars, **2010-11-14 → 2026-11-13 (≈16.0 years)**, 5.33M bars. Databento, FirstRate and Kibot were not usable here: they were blocked by the sandbox network at first, then no valid Databento API key was available, and FirstRate and Kibot are paid downloads.
- This is a **Nasdaq-100 CFD/index quote, not back-adjusted NQ futures**. Prices track the cash index, so they sit below NQ by the futures basis, and there are no contract rolls. Point moves are almost the same as NQ, so dollars are scaled at $2/pt to MNQ as you asked. Prices are snapped to the 0.25 tick.
- **There is no volume**, so the "VWAP" is an equal-weighted running mean of 1m typical price. It is anchored at the 18:00 ET session open, the CME trading day.
- Timestamps are already New York wall-clock time with DST. I checked this: the daily maintenance halt falls at the same local time in both January and July.

## Implementation choices (where the spec was open)
| Item | Choice |
|---|---|
| Resampling | 5m and 1H on clock boundaries; 4H anchored at 18:00 (18/22/02/06/10/14); daily = 18:00–17:00 trading day |
| Draws on liquidity | high and low of the last *closed* 1H, 4H and daily bar, plus every unfilled 1H/4H/daily FVG |
| "At or overlaps" | the level falls inside the 5m FVG ± 0.05% of price, or an HTF FVG range intersects that band |
| Bias | last closed 1H close above (below) EMA9 and EMA21, EMA9 above (below) EMA21, and that close above (below) VWAP at that time |
| FVG filled | price trades fully through the far edge (checked on 1m). Bars that trade through on the tap bar are invalid. 5m FVGs expire after 1440 bars (~5 days) |
| Entry | close of a 5m bar that touches the unfilled FVG and closes in the bias direction (long: close > open), +1 tick slippage. If several FVGs qualify, the most recent one is used |
| Stop | far edge of the FVG (long: gap bottom; short: gap top), filled 1 tick worse (or at the open on a gap) |
| Target | entry ± 2R or 3R, resting limit (no slippage) |
| Exit simulation | 1m bars from the entry close onward. If stop and target fall in the same 1m bar, it counts as the stop. Flatten at the last 1m close before session end (−1 tick) |
| Sessions | NY 09:30–16:00, Asia 19:00–02:00 ET. Max 3 trades per session, one at a time |
| Costs | $1.24 per MNQ round turn plus slippage as above. R = net $ / planned risk $ |
| "Any FVG" variant | also allows FVGs pointing against the bias. The stop is still the gap edge on the protective side, and trades where that edge isn't beyond entry are skipped |

## Results (all 16 runs)
Win = net P&L > 0. Profit factor is computed on R. Drawdowns are peak-to-trough on cumulative R and on 1-MNQ dollars.

### NY session, 1:2 (breakeven win rate 33.3%)

| Variant | Trades | Win rate | vs breakeven | Avg R gross | Avg R net (= expectancy) | Expectancy $/trade (1 MNQ) | Profit factor (R) | Max DD (R) | Max DD $ (1 MNQ) |
|---|---|---|---|---|---|---|---|---|---|
| bias-matched FVG / confluence | 5843 | 34.8% | +1.4 pts | +0.012 | -0.156 | -0.52 | 0.78 | 949 | 8,365 |
| bias-matched FVG / no confluence | 7136 | 35.4% | +2.0 pts | +0.034 | -0.130 | -0.09 | 0.82 | 983 | 9,153 |
| any FVG / confluence | 8463 | 30.9% | -2.5 pts | -0.027 | -0.563 | -1.27 | 0.47 | 4766 | 15,237 |
| any FVG / no confluence | 9331 | 31.2% | -2.1 pts | -0.022 | -0.541 | -1.30 | 0.48 | 5046 | 17,596 |

### NY session, 1:3 (breakeven win rate 25.0%)

| Variant | Trades | Win rate | vs breakeven | Avg R gross | Avg R net (= expectancy) | Expectancy $/trade (1 MNQ) | Profit factor (R) | Max DD (R) | Max DD $ (1 MNQ) |
|---|---|---|---|---|---|---|---|---|---|
| bias-matched FVG / confluence | 5472 | 29.9% | +4.9 pts | +0.010 | -0.159 | -0.67 | 0.79 | 913 | 9,155 |
| bias-matched FVG / no confluence | 6638 | 30.0% | +5.0 pts | +0.029 | -0.137 | -0.32 | 0.82 | 984 | 9,905 |
| any FVG / confluence | 8102 | 26.3% | +1.3 pts | -0.013 | -0.564 | -1.20 | 0.50 | 4580 | 13,998 |
| any FVG / no confluence | 8930 | 26.6% | +1.6 pts | -0.003 | -0.535 | -1.52 | 0.52 | 4805 | 19,143 |

### Asia session, 1:2 (breakeven win rate 33.3%)

| Variant | Trades | Win rate | vs breakeven | Avg R gross | Avg R net (= expectancy) | Expectancy $/trade (1 MNQ) | Profit factor (R) | Max DD (R) | Max DD $ (1 MNQ) |
|---|---|---|---|---|---|---|---|---|---|
| bias-matched FVG / confluence | 5605 | 32.0% | -1.3 pts | -0.004 | -0.383 | -1.77 | 0.54 | 2211 | 13,543 |
| bias-matched FVG / no confluence | 6025 | 32.3% | -1.0 pts | +0.002 | -0.363 | -1.68 | 0.56 | 2273 | 14,946 |
| any FVG / confluence | 7702 | 26.8% | -6.5 pts | -0.042 | -1.024 | -1.97 | 0.27 | 7893 | 17,979 |
| any FVG / no confluence | 7971 | 27.2% | -6.2 pts | -0.038 | -0.996 | -1.77 | 0.28 | 7947 | 17,884 |

### Asia session, 1:3 (breakeven win rate 25.0%)

| Variant | Trades | Win rate | vs breakeven | Avg R gross | Avg R net (= expectancy) | Expectancy $/trade (1 MNQ) | Profit factor (R) | Max DD (R) | Max DD $ (1 MNQ) |
|---|---|---|---|---|---|---|---|---|---|
| bias-matched FVG / confluence | 5267 | 28.2% | +3.2 pts | +0.004 | -0.382 | -1.85 | 0.57 | 2100 | 13,434 |
| bias-matched FVG / no confluence | 5657 | 28.5% | +3.5 pts | +0.017 | -0.352 | -1.46 | 0.60 | 2132 | 13,987 |
| any FVG / confluence | 7418 | 23.6% | -1.4 pts | -0.028 | -1.031 | -1.81 | 0.29 | 7657 | 16,392 |
| any FVG / no confluence | 7676 | 23.8% | -1.2 pts | -0.019 | -0.998 | -1.45 | 0.31 | 7668 | 16,282 |

### Year by year, core variant (bias-matched FVG + liquidity confluence)

| Year | Asia 1:2 n / win / avgR | Asia 1:3 n / win / avgR | NY 1:2 n / win / avgR | NY 1:3 n / win / avgR |
|---|---|---|---|---|
| 2010 | 37 / 21.6% / -0.92 | 35 / 20.0% / -0.89 | 55 / 23.6% / -0.60 | 50 / 20.0% / -0.57 |
| 2011 | 324 / 32.1% / -0.57 | 306 / 28.8% / -0.56 | 403 / 36.0% / -0.21 | 376 / 30.6% / -0.22 |
| 2012 | 385 / 23.1% / -0.84 | 363 / 20.1% / -0.86 | 389 / 32.6% / -0.29 | 366 / 27.6% / -0.31 |
| 2013 | 349 / 24.4% / -0.81 | 330 / 21.2% / -0.84 | 426 / 31.0% / -0.38 | 401 / 25.4% / -0.40 |
| 2014 | 383 / 25.6% / -0.81 | 355 / 22.8% / -0.84 | 379 / 31.9% / -0.29 | 347 / 29.1% / -0.23 |
| 2015 | 362 / 25.7% / -0.64 | 348 / 21.3% / -0.70 | 344 / 37.2% / -0.10 | 327 / 31.2% / -0.09 |
| 2016 | 390 / 32.8% / -0.39 | 364 / 29.4% / -0.36 | 393 / 30.5% / -0.31 | 378 / 25.7% / -0.35 |
| 2017 | 378 / 28.0% / -0.53 | 357 / 24.4% / -0.53 | 387 / 33.6% / -0.24 | 358 / 27.9% / -0.28 |
| 2018 | 376 / 37.0% / -0.25 | 357 / 33.1% / -0.23 | 356 / 35.7% / -0.09 | 332 / 30.4% / -0.06 |
| 2019 | 341 / 32.8% / -0.31 | 314 / 31.2% / -0.28 | 369 / 32.8% / -0.25 | 338 / 27.8% / -0.29 |
| 2020 | 355 / 35.5% / -0.16 | 336 / 30.7% / -0.16 | 320 / 35.6% / -0.05 | 305 / 29.2% / -0.09 |
| 2021 | 326 / 33.7% / -0.15 | 305 / 28.9% / -0.13 | 380 / 36.6% / -0.02 | 351 / 31.1% / -0.06 |
| 2022 | 304 / 33.2% / -0.25 | 289 / 30.1% / -0.24 | 360 / 38.3% / +0.05 | 342 / 35.1% / +0.09 |
| 2023 | 328 / 39.0% / -0.10 | 304 / 34.9% / -0.10 | 329 / 38.3% / -0.05 | 310 / 34.2% / -0.07 |
| 2024 | 354 / 32.8% / -0.19 | 334 / 27.2% / -0.22 | 344 / 36.6% / -0.03 | 329 / 32.5% / +0.01 |
| 2025 | 333 / 39.6% / +0.06 | 305 / 36.4% / +0.12 | 313 / 33.5% / -0.12 | 293 / 29.0% / -0.08 |
| 2026 | 280 / 42.1% / +0.12 | 265 / 36.6% / +0.17 | 296 / 40.5% / +0.10 | 269 / 35.7% / +0.12 |
Equity curves: `results/equity_{NY,Asia}_1to{2,3}.png`. Full trade list: `results/trades.csv`. Breakdown by stop size: `results/by_stop_size.csv`.

## What the numbers say
1. **Requiring the FVG to match the bias matters a lot.** "Any FVG" loses about 0.5–1.0R per trade, mostly on tiny-stop counter-direction taps.
2. **Liquidity confluence adds nothing.** It removes 8–18% of trades and leaves win rate and expectancy unchanged or slightly worse. With 6 HTF levels plus every unfilled HTF FVG, almost every 5m FVG is "near" a draw.
3. **The losses come from costs, not a negative signal.** Gross expectancy is about 0. NY trades with stops over 20 points are the only bucket that is net positive, at about +0.04–0.05R. Stops of 2.5 points or less lose 0.4–1.2R per trade net.
4. **Win rate vs breakeven is misleading on its own.** At 1:3, NY's 29.9% beats 25%, but 16–23% of trades are flattened at session end for partial R. Avg R is the honest number.
5. **The trend improves over time** as index prices rise and 1 tick becomes a smaller share of the stop. In 2025–2026 the core variant is about +0.1R per trade in both sessions, but that is only ~2 years of results and not a stable edge.

## Your Notion journal (12 trades, Sep 22 – Oct 2 2026)
2 wins, 1 break-even, 9 losses, a **16.7% win rate**. Planned R:R averaged about 2.8, so the net is roughly −2.3R. Both wins were continuation trades. All 5 reversal trades lost or broke even, and the reversals aren't part of this rule set anyway.
On the same dates the backtest's core variant took 11 NY trades (36% win at 1:2, −1.1R) and 11 Asia trades (55% win at 1:2, +6.1R).
12 trades is too few to tell skill from luck. If your true win rate were the backtest's ~35%, you would see 2 or fewer wins in 12 trades about 15% of the time. The journal doesn't log entry times, stops or which FVG/draw was used, so trades can't be matched one-for-one.

## Reproduce
```
pip install pandas pyarrow numba matplotlib
# download HistData NSXUSD ASCII M1 zips into data/raw, unzip to data/x
python3 prep_data.py data/x data/nq_1m.parquet
python3 backtest.py          # ~45 s
```
