"""Workforce Analytics Hub - self-service dashboard.

Run:  streamlit run app/streamlit_app.py          (full data, after `wfa fetch` + `wfa build`)
      WFA_SAMPLE=1 streamlit run app/streamlit_app.py   (committed sample)

Without a full warehouse (e.g. on Streamlit Community Cloud, where only the repo is available) the app
uses the committed sample, builds its warehouse on first run, and labels every page as sample data.
"""
from __future__ import annotations

import dataclasses
import io
import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))  # hosted deployments install requirements.txt, not this package

from wfa import charts, forecast, metrics, reports, scorecard, settings, warehouse  # noqa: E402
from wfa.metrics import MetricQuery, fmt, fmt_delta  # noqa: E402
from wfa.qa import ask  # noqa: E402
from wfa.qa.vocab import Vocabulary  # noqa: E402
from wfa.warehouse import connect  # noqa: E402

st.set_page_config(page_title="Workforce Analytics Hub", layout="wide")
CAT, DIMS, GOV = settings.metric_catalog(), settings.dimensions(), settings.governance()
K = GOV["privacy"]["min_cell_size"]


def resolve_paths() -> settings.DataPaths:
    paths, sample = settings.get_paths(), settings.get_paths(sample=True)
    if not paths.is_sample and not paths.warehouse.exists() and sample.manifest.exists():
        return sample  # no full data here (e.g. hosted demo): use the committed sample, labelled below
    return paths


@st.cache_resource(show_spinner="Building the sample warehouse (first visit only)...")
def ensure_warehouse(paths: settings.DataPaths) -> Path:
    if paths.warehouse.exists() or not paths.is_sample:
        return paths.warehouse
    try:
        return warehouse.build(paths)
    except OSError:  # read-only checkout: build in the temp folder instead
        alt = dataclasses.replace(paths, warehouse=Path(tempfile.gettempdir()) / "wfa_sample_warehouse.duckdb")
        return alt.warehouse if alt.warehouse.exists() else warehouse.build(alt)


PATHS = resolve_paths()
WAREHOUSE = ensure_warehouse(PATHS)


@st.cache_resource
def _base_connection(path: str):
    return connect(path, read_only=True)


def con():
    return _base_connection(str(WAREHOUSE)).cursor()


if not WAREHOUSE.exists():
    st.error(f"No warehouse found at {WAREHOUSE}. Run `wfa fetch` then `wfa build` "
             "(or `wfa --sample build` and set WFA_SAMPLE=1).")
    st.stop()


@st.cache_data(show_spinner=False)
def compute(metric: str, group_by: tuple = (), filters_json: str = "{}", years: tuple = ()) -> pd.DataFrame:
    q = MetricQuery(metric, group_by=list(group_by), filters=json.loads(filters_json), fiscal_years=list(years) or None)
    return metrics.compute(con(), q).df


@st.cache_data(show_spinner=False)
def compute_sql(metric: str, group_by: tuple = (), filters_json: str = "{}", years: tuple = ()) -> str:
    q = MetricQuery(metric, group_by=list(group_by), filters=json.loads(filters_json), fiscal_years=list(years) or None)
    return metrics.build_sql(q)[0]


@st.cache_data(show_spinner=False)
def meta():
    c = con()
    years = metrics.available_fiscal_years(c)
    agencies = dict(c.execute("SELECT agency_name, agency_code FROM dim_agency ORDER BY 1").fetchall())
    return years, agencies, metrics.dimension_values(c, "location")


@st.cache_data(show_spinner=False)
def card(fy: int, dim: str, filters_json: str):
    sc = scorecard.build(con(), fy, dim, json.loads(filters_json))
    return sc.table, sc.baseline, sc.metric_keys, scorecard.watch_list(sc)


@st.cache_data(show_spinner=False)
def benchmark() -> pd.DataFrame:
    c = con()
    if "bls_benchmark" not in {r[0] for r in c.execute("SHOW TABLES").fetchall()}:
        return pd.DataFrame(columns=["fiscal_year", "value"])
    return c.execute("SELECT fiscal_year, rate AS value FROM bls_benchmark "
                     "WHERE measure = 'total_separations_rate' ORDER BY 1").df()


