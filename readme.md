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
| **`tapr_download.py`** | The downloader. Routes each year to the right one of TEA's three download interfaces, validates responses, retries, resumes, and writes a manifest. **Use this.** |
| `tapr_scraper_full.py` | Original scraper. **Superseded — do not use.** It silently downloads empty files; see [DEBUGGING.md](DEBUGGING.md). Kept for reference. |
| `tapr_example_district.py` | Minimal single-file example against the wizard route. Still works, useful for checking connectivity. |
| `portal_probe.py` | Working client for the Texas Assessment Research Portal JSON API (statewide, district and campus, batched, with CSV export). |
| [`DEBUGGING.md`](DEBUGGING.md) | What was broken, what was verified against the live site, and TEA's own broken links. |
| [`PLAN.md`](PLAN.md) | Plan for the longitudinal build and the Stata merge/label/clean workflow. |
| [`SOURCES.md`](SOURCES.md) | What TAPR misses: other TEA data products, verified, with download recipes. |
| [`PORTAL.md`](PORTAL.md) | txresearchportal.com API map and scripting plan (STAAR, STAAR Alt 2, TELPAS, with 12 breakdown dimensions TAPR does not have). |

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

Prove the extracts are complete and rectangular, and print receipts:

```bash
python3 tapr_download.py --verify --years 2013-2025 --levels D
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
| `--levels` | `C D` | `C` campus, `D` district, `R` region, `S` state, `O` county (county is wizard-only, and TEA's county page is broken) |
| `--datasets` | all | e.g. `REF STAAR1` (setpick) or `REF STUD STAAR_ALL` (wizard). Codes differ by route — see below |
| `--output` | `tapr_data` | output directory |
| `--pace` | `2.5` | base seconds between requests. Do not lower this much; TEA throttles |
| `--no-compress` | off | write plain `.csv` instead of `.csv.gz` |
| `--no-dictionary` | off | skip saving TEA's data-dictionary pages |
| `--force` | off | re-download files that already exist |
| `--audit-years` | — | write `variable_inventory.csv` and exit |
| `--verify` | off | integrity check; prints receipts, writes `integrity_report.csv` |
| `--route` | `auto` | `auto`, `setpick` or `wizard` — see below |

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

## The routes

TEA serves TAPR through incompatible interfaces whose dataset codes differ, so
`--datasets` values are not interchangeable between them.

| `--route` | years | dataset codes | header rows |
|---|---|---|---|
| **`setpick`** | 2013 - SY 2023-24 (plus non-assessment data for SY 2024-25) | `REF`, `STAAR1`-`STAAR6`, `PART1`/`PART2`, `STAAR_ADD1`-`5`, `TAKS1`/`TAKS2` (2013-14), `GRAD`, `COMP`, `PERF`/`PERF1`-`3`, `PROF`, `KG`, `OC`/`OG`, `PKEFF`, `ACCLER`, `STAARV`, `PARTV` — the set changes by year | 1 (varnames) |
| `wizard` | SY 2023-24 onward | `REF`, `STUD`, `STAF`, `STAAR_GR3`-`GR8`, `STAAR_GR38`, `STAAR_ALL`, `STAAR_EOC`, `STAAR_SP`, `PART`, `BIL1`/`BIL2`, `KG`, `PK`, `DROP_ATT`, `COMP4`-`6`, `RHSP`, `FHSP`, `GRAD`-`GRAD4`, `APIB`, `CAD`, `ADV`, `TXIHE` — 33 codes | 2 (labels, then varnames) |

**Leave `--route` on `auto`.** It uses the setpick route through SY 2023-24,
which gives one consistent schema — single header row, full
numerator/denominator/rate coverage, year-embedded variable names — for every
year from 2013, then switches to the wizard for SY 2024-25 onward, where the
setpick route has no assessment data.

Note the year convention: `--years` takes `ccyy`, the **spring** year, so
`--years 2025` means school year 2024-25.

Use `--route wizard` to force the wizard everywhere, for the categories only it
offers (`STUD`, `STAF`, `STAAR_ALL`, `DROP_ATT`, `PK`, `APIB`, `TXIHE`).
Use `--route setpick` to force the setpick route, which still returns the
non-assessment datasets (`REF`, `GRAD`, `COMP`, `PERF1`-`3`, `PROF`, `KG`,
`PKEFF`) for SY 2024-25 if you want schema continuity for those.

Run without `--datasets` to get everything available for each year.

## Verifying integrity

`--verify` downloads each dataset and checks it rather than trusting that a
200 response means data. Per year x level x dataset it reports:

| check | meaning |
|---|---|
| `rectangular` | every data row has exactly as many fields as the header — catches truncation |
| `key capture` | the parser submitted every `key` the page offered, counted independently of the parser |
| `unique ids` | no duplicate entity ids |
| `non-blank` | rows whose id column is empty (TEA writes a bare apostrophe in some datasets) |
| `coverage` | entity ids reconciled against that year's `REF` universe |

It ends with a receipts grid of columns returned per dataset x year, so a
dataset that silently shrank is visible at a glance, and writes
`integrity_report.csv` for the details.

Results so far:

| run | scope | result |
|---|---|---|
| District, 2013-2025 | 260 dataset-years | rectangular **PASS**, key capture **PASS**; 6 real issues (below) |
| Campus, 2019/2024/2025 | 83 dataset-years | rectangular **PASS**, unique ids **PASS**, non-blank **PASS**, key capture **PASS**; 1 real issue |

Nothing is being truncated and every column TEA offers is being requested. The
real issues are TEA data characteristics to handle on import, not download
defects:

- `PKEFF` and `KG` carry rows with a **blank district id** (a bare apostrophe):
  76 rows in `PKEFF` 2022, 27 in 2023. Drop them on import.
- A few entities appear in `KG` (11-18 districts/yr, 81 campuses in 2019),
  `PKEFF` (8-10) and `ACCLER` (1) but **not in that year's `REF`**.

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

The largest seam is at SY 2024-25, where the panel has to change routes because
the setpick route carries no assessment data for that year. See
[`DEBUGGING.md`](DEBUGGING.md) for the evidence and the reason.

## Assessment portal

`portal_probe.py` is a working client for TEA's Texas Assessment Research
Portal, which carries student-group breakdowns TAPR does not have — `gifted`,
`plan_504`, `titleia_flag`, `migrant` — plus STAAR Alternate 2 and TELPAS.

```bash
python3 portal_probe.py --map
```

```bash
python3 portal_probe.py --query --all-districts --batch 50 --out staar.csv
```

Output carries the TEA identifier in an `ID/CDC` column (6-digit CDN for
districts, 9-digit campus id for campuses), so it joins to TAPR with no
crosswalk. See [`PORTAL.md`](PORTAL.md) for the API map and the two non-obvious
gotchas that make the difference between data and a 2-byte file.

## Author

Eric A. Booth (eric.a.booth@gmail.com).
