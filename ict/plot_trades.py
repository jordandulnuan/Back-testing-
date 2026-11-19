"""Candlestick plots of individual trades for visual verification of the pattern detection."""
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np
import pandas as pd

UP, DOWN = "#1baf7a", "#e34948"
LEVEL, IFG, LEG = "#2a78d6", "#eda100", "#6250d6"


def plot_trade(tr, m1, path, before_min=45, after_min=15):
    t_touch = pd.Timestamp(tr["touch_t"])
    t_leg = pd.Timestamp(tr["leg_start_t"])
    t_sig = pd.Timestamp(tr["signal_t"])
    t_exit = pd.Timestamp(tr["exit_t"])
    a = min(t_leg, t_touch) - pd.Timedelta(minutes=10)
    a = min(a, t_touch - pd.Timedelta(minutes=before_min))
    b = t_exit + pd.Timedelta(minutes=after_min)
    w = m1.loc[a:b]
    fig, ax = plt.subplots(figsize=(14, 7), dpi=110)
    x = np.arange(len(w))
    tx = {t: i for i, t in enumerate(w.index)}
    for i, (o, h, l, c) in enumerate(w[["open", "high", "low", "close"]].values):
        col = UP if c >= o else DOWN
        ax.vlines(i, l, h, color=col, lw=0.8)
        ax.add_patch(Rectangle((i - 0.35, min(o, c)), 0.7, max(abs(c - o), 0.05), color=col, lw=0))

    def xi(t):
        return np.searchsorted(w.index.values, np.datetime64(pd.Timestamp(t)))

    # key level zone: from when it became active (or window start) to the touch
    x0 = max(0, xi(pd.Timestamp(tr["level_active_t"])))
    ax.add_patch(Rectangle((x0 - 0.5, tr["level_zb"]), xi(t_touch) - x0 + 1, max(tr["level_zt"] - tr["level_zb"], 0.25),
                           color=LEVEL, alpha=0.25, lw=0))
    ax.text(x0, tr["level_zt"], f" key level: {tr['level_type']} {tr['level_tf']}", color=LEVEL, fontsize=8, va="bottom")
    # IFG gap from its formation to the signal
    x_g = max(0, xi(pd.Timestamp(tr["ifg_known_t"])) - 1)
    ax.add_patch(Rectangle((x_g - 2.5, tr["ifg_bot"]), xi(t_sig) - x_g + 3, tr["ifg_top"] - tr["ifg_bot"],
                           facecolor="none", edgecolor=IFG, lw=1.6, hatch="///", alpha=0.9))
    ax.text(x_g - 2.5, tr["ifg_top"], f" IFG {tr['ifg_tf']}", color="#9a6a00", fontsize=8, va="bottom")
    # manipulation leg: swing extreme -> leg extreme
    ax.plot([xi(t_leg), xi(pd.Timestamp(tr["leg_ext_t"]))], [tr["leg_far"], tr["leg_extreme"]], color=LEG, lw=1.4,
            ls="--")
    ax.text(xi(t_leg), tr["leg_far"], " leg start (1m swing)", color=LEG, fontsize=8)
    # entry / stop / target
    xe = xi(t_sig) - 1
    xx = xi(t_exit)
    for y, col, lab in ((tr["entry"], "#0b0b0b", "entry"), (tr["stop"], DOWN, "stop"), (tr["target"], UP, "target")):
        ax.hlines(y, xe, xx, color=col, lw=1.2)
        ax.text(xx + 0.5, y, f" {lab} {y:.2f}", color=col, fontsize=8, va="center")
    ax.plot(xe, tr["entry"], marker=">", color="#0b0b0b", ms=9)
    ax.plot(xx, tr["exit"], marker="x", color="#0b0b0b", ms=9)
    ax.axvline(xi(t_touch), color=LEVEL, lw=0.8, ls=":")
    ticks = list(range(0, len(w), max(1, len(w) // 12)))
    ax.set_xticks(ticks)
    ax.set_xticklabels([w.index[i].strftime("%H:%M") for i in ticks], fontsize=8)
    ax.set_xlim(-1, len(w) + 8)
    ax.set_title(f"{tr['day']}  {tr['direction_s']}  bias score {tr['bias_score']:+d}  |  level {tr['level_type']} {tr['level_tf']}"
                 f"  |  IFG {tr['ifg_tf']}  |  exit {tr['exit_reason']} ({tr['outcome']}, {tr['r']:+.2f}R)"
                 f"  |  SMT {tr['smt']}", loc="left", fontsize=9)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