@st.cache_data(show_spinner="Building Excel and PowerPoint...")
def report_files(fy: int) -> tuple[bytes, bytes]:
    d = reports.collect(con(), fy, PATHS.is_sample)
    xb, pb = io.BytesIO(), io.BytesIO()
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        x = reports.write_excel(d, Path(tmp) / "s.xlsx")
        p = reports.write_pptx(d, Path(tmp) / "s.pptx")
        xb.write(x.read_bytes())
        pb.write(p.read_bytes())
    return xb.getvalue(), pb.getvalue()


@st.cache_data(show_spinner=False)
def planning():
    preds, summary = forecast.backtest(con())
    return forecast.outlook(con()), summary, preds


YEARS, AGENCIES, LOCATIONS = meta()

# ------------------------------------------------------------------ header + one filter row
st.title("Workforce Analytics Hub")
st.caption(f"{len(AGENCIES)} NYC agencies across {len([x for x in LOCATIONS if x not in ('Outside NYC', 'Unknown')])} "
           f"boroughs plus outside-NYC sites · public payroll records FY{YEARS[0]}-FY{YEARS[-1]} (NYC Open Data) · "
           f"names never loaded · groups under {K} employees suppressed")
if PATHS.is_sample:
    sm = json.loads(PATHS.manifest.read_text(encoding="utf-8")) if PATHS.manifest.exists() else {}
    n_rows = sum(f["source_row_count"] for f in sm.get("files", {}).values())
    st.warning(f"SAMPLE DATA: this view runs on a seeded random sample of real payroll records "
               f"({sm.get('per_cell', '?')} per agency per fiscal year, {n_rows:,} in total). Rates are computed "
               "on the sample and headcounts are sample counts, not the actual workforce. Results on the full "
               "1.1M records are in the [project README](https://github.com/sreeyagalla/workforce-analytics-hub"
               "#results-measured-on-the-full-fetched-dataset).")

f1, f2, f3 = st.columns([1, 3, 3])
fy = f1.selectbox("Fiscal year", YEARS[::-1], index=0)
sel_agencies = f2.multiselect("Agencies", list(AGENCIES), placeholder="All agencies in scope")
sel_locations = f3.multiselect("Work locations", LOCATIONS, placeholder="All locations")
filters = {}
if sel_agencies:
    filters["agency"] = [AGENCIES[a] for a in sel_agencies]
if sel_locations:
    filters["location"] = sel_locations
FJ = json.dumps(filters, sort_keys=True)
scope_label = ", ".join(sel_agencies) if sel_agencies else "All agencies in scope"
if sel_locations:
    scope_label += " · " + ", ".join(sel_locations)

tabs = st.tabs(["Overview", "Scorecards", "Explore", "Ask a question", "Planning", "Data quality", "Definitions"])


def value_of(metric: str, year: int, fj: str = FJ):
    df = compute(metric, (), fj, (year,))
    return (df["value"].iloc[0], bool(df["suppressed"].iloc[0])) if len(df) else (float("nan"), False)


