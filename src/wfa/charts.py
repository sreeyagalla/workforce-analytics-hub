"""Chart builders shared by the dashboard (Plotly) and the PowerPoint deck (matplotlib).

Style: one y-axis per chart, 2px lines with 8px markers, bars capped in thickness
with a 2px surface gap, solid hairline gridlines, text in ink tokens (never the
series color). Series 1 (in-scope data) is blue, series 2 (external benchmark)
is orange; that pair was run through the palette validator. Status colors are
used only for RAG status and always paired with a text label.
"""
from __future__ import annotations

import io

import pandas as pd

SURFACE, INK, INK2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
STATUS = {"green": "#0ca30c", "amber": "#fab219", "red": "#d03b3b"}
STATUS_LABEL = {"green": "● Green", "amber": "▲ Amber", "red": "■ Red", "": ""}
FONT = "system-ui, -apple-system, Segoe UI, sans-serif"


# ------------------------------------------------------------------ plotly

def _layout(fig, title: str, y_fmt: str | None = None, height: int = 380):
    fig.update_layout(
        title=dict(text=title, font=dict(size=15, color=INK), x=0, xanchor="left"),
        font=dict(family=FONT, color=INK2, size=12), paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
        margin=dict(l=10, r=90, t=70, b=10), height=height, hovermode="closest",
        legend=dict(orientation="h", yanchor="top", y=-0.12, x=0, font=dict(color=INK2)),
        hoverlabel=dict(bgcolor="white", font=dict(color=INK, family=FONT)), barcornerradius=4,
    )
    fig.update_xaxes(showgrid=False, linecolor=AXIS, tickcolor=AXIS, tickfont=dict(color=MUTED), automargin=True)
    fig.update_yaxes(gridcolor=GRID, gridwidth=1, zeroline=False, linecolor=AXIS, tickfont=dict(color=MUTED),
                     automargin=True)
    if y_fmt:
        fig.update_yaxes(tickformat=y_fmt)
    return fig


def plotly_trend(series: dict[str, pd.DataFrame], title: str, pct: bool = True):
    """series: {name: DataFrame[fiscal_year, value]}; first is the in-scope series, second the benchmark."""
    import plotly.graph_objects as go
    fig = go.Figure()
    years = sorted({int(y) for df in series.values() for y in df.dropna(subset=["value"])["fiscal_year"]})
    for i, (name, df) in enumerate(series.items()):
        df = df.dropna(subset=["value"]).sort_values("fiscal_year")
        fig.add_trace(go.Scatter(
            x=df["fiscal_year"].astype(int), y=df["value"], name=name, mode="lines+markers",
            customdata=[f"FY{int(y)}" for y in df["fiscal_year"]],
            line=dict(color=SERIES[i], width=2), marker=dict(size=8, color=SERIES[i], line=dict(color=SURFACE, width=2)),
            hovertemplate=f"{name}<br>%{{customdata}}: %{{y:{'.1%' if pct else ',.1f'}}}<extra></extra>"))
        if len(df):
            last = df.iloc[-1]
            fig.add_annotation(x=int(last.fiscal_year), y=last["value"], xanchor="left", xshift=8, showarrow=False,
                               text=f"{last['value']:.1%}" if pct else f"{last['value']:,.1f}", font=dict(color=INK2))
    _layout(fig, title, ".0%" if pct else None)
    fig.update_xaxes(tickvals=years, ticktext=[f"FY{y}" for y in years])
    fig.update_yaxes(rangemode="tozero")
    return fig


def plotly_ranked_bars(df: pd.DataFrame, label_col: str, value_col: str, title: str, fmt_key: str,
                       status_col: str | None = None, reference: float | None = None, reference_label: str = "Overall"):
    """Horizontal bars, largest at top. Red-status units use the status color and a text label."""
    import plotly.graph_objects as go
    from .metrics import fmt
    d = df.dropna(subset=[value_col]).sort_values(value_col)
    red = d[status_col].eq("red") if status_col else pd.Series(False, index=d.index)
    colors = [STATUS["red"] if r else SERIES[0] for r in red]
    labels = [fmt(v, fmt_key) + ("  ■ red" if r else "") for v, r in zip(d[value_col], red)]
    fig = go.Figure(go.Bar(
        x=d[value_col], y=d[label_col], orientation="h", marker=dict(color=colors, line=dict(width=0)),
        text=labels, textposition="outside", textfont=dict(color=INK2), cliponaxis=False,
        hovertemplate="%{y}: %{text}<extra></extra>", showlegend=False))
    if reference is not None:
        fig.add_vline(x=reference, line=dict(color=INK2, width=1))
        fig.add_annotation(x=reference, y=1.0, yref="paper", yanchor="bottom", showarrow=False,
                           text=f"{reference_label} {fmt(reference, fmt_key)}", font=dict(color=INK2, size=11))
    fig.update_layout(bargap=0.35)
    _layout(fig, title, ".0%" if fmt_key == "pct" else None, height=max(260, 28 * len(d) + 110))
    fig.update_xaxes(showgrid=True, gridcolor=GRID, range=[0, float(d[value_col].max()) * 1.22 if len(d) else 1],
                     tickformat=".0%" if fmt_key == "pct" else None)
    fig.update_yaxes(tickformat=None)
    fig.update_yaxes(showgrid=False)
    return fig


