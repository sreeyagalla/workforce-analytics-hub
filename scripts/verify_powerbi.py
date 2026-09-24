"""Verify the generated Power BI model in Power BI Desktop's own engine (Windows + Power BI Desktop).

  python scripts/verify_powerbi.py --out C:\\pbi\\workforce      (full data; add --sample for the sample)

Steps: export the star schema + PBIP, open it in Power BI Desktop, find the local Analysis Services
port, run a TMSL refresh, execute DAX queries for every measure by fiscal year and by fiscal year x
each dimension, compare every value with wfa.metrics.compute(), write a report, close Power BI Desktop.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wfa import metrics, powerbi, settings, warehouse  # noqa: E402
from wfa.metrics import MetricQuery  # noqa: E402

DIM_COLUMN = {"agency": "dim_agency[agency_code]", "location": "dim_location[location]",
              "pay_basis": "dim_pay_basis[pay_basis]", "tenure_band": "dim_tenure_band[tenure_band]"}


def ps(cmd: str) -> str:
    return subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True).stdout.strip()


def pbi_pids() -> set[int]:
    out = ps("Get-Process PBIDesktop,msmdsrv -ErrorAction SilentlyContinue | ForEach-Object { $_.Id }")
    return {int(x) for x in out.split()} if out else set()


def open_in_power_bi(path: Path) -> None:
    """Open a Power BI file with the handler Power BI Desktop registered for its extension (works for the
    Store app, which has no exe alias). Opening the report's definition.pbir also opens its semantic model."""
    import ctypes
    import winreg
    from ctypes import wintypes
    progid = None
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, path.suffix + r"\OpenWithProgids") as k:
            progid = winreg.EnumValue(k, 0)[0]
    except OSError:
        pass
    if not progid:
        os.startfile(str(path))
        return

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]
    info = SHELLEXECUTEINFOW(cbSize=ctypes.sizeof(SHELLEXECUTEINFOW), fMask=0x1, lpVerb="open",
                             lpFile=str(path), lpClass=progid, nShow=1)  # 0x1 = SEE_MASK_CLASSNAME
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        raise OSError(f"ShellExecuteEx failed for {path} with {progid}")