# ------------------------------------------------------------------ Overview
with tabs[0]:
    st.subheader(f"FY{fy} · {scope_label}")
    tiles = ["headcount", "turnover_rate", "new_hire_attrition_rate", "ot_hours_per_worker", "median_base_salary"]
    cols = st.columns(len(tiles))
    for c, key in zip(cols, tiles):
        m = CAT[key]
        cur, supp = value_of(key, fy)
        prev, _ = value_of(key, fy - 1) if fy - 1 in YEARS else (float("nan"), False)
        if supp:
            c.metric(m["label"], f"n<{K}", help="Suppressed: fewer than the minimum group size.")
            continue
        if m["format"] == "pct":
            delta = fmt_delta(cur - prev, "pct") if pd.notna(prev) and pd.notna(cur) else None
        else:
            delta = f"{(cur / prev - 1) * 100:+.1f}%" if pd.notna(prev) and prev and pd.notna(cur) else None
        c.metric(m["label"], fmt(cur, m["format"]), delta=f"{delta} vs FY{fy - 1}" if delta else None,
                 delta_color={"higher_is_worse": "inverse", "higher_is_better": "normal"}.get(m["polarity"], "off"),
                 help=m["definition"])

    left, right = st.columns(2)
    with left:
        trend = compute("turnover_rate", (), FJ)
        series = {f"Turnover, {scope_label}": trend[["fiscal_year", "value"]]}
        bench = benchmark()
        if len(bench):
            series["BLS JOLTS, state & local gov. excl. education (national)"] = bench
        st.plotly_chart(charts.plotly_trend(series, "Turnover rate by fiscal year"), width="stretch", theme=None)
        st.caption("Benchmark: BLS total separations rate, monthly rates summed July-June. Context only: different "
                   "population and a survey-based definition.")
    with right:
        tbl, base, keys, _ = card(fy, "agency", json.dumps({k: v for k, v in filters.items()}, sort_keys=True))
        body = tbl[tbl["agency"] != scorecard.ALL_LABEL]
        st.plotly_chart(charts.plotly_ranked_bars(body, "agency", "turnover_rate", f"Turnover rate by agency, FY{fy}",
                                                  "pct", "turnover_rate__rag", base.get("turnover_rate")),
                        width="stretch", theme=None)
        st.caption(f"■ red = at least {GOV['scorecard']['red_ratio']}x the all-in-scope rate (the vertical line).")

    left, right = st.columns(2)
    with left:
        loc = compute("turnover_rate", ("location",), FJ, (fy,))
        loc = loc[~loc["suppressed"]].sort_values("value", ascending=False)
        st.plotly_chart(charts.plotly_bars(loc, "location", "value", f"Turnover rate by work location, FY{fy}", "pct", "population"),
                        width="stretch", theme=None)
    with right:
        pb = compute("turnover_rate", ("pay_basis",), FJ, (fy,))
        pb = pb[~pb["suppressed"]].sort_values("value", ascending=False)
        st.plotly_chart(charts.plotly_bars(pb, "pay_basis", "value", f"Turnover rate by pay basis, FY{fy}", "pct", "population"),
                        width="stretch", theme=None)
    with st.expander("Table view of the charts above"):
        st.dataframe(trend.assign(value=trend["value"].map(lambda v: fmt(v, "pct"))), hide_index=True)
        st.dataframe(pd.concat([loc.assign(group=loc["location"]), pb.assign(group=pb["pay_basis"])])
                     [["group", "value", "numerator", "denominator"]].assign(value=lambda x: x["value"].map(lambda v: fmt(v, "pct"))),
                     hide_index=True)

# ------------------------------------------------------------------ Scorecards
with tabs[1]:
    dim = st.radio("Scorecard by", ["agency", "location"], horizontal=True,
                   format_func=lambda d: DIMS[d]["label"])
    tbl, base, keys, watch = card(fy, dim, FJ)
    disp = pd.DataFrame({DIMS[dim]["label"]: tbl[dim]})
    for key in keys:
        m = CAT[key]
        disp[m["label"]] = [f"n<{K}" if pd.isna(v) else fmt(v, m["format"]) for v in tbl[key]]
        deltas = tbl[f"{key}__delta"]
        disp[f"{m['label']} vs FY{fy - 1}"] = [
            "" if pd.isna(v) else (fmt_delta(v, "pct") if m["format"] == "pct" else f"{v * 100:+.1f}%") for v in deltas]
        if m["polarity"] != "neutral":
            disp[f"{m['label']} status"] = tbl[f"{key}__rag"].map(lambda s: charts.STATUS_LABEL.get(s or "", ""))
    status_cols = [c for c in disp.columns if c.endswith(" status")]
    fill = {"■ Red": "background-color: #f6d5d5", "▲ Amber": "background-color: #fdefcb",
            "● Green": "background-color: #d6efd6"}
    styled = disp.style.map(lambda v: fill.get(v, ""), subset=status_cols)
    st.dataframe(styled, hide_index=True, width="stretch", height=min(700, 36 * (len(disp) + 1) + 4))
    st.caption(f"Status compares each row with '{scorecard.ALL_LABEL}' for metrics where higher is worse: "
               f"■ Red >= {GOV['scorecard']['red_ratio']}x, ▲ Amber >= {GOV['scorecard']['amber_ratio']}x, ● Green below.")
    if watch:
        st.markdown("**Watch list (red)**")
        st.dataframe(pd.DataFrame([{"Unit": w["unit"], "Metric": w["label"], "Value": fmt(w["value"], CAT[w["metric"]]["format"]),
                                    "All in scope": fmt(w["baseline"], CAT[w["metric"]]["format"]),
                                    "Ratio": f"{w['ratio']:.2f}x"} for w in watch]), hide_index=True)
    st.markdown("**Automated report pack** (all agencies in scope, selected fiscal year)")
    if st.button("Build Excel + PowerPoint"):
        st.session_state["reports_fy"] = fy
    if st.session_state.get("reports_fy") == fy:
        xlsx, pptx = report_files(fy)
        c1, c2 = st.columns(2)
        c1.download_button("Download Excel scorecard", xlsx, f"workforce_scorecard_FY{fy}.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        c2.download_button("Download PowerPoint deck", pptx, f"workforce_scorecard_FY{fy}.pptx",
                           "application/vnd.openxmlformats-officedocument.presentationml.presentation")

