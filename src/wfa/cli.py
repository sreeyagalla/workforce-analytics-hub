"""Command line: wfa fetch | build | sample | report | ask | backtest | all

Add --sample to any command to use the committed sample instead of the full data.
"""
from __future__ import annotations

import argparse
import sys

from . import settings


def _con(paths):
    from .warehouse import connect
    if not paths.warehouse.exists():
        sys.exit(f"No warehouse at {paths.warehouse}. Run `wfa build{' --sample' if paths.is_sample else ''}` first.")
    return connect(paths.warehouse, read_only=True)


def cmd_fetch(args, paths):
    from . import bls, ingest
    ingest.fetch_payroll()
    print(bls.fetch_benchmark().to_string(index=False))


def cmd_build(args, paths):
    from . import warehouse
    out = warehouse.build(paths)
    con = warehouse.connect(out, read_only=True)
    dq = con.execute("SELECT status, check_name, failing_rows, total_rows FROM dq_results").df()
    print(f"Built {out}")
    print(dq.to_string(index=False))
    counts = dq["status"].value_counts().to_dict()
    print("DQ summary:", counts)
    if counts.get("fail"):
        sys.exit("Data quality checks failed; see dq_results.")


def cmd_sample(args, paths):
    from . import sample
    m = sample.make_sample()
    print({k: v["source_row_count"] for k, v in m["files"].items()})


def cmd_report(args, paths):
    from . import metrics, reports
    con = _con(paths)
    fy = args.fy or metrics.available_fiscal_years(con)[-1]
    xlsx, pptx = reports.build_all(con, fy, paths.reports_dir, is_sample=paths.is_sample)
    print(f"Wrote {xlsx}\nWrote {pptx}")


def cmd_ask(args, paths):
    from .qa import ask
    a = ask(_con(paths), " ".join(args.question), parser=args.parser)
    print(("[refused: " + a.category + "] " if a.refused else "") + a.text)
    for n in a.notes:
        print("  note:", n)
    for s in a.suggestions:
        print("  try:", s)
    print(f"  (parser: {a.parser})")


def cmd_backtest(args, paths):
    from . import forecast
    preds, summary = forecast.backtest(_con(paths))
    print(summary.to_string(index=False))


def main(argv=None):
    p = argparse.ArgumentParser(prog="wfa", description="Workforce Analytics Hub")
    p.add_argument("--sample", action="store_true", help="use the committed sample data")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch", help="download payroll records (NYC Open Data) and the BLS benchmark")
    sub.add_parser("build", help="build the DuckDB warehouse and run data quality checks")
    sub.add_parser("sample", help="regenerate the committed sample from the fetched files")
    r = sub.add_parser("report", help="write the Excel scorecard and PowerPoint deck")
    r.add_argument("--fy", type=int)
    a = sub.add_parser("ask", help="ask a workforce question")
    a.add_argument("question", nargs="+")
    a.add_argument("--parser", choices=["auto", "rules", "llm"], default=None)
    sub.add_parser("backtest", help="backtest the separations outlook")
    sub.add_parser("all", help="fetch + build + report")
    args = p.parse_args(argv)
    paths = settings.get_paths(sample=args.sample)
    if args.cmd == "all":
        if not args.sample:
            cmd_fetch(args, paths)
        cmd_build(args, paths)
        args.fy = None
        cmd_report(args, paths)
        return
    {"fetch": cmd_fetch, "build": cmd_build, "sample": cmd_sample, "report": cmd_report,
     "ask": cmd_ask, "backtest": cmd_backtest}[args.cmd](args, paths)


if __name__ == "__main__":
    main()
