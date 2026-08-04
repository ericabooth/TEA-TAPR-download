# TEA TAPR data downloader

Downloads **Texas Academic Performance Report (TAPR)** flat files from the Texas
Education Agency, across school years, aggregation levels and data categories.

TEA's TAPR site has no API and no resolvable download URLs — it drives a SAS
"broker" CGI through a series of dropdowns, radio buttons and checkboxes. This
project automates that form flow and records enough metadata alongside each file
to build a longitudinal panel afterwards.

## Files

| file | what it is |
|---|---|
| **`tapr_download.py`** | The downloader. Handles both of TEA's TAPR applications (2013-2023 and 2024-), validates responses, retries, resumes, and writes a manifest. **Use this.** |
| `tapr_scraper_full.py` | Original scraper. **Superseded — do not use.** It silently downloads empty files; see [DEBUGGING.md](DEBUGGING.md). Kept for reference. |
| `tapr_example_district.py` | Minimal single-file example against the modern endpoint. Still works, useful for checking connectivity. |
| `portal_probe.py` | Reproducible probe for the Texas Assessment Research Portal JSON API. Maps the endpoints, organization tree and query wizard. |
| [`DEBUGGING.md`](DEBUGGING.md) | What was broken, what was verified against the live site, and TEA's own broken links. |
| [`PLAN.md`](PLAN.md) | Plan for the longitudinal build and the Stata merge/label/clean workflow. |
| [`SOURCES.md`](SOURCES.md) | What TAPR misses: other TEA data products, verified, with download recipes. |
| [`PORTAL.md`](PORTAL.md) | txresearchportal.com API map and scripting plan (STAAR, STAAR Alt 2, TELPAS, with 12 breakdown dimensions). |

## Requirements

Python 3.9+ and `requests`:

```bash
pip install requests
```

## Usage

Download district and campus files for all available years:

```bash
python3 tapr_download.py --years 2013-2025 --levels C D
```

A single dataset, to try it out:

```bash
python3 tapr_download.py --years 2024 --levels D --datasets REF
```

Write a variable inventory (one row per year x level x dataset x variable, with
TEA's label text) without keeping the data — this is the input to the
label-drift crosswalk described in `PLAN.md`:

```bash
python3 tapr_download.py --audit-years 2013-2025 --levels D
```

### Options

| flag | default | meaning |
|---|---|---|
| `--years` | `2013-2025` | `2013-2025`, or `2019,2021`, or a mix |
| `--levels` | `C D` | `C` campus, `D` district, `R` region, `S` state, `O` county (county is 2024+ only) |
| `--datasets` | all | e.g. `REF STUD STAAR_ALL`. Codes differ between the two endpoints — see below |
| `--output` | `tapr_data` | output directory |
| `--pace` | `2.5` | base seconds between requests. Do not lower this much; TEA throttles |
| `--no-compress` | off | write plain `.csv` instead of `.csv.gz` |
| `--no-dictionary` | off | skip saving TEA's data-dictionary pages |
| `--force` | off | re-download files that already exist |
| `--audit-years` | — | write `variable_inventory.csv` and exit |

## Output layout

```
tapr_data/
  manifest.csv              status, dimensions, SHA-256 for every request
  manifest.json
  variable_inventory.csv    (--audit-years only)
  2024/
    Districts/
      tapr_2024_D_REF.csv.gz
      _dictionary/REF.html
    Campuses/
      ...
```

Runs resume: files already present are skipped, so an interrupted run can be
restarted with the same command.

## The two routes

TEA serves TAPR through two incompatible interfaces, and the dataset codes are
different, so `--datasets` values are not interchangeable between them.

| `--route` | years | dataset codes | header rows |
|---|---|---|---|
| **`setpick`** (default) | 2013-2025 | `REF`, `STAAR1`-`STAAR6`, `PART1`/`PART2`, `STAAR_ADD1`-`5`, `TAKS1`/`TAKS2` (2013-14), `GRAD`, `COMP`, `PERF`/`PERF1`-`3`, `PROF`, `KG`, `OC`/`OG`, `PKEFF`, `ACCLER`, `STAARV`, `PARTV` — the set changes by year | 1 (varnames) |
| `wizard` | 2024- | `REF`, `STUD`, `STAF`, `STAAR_GR3`-`GR8`, `STAAR_GR38`, `STAAR_ALL`, `STAAR_EOC`, `STAAR_SP`, `PART`, `BIL1`/`BIL2`, `KG`, `PK`, `DROP_ATT`, `COMP4`-`6`, `RHSP`, `FHSP`, `GRAD`-`GRAD4`, `APIB`, `CAD`, `ADV`, `TXIHE` — 33 codes | 2 (labels, then varnames) |

**Default to `setpick`.** It covers every year 2013-2025 with one consistent
schema — single header row, full numerator/denominator/rate coverage, and the
same year-embedded variable names — which is what makes the years appendable.
The script switches `prgopt` internally at 2024; you do not need to care.

Use `--route wizard` when you want the categories only it offers (`STUD`,
`STAF`, `STAAR_ALL`, `DROP_ATT`, `PK`, `APIB`, `TXIHE`, ...), or for 2025 STAAR,
which the setpick route does not serve.

Run without `--datasets` to get everything available for each year.

## Scale

Campus level is large: the 2024 campus `STAAR_ALL` extract alone is 20 MB raw
(1,100 columns x 9,082 campuses), 6.5 MB gzipped. A full campus + district run
across 2013-2025 is on the order of 10-30 GB gzipped, and takes hours — the
pacing is deliberate and TEA will drop connections if you rush it.

## Before you use the data

Read [`PLAN.md`](PLAN.md) first. TAPR files are not appendable as they stand:
the two-digit year is embedded in every variable name, labels embed the year,
variables appear and disappear, column order moves, 2022 uses mixed-case names
and 2023 prefixes every ID with an apostrophe. `PLAN.md` documents the
harmonisation rules and the Stata-side workflow.

## Author

Eric A. Booth (eric.a.booth@gmail.com).