# ------------------------------------------------------------------ Explore
with tabs[2]:
    c1, c2 = st.columns(2)
    mkey = c1.selectbox("Metric", list(CAT), format_func=lambda k: CAT[k]["label"])
    dim_opts = ["(trend over time)"] + CAT[mkey]["dimensions"]
    dsel = c2.selectbox("Break down by", dim_opts, format_func=lambda d: DIMS[d]["label"] if d in DIMS else d)
    m = CAT[mkey]
    st.markdown(f"**{m['label']}** - {m['definition']}")
    if dsel == "(trend over time)":
        df = compute(mkey, (), FJ)
        st.plotly_chart(charts.plotly_trend({m["label"]: df[["fiscal_year", "value"]]}, f"{m['label']}, {scope_label}",
                                            pct=m["format"] == "pct"), width="stretch", theme=None)
        sql = compute_sql(mkey, (), FJ)
    else:
        df = compute(mkey, (dsel,), FJ, (fy,))
        shown = df[~df["suppressed"]]
        top = shown.sort_values("value", ascending=False).head(25)
        st.plotly_chart(charts.plotly_ranked_bars(top, dsel, "value", f"{m['label']} by {DIMS[dsel]['label'].lower()}, "
                                                  f"FY{fy} (top {len(top)})", m["format"]),
                        width="stretch", theme=None)
        if df["suppressed"].any():
            st.caption(f"{int(df['suppressed'].sum())} groups suppressed (fewer than {K} employees).")
        sql = compute_sql(mkey, (dsel,), FJ, (fy,))
    st.dataframe(df.assign(value=df["value"].map(lambda v: fmt(v, m["format"]))), hide_index=True, width="stretch")
    with st.expander("Caveats"):
        st.write(m.get("caveats", ""))
    with st.expander("SQL generated from the metric catalog"):
        st.code(sql, language="sql")

# ------------------------------------------------------------------ Ask
with tabs[3]:
    st.markdown("Ask in plain English. The question is translated into a governed metric query; every number "
                "in the answer is computed by the metric layer, and questions it can't answer from this data are "
                "refused rather than guessed.")
    examples = ["What was the turnover rate at Parks in FY2025?",
                "Which agency had the highest new-hire attrition in 2024?",
                "Compare hourly vs salaried turnover at Parks in 2025",
                "How has overtime per worker changed at Correction since 2021?",
                "What is John Smith's salary?",
                "Why did turnover go up at Correction?"]
    ex_cols = st.columns(3)
    for i, ex in enumerate(examples):
        if ex_cols[i % 3].button(ex, key=f"ex{i}"):
            st.session_state["question"] = ex
    q = st.text_input("Question", key="question", placeholder="e.g. Turnover by work location for Sanitation in 2025")
    if q:
        vocab = Vocabulary.from_warehouse(con())
        ans = ask(con(), q, vocab=vocab)
        (st.warning if ans.refused else st.success)(ans.text)
        for n in ans.notes:
            st.caption("Note: " + n)
        if ans.suggestions:
            st.caption("Try: " + " · ".join(ans.suggestions))
        st.caption(f"Parser: {ans.parser}" + ("" if ans.parser.startswith("llm") else
                                              " (set ANTHROPIC_API_KEY to use the optional LLM parser)"))
        if not ans.refused:
            with st.expander("Structured query this question was translated to"):
                st.json(asdict(ans.spec))
            with st.expander("Result table"):
                st.dataframe(ans.table, hide_index=True)
            with st.expander("SQL executed"):
                st.code(ans.sql, language="sql")

