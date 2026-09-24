# Power BI export

`wfa export-powerbi` writes a star schema and a Power BI Project (`.pbip`). The semantic model already
contains the tables, relationships, and the governed metrics as DAX measures. **The report pages are
not built**: the project opens on one empty page for you to design.

## What's in the model

| Table | Rows (full data) | Contents |
|---|---|---|
| `fact_workforce` | 5,976 | Additive components (counts and exact-to-the-cent sums) at fiscal year × agency × location × pay basis × tenure band. Hidden: use the measures. |
| `dim_fiscal_year`, `dim_agency`, `dim_location`, `dim_pay_basis`, `dim_tenure_band` | 6 / 16 / 6 / 4 / 7 | Dimensions (single-direction, many-to-one relationships to the fact). |
| `agg_median_salary`, `agg_median_salary_total` | 96 / 6 | Median base salary pre-computed by the metric layer, because a median can't be rebuilt from sums. |
| `Metrics` | – | The 9 governed measures ([`measures.dax`](measures.dax)), generated from `config/metrics.yml`. |

The fact's component columns are defined by the same SQL fragments as the metric catalog, so the export
and the dashboard can't drift apart. No names are exported, and no job titles: title-level groups are
mostly under the suppression threshold.

**Governance rules built into every measure:**
- **Small groups:** BLANK for any group with fewer than 10 employees (`config/governance.yml`).
- **One year at a time:** BLANK unless exactly one fiscal year is in context. Every metric is defined
  per fiscal year, so put a fiscal-year slicer or axis on each page.
- **Undefined breakdowns:** BLANK where the catalog doesn't define the metric for that breakdown. For
  example, turnover by tenure band is undefined because the people in a band change every year.
- **Median salary:** only by agency or for all agencies.
- **No implicit measures:** the model discourages them, so report authors use the governed measures
  rather than summing raw columns.

## Open it

1. Export to a local folder **outside OneDrive**. Microsoft warns that PBIP projects in synced folders
   can fail to save.
   ```
   wfa export-powerbi --out C:\pbi\workforce
   ```
2. In Power BI Desktop, open `C:\pbi\workforce\WorkforceAnalytics.pbip`, either with **File > Open**
   or by double-clicking it.
3. Click **Home > Refresh**. The project ships without cached data, so the tables are empty until the
   first refresh loads the CSVs from `data\`.
4. If you move the folder, update **Transform data > Edit parameters > DataFolder**.
5. To save your report, either:
   - use **File > Save as** `.pbix`, or
   - keep it as a project by turning on **File > Options > Preview features > Power BI Project (.pbip)
     save option**. Microsoft still lists Power BI Projects as a preview feature.

## Build the report (your part)

A starting layout that mirrors the dashboard. Every visual uses measures from the `Metrics` table.

- **Overview:**
  - a slicer on `dim_fiscal_year[fiscal_year_label]`;
  - cards for Headcount, Turnover rate, Same-year new-hire attrition, and Overtime hours per worker;
  - a line chart of Turnover rate by `fiscal_year_label` (each point is one year, so the measure is defined);
  - a bar chart of Turnover rate by `dim_agency[agency_name]`.
- **Scorecard:** a matrix with `agency_name` on rows and the measures as values, plus conditional
  formatting on Turnover rate.
- **Locations and pay:** Turnover rate by `dim_location[location]` and by `dim_pay_basis[pay_type]`.

Blank cells are expected. They mean a group is too small or the breakdown is undefined, never zero.

## Verification in Power BI's own engine

`scripts/verify_powerbi.py` runs the whole path on Windows with Power BI Desktop installed. It is not
part of pytest.
1. Export the project.
2. Open it in Power BI Desktop.
3. Connect to Power BI's local engine and run a full refresh.
4. Run DAX queries for all 9 measures: overall, by each dimension, by agency × location, and with no
   single year selected.
5. Compare every cell with `wfa.metrics.compute()`.

Results ([`verification_runs.json`](verification_runs.json)), Power BI Desktop 2.157.1354.0, 2026-09-24:

| Data | Opened via | Fact rows loaded | Cells compared | Mismatches |
|---|---|---|---|---|
| Full (1.1M records) | `definition.pbir` | 5,976 / 5,976 | 6,105 | 0 |
| Sample (24k records) | `definition.pbir` | 3,559 / 3,559 | 5,807 | 0 |
| Sample (24k records) | `.pbip` | 3,559 / 3,559 | 5,807 | 0 |

Suppressed small groups were part of the comparison: 308 cells in the full-data run and 1,002 in each
sample run. Power BI returned BLANK for every one.

Two real bugs were found this way and fixed before these runs:
- A measure named `Separations` collided with the fact column `separations`, because Power BI names
  are case-insensitive. The measures now live in the separate `Metrics` table.
- Overtime hours per worker came back rounded to 4 decimal places, because fixed-decimal ÷ integer
  stays fixed-decimal in Power BI's engine. The numerator is now converted to a floating-point number
  before dividing.

Python-side tests (`tests/test_powerbi_export.py`, 50 tests) check the same logic without Power BI.

## Honest notes

- **The exported fact contains small cells.** They're needed so rollups are exact, and suppression
  happens in the measures. Treat the export folder like the warehouse: analyst-tier, gitignored. If
  you publish a report, don't allow viewers to export underlying data.
- **No row-level security** is defined.
- **The verification ran on one machine and one Power BI Desktop version.** It checks the measures'
  numbers, not any report layout.