def find_port(started: float, timeout: float = 300) -> int:
    roots = [Path(os.environ["USERPROFILE"]) / "Microsoft" / "Power BI Desktop Store App" / "AnalysisServicesWorkspaces",
             Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Power BI Desktop" / "AnalysisServicesWorkspaces"]
    while time.time() - started < timeout:
        for root in roots:
            for f in root.glob("*/Data/msmdsrv.port.txt") if root.exists() else []:
                if f.stat().st_mtime >= started - 5:
                    raw = f.read_bytes()
                    text = raw.decode("utf-16-le" if b"\x00" in raw else "utf-8").strip("\ufeff \r\n\x00")
                    return int(text)
        time.sleep(2)
    raise TimeoutError("Power BI Desktop did not start an Analysis Services instance")


def queries() -> list[dict]:
    meas = ", ".join(f'"{k}", [{powerbi.measure_name(k)}]' for k in powerbi.MEASURES)
    qs = [{"name": "rowcount", "dax": 'EVALUATE ROW("rows", COUNTROWS(fact_workforce))'},
          {"name": "all", "dax": f"EVALUATE SUMMARIZECOLUMNS ( dim_fiscal_year[fiscal_year], {meas} )"}]
    for dim, col in DIM_COLUMN.items():
        qs.append({"name": dim, "dax": f"EVALUATE SUMMARIZECOLUMNS ( dim_fiscal_year[fiscal_year], {col}, {meas} )"})
    qs.append({"name": "agency_location", "dax": "EVALUATE SUMMARIZECOLUMNS ( dim_fiscal_year[fiscal_year], "
               f"{DIM_COLUMN['agency']}, {DIM_COLUMN['location']}, {meas} )"})
    qs.append({"name": "no_single_year", "dax": f"EVALUATE ROW ( {meas} )"})
    return qs


def expected(con, key: str, dim: str | None) -> dict:
    """{(fy, group): governed value, or None where Power BI must show BLANK}."""
    cat = settings.metric_catalog()
    years = metrics.available_fiscal_years(con)
    if dim and (dim not in cat[key]["dimensions"]
                or (key in powerbi.PRECOMPUTED_DIMS and dim not in powerbi.PRECOMPUTED_DIMS[key])):
        return {"__all_blank__": True}
    df = metrics.compute(con, MetricQuery(key, group_by=[dim] if dim else [], fiscal_years=years)).df
    if dim == "agency":
        codes = dict(con.execute("SELECT agency_name, agency_code FROM dim_agency").fetchall())
        df["agency"] = df["agency"].map(codes)
    if dim == "tenure_band":
        df["tenure_band"] = df["tenure_band"].fillna(powerbi.UNKNOWN_BAND)
    out = {}
    for _, r in df.iterrows():
        v = None if pd.isna(r["value"]) else float(r["value"])
        out[(int(r["fiscal_year"]), r[dim] if dim else None)] = v
    return out


def compare_agency_location(con, res: dict) -> tuple[int, list[str]]:
    """Two filters at once: each agency's breakdown by location."""
    checked, problems = 0, []
    got = {(int(r["dim_fiscal_year[fiscal_year]"]), r[DIM_COLUMN["agency"]], r[DIM_COLUMN["location"]]): r
           for r in res["rows"]}
    codes = [c for (c,) in con.execute("SELECT agency_code FROM dim_agency").fetchall()]
    cat = settings.metric_catalog()
    for key in powerbi.MEASURES:
        blank = "location" not in cat[key]["dimensions"] or (
            key in powerbi.PRECOMPUTED_DIMS and "location" not in powerbi.PRECOMPUTED_DIMS[key])
        for code in codes:
            if blank:
                for cell, row in got.items():
                    if cell[1] == code:
                        checked += 1
                        if row.get(f"[{key}]") is not None:
                            problems.append(f"{key} agency x location {cell}: expected BLANK")
                continue
            df = metrics.compute(con, MetricQuery(key, group_by=["location"], filters={"agency": [code]})).df
            for _, r in df.iterrows():
                checked += 1
                value = None if pd.isna(r["value"]) else float(r["value"])
                engine = got.get((int(r["fiscal_year"]), code, r["location"]), {}).get(f"[{key}]")
                if (value is None) != (engine is None) or (value is not None and not math.isclose(
                        float(engine), value, rel_tol=1e-9, abs_tol=1e-9)):
                    problems.append(f"{key} agency x location {(int(r['fiscal_year']), code, r['location'])}: "
                                    f"governed {value}, Power BI {engine}")
    return checked, problems


def compare(con, results: dict) -> tuple[int, list[str]]:
    checked, problems = 0, []
    c2, p2 = compare_agency_location(con, results["agency_location"])
    checked, problems = checked + c2, problems + p2
    row = (results["no_single_year"]["rows"] or [{}])[0]
    for key in powerbi.MEASURES:
        checked += 1
        if row.get(f"[{key}]") is not None:
            problems.append(f"{key} with no single fiscal year: expected BLANK, got {row[f'[{key}]']}")
    for dim in [None, *DIM_COLUMN]:
        res = results[dim or "all"]
        if res.get("error"):
            problems.append(f"query {dim or 'all'} failed: {res['error']}")
            continue
        rows = res["rows"]
        got = {}
        for row in rows:
            fy = int(row["dim_fiscal_year[fiscal_year]"])
            group = row[DIM_COLUMN[dim]] if dim else None
            got[(fy, group)] = row
        for key in powerbi.MEASURES:
            exp = expected(con, key, dim)
            if "__all_blank__" in exp:
                for cell, row in got.items():
                    checked += 1
                    if row.get(f"[{key}]") is not None:
                        problems.append(f"{key} by {dim} {cell}: expected BLANK, got {row[f'[{key}]']}")
                continue
            for cell, value in exp.items():
                checked += 1
                engine = got.get(cell, {}).get(f"[{key}]")
                if value is None and engine is None:
                    continue
                if value is None or engine is None or not math.isclose(float(engine), value, rel_tol=1e-9, abs_tol=1e-9):
                    problems.append(f"{key} by {dim} {cell}: governed {value}, Power BI {engine}")
    return checked, problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="export folder (use a local, non-OneDrive path)")
    ap.add_argument("--sample", action="store_true")
    ap.add_argument("--keep-open", action="store_true")
    ap.add_argument("--open", choices=["pbir", "pbip"], default="pbir", help="which project file to open")
    args = ap.parse_args()
    paths = settings.get_paths(sample=args.sample)
    con = warehouse.connect(paths.warehouse, read_only=True)
    out = Path(args.out)
    manifest = powerbi.export(con, out, is_sample=args.sample)
    qfile, rfile = out / "verify_queries.json", out / "verify_results.json"
    qfile.write_text(json.dumps(queries()), encoding="utf-8")

    before = pbi_pids()
    started = time.time()
    open_in_power_bi(out / manifest["pbip"] if args.open == "pbip" else out / f"{powerbi.PROJECT}.Report" / "definition.pbir")
    try:
        port = find_port(started)
        print(f"Power BI Desktop engine on port {port}; refreshing and querying...")
        time.sleep(10)
        proc = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                               str(ROOT / "scripts" / "run_dax.ps1"), "-Port", str(port), "-QueriesFile", str(qfile),
                               "-OutFile", str(rfile), "-Refresh"], capture_output=True, text=True, timeout=900)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout)
        data = json.loads(rfile.read_text(encoding="utf-8-sig"))
    except Exception:
        shot = out / "failure_screenshot.png"
        ps("Add-Type -AssemblyName System.Windows.Forms, System.Drawing; $b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds; "
           "$m=New-Object System.Drawing.Bitmap $b.Width,$b.Height; [System.Drawing.Graphics]::FromImage($m).CopyFromScreen("
           f"$b.Location,[System.Drawing.Point]::Empty,$b.Size); $m.Save('{shot}')")
        print(f"Failed; screenshot saved to {shot}")
        raise
    finally:
        if not args.keep_open:
            new = pbi_pids() - before
            if new:
                ps("Stop-Process -Id " + ",".join(map(str, new)) + " -Force -ErrorAction SilentlyContinue")

    loaded = data["results"]["rowcount"]["rows"][0]["[rows]"] if data["results"]["rowcount"]["rows"] else None
    checked, problems = compare(con, data["results"])
    version = ps("(Get-AppxPackage -Name Microsoft.MicrosoftPowerBIDesktop).Version") or "unknown"
    summary = {"date": date.today().isoformat(), "power_bi_desktop": version, "sample": args.sample, "opened": args.open,
               "refresh": data["refresh"], "fact_rows_exported": manifest["tables"][powerbi.FACT],
               "fact_rows_loaded_in_power_bi": loaded, "cells_compared": checked, "mismatches": len(problems)}
    print(json.dumps(summary, indent=2))
    for p in problems[:20]:
        print("  MISMATCH", p)
    (out / "verification_summary.json").write_text(json.dumps({**summary, "problems": problems}, indent=2),
                                                   encoding="utf-8")
    sys.exit(1 if problems or loaded != manifest["tables"][powerbi.FACT] else 0)


if __name__ == "__main__":
    main()