# ------------------------------------------------------------------ Planning
with tabs[4]:
    outlook, summary, preds = planning()
    method, mape = forecast.select_method(summary)
    st.markdown(f"**Separations outlook, FY{YEARS[-1] + 1}** (whole agencies; not a prediction about individuals)")
    mean = summary.groupby("method")["mape"].mean()
    st.write(f"Backtest FY{int(summary.fiscal_year.min())}-FY{int(summary.fiscal_year.max())}, each year predicted "
             f"from earlier years only. Mean absolute percentage error across agencies: last-year rate "
             f"{mean['last_rate']:.1%}, trailing 3-year rate {mean['trailing_mean']:.1%}, naive same count "
             f"{mean['naive_count']:.1%}. The outlook uses the lowest ({reports.METHOD_LABEL[method]}). "
             "Treat it as a planning baseline.")
    st.dataframe(outlook.assign(**{c: outlook[c].round(0) for c in ["projected_separations", *forecast.METHODS]}),
                 hide_index=True, width="stretch")
    st.markdown("**Backtest detail**")
    st.dataframe(summary.assign(mape=summary["mape"].map(lambda v: f"{v:.1%}")), hide_index=True)

# ------------------------------------------------------------------ Data quality
with tabs[5]:
    dq = con().execute("SELECT status, check_name, category, failing_rows, total_rows, detail FROM dq_results").df()
    counts = dq["status"].value_counts()
    c = st.columns(4)
    for col, s in zip(c, ["pass", "warn", "fail", "info"]):
        col.metric(s.capitalize(), int(counts.get(s, 0)))
    icon = {"pass": "● pass", "warn": "▲ warn", "fail": "■ fail", "info": "i info"}
    order = {"fail": 0, "warn": 1, "info": 2, "pass": 3}
    dq = dq.sort_values("status", key=lambda s: s.map(order), kind="stable")
    st.dataframe(dq.assign(status=dq["status"].map(icon)), hide_index=True, width="stretch",
                 height=36 * (len(dq) + 1) + 4)
    st.markdown("**Lineage**: NYC Open Data API (only non-PII columns requested) -> `raw_payroll` (as fetched) -> "
                "`dim_agency` (governed names by stable code) + `fct_payroll` (typed, standardized, flagged) -> "
                "`config/metrics.yml` (governed metrics) -> dashboard, Excel, PowerPoint, Q&A.")
    if PATHS.manifest.exists():
        with st.expander("Fetch manifest"):
            st.json(json.loads(PATHS.manifest.read_text(encoding="utf-8")))

# ------------------------------------------------------------------ Definitions
with tabs[6]:
    st.dataframe(pd.DataFrame([{"Metric": m["label"], "Definition": m["definition"], "Breakdowns": ", ".join(
        DIMS[d]["label"] for d in m["dimensions"]), "Higher is": {"higher_is_worse": "worse",
                                                                 "higher_is_better": "better"}.get(m["polarity"], "neutral"),
                               "Caveats": m.get("caveats", "")} for m in CAT.values()]),
                 hide_index=True, width="stretch")
    st.markdown(f"**Governance rules** (config/governance.yml): minimum cell size {K}; PII columns never requested: "
                f"{', '.join(GOV['privacy']['pii_columns'])}; RAG: red >= {GOV['scorecard']['red_ratio']}x, amber >= "
                f"{GOV['scorecard']['amber_ratio']}x of the all-in-scope value.")