def plotly_bars(df: pd.DataFrame, x_col: str, y_col: str, title: str, fmt_key: str, n_col: str | None = None):
    """Vertical bars; with n_col, each label shows the group size so small groups are not over-read."""
    import plotly.graph_objects as go
    from .metrics import fmt
    d = df.dropna(subset=[y_col])
    labels = [fmt(v, fmt_key) + (f" (n={n:,.0f})" if n_col else "") for v, n in
              zip(d[y_col], d[n_col] if n_col else [None] * len(d))]
    fig = go.Figure(go.Bar(x=d[x_col], y=d[y_col], marker=dict(color=SERIES[0]),
                           text=labels, textposition="outside",
                           textfont=dict(color=INK2), cliponaxis=False,
                           hovertemplate="%{x}: %{text}<extra></extra>"))
    fig.update_layout(bargap=0.45, uniformtext=dict(minsize=10, mode="show"))
    return _layout(fig, title, ".0%" if fmt_key == "pct" else None)


# ------------------------------------------------------------------ matplotlib (static, for PowerPoint)

def _mpl():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        "font.family": ["Segoe UI", "DejaVu Sans", "sans-serif"], "font.size": 11, "text.color": INK2,
        "axes.facecolor": SURFACE, "figure.facecolor": SURFACE, "axes.edgecolor": AXIS, "axes.labelcolor": INK2,
        "xtick.color": MUTED, "ytick.color": MUTED, "axes.grid": False, "grid.color": GRID, "grid.linewidth": 0.8,
        "axes.spines.top": False, "axes.spines.right": False,
    })
    return plt


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=200, bbox_inches="tight")
    import matplotlib.pyplot as plt
    plt.close(fig)
    return buf.getvalue()


def png_trend(series: dict[str, pd.DataFrame], title: str) -> bytes:
    plt = _mpl()
    from matplotlib.ticker import PercentFormatter
    fig, ax = plt.subplots(figsize=(8, 4))
    years = sorted({int(y) for df in series.values() for y in df.dropna(subset=["value"])["fiscal_year"]})
    for i, (name, df) in enumerate(series.items()):
        df = df.dropna(subset=["value"]).sort_values("fiscal_year")
        x = df["fiscal_year"].astype(int).tolist()
        ax.plot(x, df["value"], color=SERIES[i], linewidth=2, marker="o", markersize=6,
                markeredgecolor=SURFACE, markeredgewidth=1.5, label=name, solid_capstyle="round")
        if len(df):
            ax.annotate(f"{df['value'].iloc[-1]:.1%}", (x[-1], df["value"].iloc[-1]), xytext=(8, 0),
                        textcoords="offset points", va="center", color=INK2)
    from matplotlib.ticker import MaxNLocator
    ax.set_xticks(years, [f"FY{y}" for y in years])
    ax.yaxis.set_major_locator(MaxNLocator(nbins=6, steps=[1, 2, 5, 10]))
    ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    ax.set_ylim(bottom=0)
    ax.grid(axis="y")
    ax.set_title(title, loc="left", color=INK, fontsize=13)
    ax.legend(frameon=False, loc="upper left", bbox_to_anchor=(0, -0.08), ncol=2)
    return _png(fig)


def png_ranked_bars(df: pd.DataFrame, label_col: str, value_col: str, title: str, fmt_key: str,
                    status_col: str | None = None, reference: float | None = None) -> bytes:
    plt = _mpl()
    from .metrics import fmt
    d = df.dropna(subset=[value_col]).sort_values(value_col)
    red = d[status_col].eq("red").tolist() if status_col else [False] * len(d)
    fig, ax = plt.subplots(figsize=(8, max(3, 0.32 * len(d) + 1)))
    ax.barh(d[label_col], d[value_col], height=0.6, color=[STATUS["red"] if r else SERIES[0] for r in red])
    vmax = d[value_col].max()
    for y, (v, r) in enumerate(zip(d[value_col], red)):
        ax.text(v + vmax * 0.01, y, fmt(v, fmt_key) + ("  ■ red" if r else ""), va="center", color=INK2, fontsize=9)
    if reference is not None:
        ax.axvline(reference, color=INK2, linewidth=1)
        ax.text(reference, len(d) - 0.3, f" Overall {fmt(reference, fmt_key)}", color=INK2, fontsize=9, va="bottom")
    from matplotlib.ticker import MaxNLocator
    ax.xaxis.set_major_locator(MaxNLocator(nbins=6, steps=[1, 2, 5, 10]))
    if fmt_key == "pct":
        from matplotlib.ticker import PercentFormatter
        ax.xaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    ax.set_xlim(0, vmax * 1.25)
    ax.grid(axis="x")
    ax.tick_params(axis="y", length=0, labelcolor=INK2)
    ax.set_title(title, loc="left", color=INK, fontsize=13)
    return _png(fig)
