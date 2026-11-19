# NQ intraday FVG backtests

Two studies on the same 1-minute Nasdaq-100 data, sessions, costs and exit engine:

1. **Original study** (`backtest.py`): 1H bias, then a 5m FVG retrace with a liquidity-draw confluence, 16 configurations.
2. **Add-on confluence study** (`addons.py`): the baseline compared with OTE, IFVG (A/B) and rejection-block add-ons, 40 configurations, using a development window and a hold-out window.

**Bottom line for both: nothing makes money after costs over 2010–2026, and no add-on beats the baseline after costs in a way the statistics support.**

---

## Data, and what's missing

| | Wanted | Used |
|---|---|---|
| Instrument | NQ futures, continuous, back-adjusted | HistData `NSXUSD`, a Nasdaq-100 **index/CFD quote** (no contract rolls, no futures basis) |
| History | 2006 → now | **2010-11-14 → 2026-11-13, ≈16.0 years** (5.33M 1m bars) |
| Volume | yes, for a real VWAP | **none.** "VWAP" is an equal-weighted running mean of 1m typical price, anchored at 18:00 ET |
| Tick size | 0.25 | source quotes to 0.001; prices snapped to 0.25 |

- **Databento:** the credential saved in this environment is invalid. Databento reports that its username is your login name, not an API key starting with `db-`. To switch to real NQ futures with volume, replace the `hist.databento.com` network secret in the environment settings with your `db-` key as the username and an empty password. Then pull `GLBX.MDP3`, `ohlcv-1m`, `NQ.v.0` (Databento's history starts June 2010) and rerun.
- **What the substitute misses:** real futures prices (including the roll), volume, and so a real VWAP. Overnight liquidity and spreads differ from NQ, and the 2006–2010 history is absent.
- **Timezone check** (`check_timezone.py` → `results/tz_check.csv`): I took the median 1m range for every January (EST) and July (EDT) from 2011 to 2026. In all 32 months the biggest bar between 07:00 and 11:00 falls between 09:30 and 10:00 local time, and the range jumps 1.75–5× from 09:29 to 09:30. A feed fixed to EST would put July's spike at 08:30. **The timestamps are New York time with DST.**

---

## Add-on study (`addons.py`)

### Setup shared by every variant (the baseline)
- **1H bias:** the last closed 1H candle closes above both EMA9 and EMA21, EMA9 is above EMA21, and the close is above VWAP. Bearish is the reverse; mixed means no trade.
- **Entry:** a 5m candle taps an unfilled 5m FVG in the bias direction and closes in the trade direction. Entry is that candle's close plus 1 tick of slippage.
- **Targets:** 1:2 and 1:3, run separately.
- **Sessions:** Asia 19:00–02:00 ET and NY 09:30–16:00 ET, tested separately. At most 3 trades per session per day, one at a time, flat at session end.
- **Costs:** $1.24 per round turn plus 1 tick of slippage on entry and on stop or flatten exits. Targets are resting limits.
- **Same-minute rule:** if the stop and target are both hit in the same 1m bar, it counts as the stop.
- The baseline uses no liquidity-draw filter: in the original study that filter changed nothing.

### Variants (10 rule-sets × 2 sessions × 2 R:R = 40 configurations)
| # | Variant | Stop |
|---|---|---|
| B0 | Baseline | FVG far edge (original) |
| B1 | Baseline | entry-candle wick extreme ± 1 tick |
| 1a/1b | OTE: FVG midpoint inside the 62–79% retracement of the latest 5m impulse leg in the bias direction | FVG edge / candle |
| 2Aa/2Ab | IFVG-A: retest of an inverted FVG | IFVG far edge / candle |
| 2Ba/2Bb | IFVG-B: new bias-direction FVG formed within 1h after an inversion | FVG edge / candle |
| 3a | Rejection block, tap-and-close entry | wick tip ± 1 tick |
| 3b | Rejection block, limit at the block edge | wick tip ± 1 tick |

**How many things were tried:** 40 configurations in this study (32 add-on cells compared with the baseline), on top of the 16 in the original study, so **56 backtests on the same data**. With 32 comparisons, one or two would look "significant" (|t| > 2) by chance alone. No add-on gets close to that in its favour in the hold-out: the best is |t| = 0.9.

### Results: net R per trade after costs (full period, with dev and hold-out in brackets)
| Variant | NY 1:2 | NY 1:3 | Asia 1:2 | Asia 1:3 | Fewest trades in a cell |
|---|---|---|---|---|---|
| Baseline: FVG-edge stop | -0.13 (dev -0.20 / hold +0.02) | -0.14 (dev -0.22 / hold +0.02) | -0.36 (dev -0.52 / hold -0.07) | -0.35 (dev -0.52 / hold -0.04) | 1981 |
| Baseline: candle stop | -0.22 (dev -0.32 / hold -0.05) | -0.21 (dev -0.31 / hold -0.02) | -0.52 (dev -0.72 / hold -0.16) | -0.52 (dev -0.74 / hold -0.13) | 2574 |
| V1 OTE: FVG-edge stop | -0.11 (dev -0.13 / hold -0.08) | -0.08 (dev -0.08 / hold -0.08) | -0.40 (dev -0.54 / hold -0.08) | -0.37 (dev -0.52 / hold -0.02) | 205 ⚠️ |
| V1 OTE: candle stop | -0.19 (dev -0.24 / hold -0.10) | -0.14 (dev -0.20 / hold -0.02) | -0.64 (dev -0.82 / hold -0.27) | -0.63 (dev -0.82 / hold -0.24) | 239 ⚠️ |
| V2A IFVG retest: IFVG-edge stop | -0.12 (dev -0.19 / hold +0.02) | -0.11 (dev -0.21 / hold +0.09) | -0.36 (dev -0.51 / hold -0.03) | -0.36 (dev -0.52 / hold -0.02) | 423 |
| V2A IFVG retest: candle stop | -0.22 (dev -0.33 / hold -0.02) | -0.21 (dev -0.33 / hold +0.03) | -0.50 (dev -0.69 / hold -0.11) | -0.53 (dev -0.71 / hold -0.14) | 580 |
| V2B post-inversion FVG: FVG-edge stop | -0.12 (dev -0.19 / hold +0.02) | -0.13 (dev -0.20 / hold +0.01) | -0.35 (dev -0.51 / hold -0.05) | -0.35 (dev -0.51 / hold -0.03) | 1529 |
| V2B post-inversion FVG: candle stop | -0.22 (dev -0.31 / hold -0.06) | -0.21 (dev -0.31 / hold -0.03) | -0.51 (dev -0.69 / hold -0.15) | -0.49 (dev -0.69 / hold -0.13) | 2009 |
| V3 RB: tap-and-close | -0.20 (dev -0.29 / hold +0.04) | -0.20 (dev -0.32 / hold +0.10) | -0.44 (dev -0.57 / hold -0.10) | -0.41 (dev -0.54 / hold -0.08) | 92 ⚠️ |
| V3 RB: limit at edge | -0.63 (dev -0.74 / hold -0.38) | -0.63 (dev -0.77 / hold -0.32) | -0.94 (dev -1.23 / hold -0.23) | -0.99 (dev -1.26 / hold -0.33) | 336 |
⚠️ = at least one dev or hold-out cell has fewer than 300 trades. OTE has 205–296 hold-out trades. RB tap-and-close has only 92 NY hold-out trades. Its total trade count is 329 in NY (only just above 300) and 605–614 in Asia.

Win rate, gross R, expectancy ($), profit factor, max drawdown (R and $), the yearly breakdown and the stop-size split for every variant are in **[RESULTS_ADDONS.md](RESULTS_ADDONS.md)**. The per-trade log is `results/addons/trades.csv`, and the CSVs are `summary_*.csv`, `yearly.csv`, `by_stop_size*.csv` and `vs_baseline.csv` in `results/addons/`.

Charts:
- `results/addons/equity_grid.png`: cumulative net R for each variant against the baseline.
- `results/addons/net_r_per_trade_by_year.png`: net R per trade by year. Use this one to compare variants, because cumulative R penalises variants that simply take more trades. Early RB-limit Asia years fall below the −1.6 axis floor.

### What it says
1. **No variant beats the baseline after costs.** All 40 configurations lose money over 2010–2026. In the hold-out, the gap to the baseline is at most +0.07R per trade, at t ≤ 0.9.
   - **IFVG-A with the IFVG-edge stop** is the closest call: +0.09R per trade on NY 1:3 in the hold-out, t = 1.4 against zero.
   - **RB tap-and-close** is +0.10R per trade on NY 1:3 in the hold-out, but on only 92 trades with t = 0.5.
   - Both were level with or worse than the baseline in the development years. That is noise, not an edge.
2. **The candle-wick stop is worse than the FVG-edge stop in 31 of the 32 family/session/R:R/period cells.** In the development years the gap is −0.09 to −0.30R per trade (t from −1.0 to −7.0). The only exception is OTE NY 1:3 in the hold-out (+0.06R, t = 0.4). The tighter stop roughly doubles the cost per R and gets hit more often.
3. **OTE** cuts trades by about 90% and leaves expectancy unchanged within noise, and the hold-out has too few trades to judge.
4. **IFVG-B** is essentially the baseline: 69% of baseline FVGs qualify, and the differences are within ±0.02R.
5. **Rejection block with a limit entry is clearly worse**: −0.23 to −0.38R per trade in the hold-out, t from −3 to −4. The blocks are small, so stops are 1–2 points in the dev years, and a fill on touch tends to be followed by a run straight through the wick.
6. **Stop size drives everything.** Across variants, net R improves steadily from stops of 2.5 points or less (−0.5 to −1.3R) to stops over 20 points (about 0R). The hold-out looks better than the dev period mainly because the index is about 3× higher, so one tick plus commission is a much smaller share of the stop. Gross R before costs is between −0.10 and +0.17 per trade in every dev and hold-out cell (RB limit: −0.30 to −0.03). The setups have no edge before costs, so they can't have one after.

### Assumptions (all fixed before any result was seen; nothing was tuned)
**Data and clock**
- Bars are labelled by their open time and become usable only at their close.
- 5m and 1H bars use clock boundaries. 4H bars are anchored at 18:00 (18/22/02/06/10/14). The daily bar is the 18:00–17:00 trading day.
- A trading day belongs to the date after the 18:00 open. The Asia session is labelled by the evening it opens.

**Bias**
- EMA9 and EMA21 use 1H closes, are continuous across days, and are computed recursively (`adjust=False`).
- VWAP (volume-less, see above) is anchored at the 18:00 ET session open. The bias compares the last closed 1H close with the VWAP value at that close.
- The bias at a 5m decision uses only the 1H bar that had closed by the 5m close.

**FVGs**
- An FVG is a 3-candle gap: bullish when low₃ > high₁, bearish when high₃ < low₁. It is valid from the close of candle 3.
- It is filled, and dead, once any 1m bar trades to its far edge.
- 5m FVGs expire 1440 5m bars after forming (about 5 trading days). The fill check uses the same bar count. (A bug here was found and fixed; see the changelog.)

**Tap-and-close entries**
- The 5m bar's range must touch the zone and the zone must still be alive at that bar's close, so a bar that trades through the zone doesn't count.
- The candle must close in the trade direction (close > open for longs).
- If several zones qualify, the most recently formed one is used.

**Stops**
- FVG-edge stop: the far edge of the zone. Stop fills are 1 tick worse, or at the open if price gaps through.
- Candle stop: the entry candle's low − 1 tick for longs (high + 1 tick for shorts).

**Swings (used by OTE and RB)**
- A 5-bar fractal: a 5m high above the 2 bars before it and at least as high as the 2 bars after it. It is confirmed 2 bars later and used only after confirmation.

**OTE**
- For longs, the leg runs from the last swing low before the most recent confirmed swing high to that swing high.
- The leg must be intact: no low since the swing high may have broken the swing low.
- The zone is 62–79% of the leg measured down from the high. The FVG midpoint must lie inside it. Shorts mirror this.

**IFVG-A (shorts; longs mirror)**
- A bullish 5m FVG must form on a bounce whose first two candles tapped an older, still-unfilled bullish FVG (the "dropped, tapped an FVG, bounced" step).
- The first 5m close below its bottom, within 1440 bars, makes it a bearish IFVG over the same price range, valid from that close.
- Short on a tap-and-close into it. It dies once price trades to its top, or after 1440 bars.

**IFVG-B (shorts; longs mirror)**
- An inversion is any 5m close below a bullish FVG's bottom.
- A bearish FVG qualifies if its first candle opens at the inversion bar or up to 12 bars (1 hour) after it.
- Entry is a tap-and-close into that FVG.

**Rejection block**
- A confirmed swing-low candle whose lower wick (min(open, close) − low) is at least 50% of its range. For shorts, a swing-high candle with the upper wick.
- The block runs from the low to min(open, close).
- When it is confirmed, the block must sit fully inside a live bullish FVG. The FVG must match the bias direction.
- The block dies once price trades to its stop level (tip − 1 tick), or after 1440 bars. Each block is traded at most once.

**RB entry types**
- Tap-and-close: as above, with the stop at the tip − 1 tick.
- Limit: a resting order at the block edge (top for longs) whenever the bias, as of the last closed 5m bar, agrees.
  - It fills only when a 1m bar trades **1 tick through** the edge; a touch alone is not assumed to fill.
  - It fills at the edge price even if price gapped through it, which is conservative.
  - If the fill minute also reaches the stop, it counts as a stop. A target can't be hit in the fill minute.

**R and results**
- R = net $ ÷ planned risk $, where planned risk is entry (after slippage) minus stop, × $2.
- Gross R excludes slippage and commission.
- A win is any trade with net P&L above zero, so session-end exits with a small gain count as wins.
- Dev and hold-out periods are split by session date at 2021-01-01. The dev-only run (`--dev-only`) was used once to find bugs. The full run was then made once and the hold-out was reported as it came out.

**Statistics**
- t-statistics treat trades as independent. Variants share many trades and days, so these are rough guides, not exact p-values.

---

## Original study (`backtest.py`)
Liquidity draws (the previous 1H, 4H and daily high and low, plus unfilled 1H, 4H and daily FVGs), with the variants bias-matched FVG vs any FVG and with vs without confluence. Confluence means the level falls within 0.05% of price of the 5m FVG, or an HTF FVG overlaps it. Bias, entry, stop at the FVG edge, costs and sessions are as above. The numbers below reflect the bug fix and moved by less than 0.03R per trade.

### NY session, 1:2 (breakeven win rate 33.3%)

| Variant | Trades | Win rate | vs breakeven | Avg R gross | Avg R net | Exp $/trade | PF (R) | Max DD R | Max DD $ |
|---|---|---|---|---|---|---|---|---|---|
| bias-matched FVG / confluence | 5781 | 34.8% | +1.5 pts | +0.011 | -0.152 | -0.44 | 0.79 | 915 | 8,100 |
| bias-matched FVG / no confluence | 7082 | 35.4% | +2.1 pts | +0.034 | -0.127 | +0.04 | 0.82 | 961 | 8,954 |
| any FVG / confluence | 8456 | 29.5% | -3.9 pts | -0.041 | -0.618 | -1.48 | 0.44 | 5229 | 15,915 |
| any FVG / no confluence | 9511 | 30.0% | -3.4 pts | -0.030 | -0.589 | -1.39 | 0.46 | 5603 | 17,898 |

### NY session, 1:3 (breakeven win rate 25.0%)

| Variant | Trades | Win rate | vs breakeven | Avg R gross | Avg R net | Exp $/trade | PF (R) | Max DD R | Max DD $ |
|---|---|---|---|---|---|---|---|---|---|
| bias-matched FVG / confluence | 5408 | 29.9% | +4.9 pts | +0.009 | -0.156 | -0.65 | 0.80 | 882 | 8,817 |
| bias-matched FVG / no confluence | 6585 | 30.1% | +5.1 pts | +0.026 | -0.136 | -0.23 | 0.82 | 968 | 9,584 |
| any FVG / confluence | 8086 | 24.7% | -0.3 pts | -0.033 | -0.626 | -1.48 | 0.47 | 5073 | 15,846 |
| any FVG / no confluence | 9083 | 25.0% | +0.0 pts | -0.017 | -0.588 | -1.56 | 0.49 | 5353 | 19,525 |

### Asia session, 1:2 (breakeven win rate 33.3%)

| Variant | Trades | Win rate | vs breakeven | Avg R gross | Avg R net | Exp $/trade | PF (R) | Max DD R | Max DD $ |
|---|---|---|---|---|---|---|---|---|---|
| bias-matched FVG / confluence | 5577 | 32.1% | -1.3 pts | -0.003 | -0.379 | -1.72 | 0.55 | 2173 | 13,350 |
| bias-matched FVG / no confluence | 5998 | 32.4% | -1.0 pts | +0.002 | -0.359 | -1.65 | 0.57 | 2239 | 14,834 |
| any FVG / confluence | 7847 | 26.0% | -7.3 pts | -0.045 | -1.067 | -1.85 | 0.26 | 8378 | 17,602 |
| any FVG / no confluence | 8171 | 26.3% | -7.0 pts | -0.043 | -1.043 | -1.81 | 0.27 | 8524 | 18,367 |

### Asia session, 1:3 (breakeven win rate 25.0%)

| Variant | Trades | Win rate | vs breakeven | Avg R gross | Avg R net | Exp $/trade | PF (R) | Max DD R | Max DD $ |
|---|---|---|---|---|---|---|---|---|---|
| bias-matched FVG / confluence | 5240 | 28.3% | +3.3 pts | +0.003 | -0.378 | -1.95 | 0.57 | 2066 | 13,262 |
| bias-matched FVG / no confluence | 5632 | 28.6% | +3.6 pts | +0.016 | -0.350 | -1.58 | 0.60 | 2104 | 13,935 |
| any FVG / confluence | 7543 | 22.6% | -2.4 pts | -0.034 | -1.078 | -1.82 | 0.29 | 8138 | 17,205 |
| any FVG / no confluence | 7848 | 22.8% | -2.2 pts | -0.026 | -1.046 | -1.68 | 0.30 | 8211 | 17,558 |

### Year by year, bias-matched FVG + liquidity confluence (n / win / avg net R)

| Year | Asia 1:2 | Asia 1:3 | NY 1:2 | NY 1:3 |
|---|---|---|---|---|
| 2010 | 37 / 21.6% / -0.92 | 35 / 20.0% / -0.89 | 55 / 23.6% / -0.60 | 50 / 20.0% / -0.57 |
| 2011 | 320 / 32.5% / -0.55 | 302 / 29.1% / -0.54 | 400 / 35.8% / -0.21 | 373 / 30.6% / -0.21 |
| 2012 | 383 / 23.2% / -0.84 | 361 / 20.2% / -0.86 | 389 / 32.4% / -0.30 | 366 / 27.3% / -0.32 |
| 2013 | 348 / 24.4% / -0.81 | 330 / 21.2% / -0.84 | 421 / 30.6% / -0.38 | 395 / 25.1% / -0.40 |
| 2014 | 382 / 25.7% / -0.80 | 354 / 22.9% / -0.83 | 377 / 32.4% / -0.27 | 345 / 29.6% / -0.21 |
| 2015 | 359 / 25.9% / -0.62 | 345 / 21.4% / -0.69 | 341 / 37.5% / -0.09 | 324 / 31.5% / -0.08 |
| 2016 | 389 / 32.9% / -0.38 | 363 / 29.5% / -0.35 | 391 / 30.7% / -0.31 | 376 / 25.8% / -0.34 |
| 2017 | 376 / 28.2% / -0.53 | 355 / 24.5% / -0.53 | 387 / 33.6% / -0.24 | 357 / 28.0% / -0.27 |
| 2018 | 376 / 36.7% / -0.26 | 357 / 32.8% / -0.24 | 354 / 35.6% / -0.09 | 330 / 30.3% / -0.07 |
| 2019 | 340 / 32.9% / -0.31 | 313 / 31.3% / -0.27 | 365 / 32.9% / -0.23 | 335 / 28.1% / -0.26 |
| 2020 | 355 / 35.5% / -0.16 | 336 / 30.7% / -0.16 | 315 / 35.6% / -0.05 | 300 / 29.3% / -0.08 |
| 2021 | 326 / 33.7% / -0.15 | 305 / 28.9% / -0.13 | 377 / 36.9% / -0.02 | 348 / 31.3% / -0.06 |
| 2022 | 298 / 33.6% / -0.23 | 283 / 30.4% / -0.22 | 353 / 38.5% / +0.05 | 335 / 35.2% / +0.08 |
| 2023 | 326 / 39.0% / -0.10 | 302 / 34.8% / -0.10 | 320 / 38.4% / -0.04 | 301 / 34.2% / -0.07 |
| 2024 | 356 / 33.1% / -0.18 | 336 / 27.7% / -0.21 | 339 / 36.6% / -0.03 | 324 / 32.4% / +0.00 |
| 2025 | 331 / 39.3% / +0.04 | 303 / 36.0% / +0.10 | 304 / 34.2% / -0.10 | 283 / 29.7% / -0.07 |
| 2026 | 275 / 42.5% / +0.13 | 260 / 36.5% / +0.17 | 293 / 40.3% / +0.10 | 266 / 35.3% / +0.10 |
Equity curves: `results/equity_{NY,Asia}_1to{2,3}.png`. Trades: `results/trades.csv`.

**Journal comparison (from Notion, 12 trades, Sep 22 – Oct 2 2026):** 2 wins, 1 break-even and 9 losses, a 16.7% win rate and about −2.3R. Both wins were continuation trades; all 5 reversals lost or broke even. Twelve trades can't separate skill from luck: at a true 35% win rate, 2 or fewer wins in 12 happens about 15% of the time.

---

## Changelog
- **Fill/expiry clock mismatch fixed** (affects both studies). FVGs expired after 1440 5m bars, about 7 calendar days, but the fill scan stopped after 7200 minutes, about 5 calendar days. FVGs 5–7 days old that had already been filled could therefore still be traded. Both now use the bar count. Brute-force check: 0 mismatches out of 300 FVGs. Audit: 0 of 6,000 sampled trades used a dead zone. The original study's numbers moved slightly; its conclusions are unchanged.

## Reproduce
```
pip install pandas pyarrow numba matplotlib
# HistData NSXUSD ASCII M1 zips -> data/raw, unzipped to data/x
python3 prep_data.py data/x data/nq_1m.parquet
python3 check_timezone.py
python3 backtest.py && python3 report_original.py      # original study (~1 min)
python3 addons.py && python3 report_addons.py          # add-on study (~2 min)
```
