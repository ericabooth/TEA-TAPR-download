# TEA-TAPR-download

Python tools for bulk-downloading school data from the Texas Education Agency
(TEA):

- **TAPR** — Texas Academic Performance Reports, school years 2012-13 through
  2024-25, at campus, district, region, and state level.
- **Texas Assessment Research Portal** — STAAR, STAAR Alternate 2, and TELPAS
  results with twelve student-group breakdowns (including Section 504,
  gifted/talented, Title I, and migrant status) that TAPR does not carry.

TEA publishes this data behind multi-step web forms with no API, no bulk
download, and no stable file URLs. These scripts automate the forms and add
what a reliable bulk download needs: routing across TEA's several download
interfaces, response validation, retry with backoff, resume, and a manifest
with checksums.

## Requirements

Python 3.9+. `tapr_download.py` needs `requests`; the portal scripts use the
standard library only. `fetch_dictionaries.py` needs `pdftotext`
(`brew install poppler`).

```bash
pip install requests
```

## Quick start

Probe TEA's endpoints first — a site-side change shows up as a named failure
instead of a mysterious empty download later:

```bash
python3 tapr_download.py --health
```

Download district and campus files for all available years:

```bash
python3 tapr_download.py --years 2013-2025 --levels C D
```

A single dataset, to try it out:

```bash
python3 tapr_download.py --years 2024 --levels D --datasets REF
```

Runs resume: files already present are skipped, so an interrupted run can be
restarted with the same command.

**Year convention:** `--years` takes TEA's `ccyy`, the **spring** year, so
`--years 2025` means school year 2024-25.

## The TAPR routes

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

### Options

| flag | default | meaning |
|---|---|---|
| `--years` | `2013-2025` | `2013-2025`, or `2019,2021`, or a mix (spring years) |
| `--levels` | `C D` | `C` campus, `D` district, `R` region, `S` state |
| `--datasets` | all | e.g. `REF STAAR1` (setpick) or `REF STUD STAAR_ALL` (wizard) |
| `--output` | `tapr_data` | output directory |
| `--pace` | `2.5` | base seconds between requests; do not lower it — TEA throttles |
| `--route` | `auto` | `auto`, `setpick` or `wizard` |
| `--health` | off | probe every TEA endpoint (6 checks) and exit |
| `--verify` | off | integrity check against the live site; prints receipts, writes `integrity_report.csv` |
| `--check` | off | verify files already on disk (gzip, rectangular, sha256 vs manifest) |
| `--backfill` | off | with `--check`: record checksums for files that lack one |
| `--audit-years` | — | write a variable inventory (names and labels per year) and exit |
| `--force` | off | re-download files that already exist |
| `--no-compress` | off | write plain `.csv` instead of `.csv.gz` |

### Output layout

```
tapr_data/
  manifest.csv              status, dimensions, SHA-256 for every request
  manifest.json
  2024/
    Districts/
      tapr_2024_D_REF.csv.gz
      _dictionary/REF.html
    Campuses/
      ...
```

### Verifying integrity

TEA answers HTTP 200 for every failure mode — error stubs, throttle pages, and
empty bodies all arrive as 200 — so "it downloaded" proves nothing. `--verify`
re-downloads each dataset and checks it: every row matches the header width,
every offered column was requested, entity ids are unique and reconcile against
that year's reference file. It ends with a receipts grid of columns returned
per dataset per year, so a dataset that silently shrank is visible at a glance.

`--check` is the companion for data already on disk: gzip decodes, CSV parses,
rows are rectangular, checksums match the manifest.

## Why scraping TEA is tricky

The short list; [`DEBUGGING.md`](DEBUGGING.md) documents each with evidence.

- **HTTP 200 on every failure.** Validate structure, never status codes. Even
  `Content-Type` on a successful download is an unresolved SAS macro.
- **Throttling, two ways.** TEA drops TCP connections *and* returns HTTP 429
  pages. A throttled response is indistinguishable from "no data for this
  year" — re-run any negative finding slowly before believing it.
- **The wizard route requires `var_type`.** Omit it and TEA returns identifier
  columns only: a well-formed file with no data in it.
- **Identifiers are zero-padded strings.** `DISTRICT` is 6 characters, `CAMPUS`
  is 9. Read them as numeric and `001902` becomes `1902`.
- **Apostrophe-guarded ids, 2021-2024.** Values arrive as `'001902` on the
  setpick routes in those years. The scripts strip them.
- **The year is embedded in variable names.** `DDA03ARE1019D` (2019) and
  `DDA03ARE1023D` (2023) are the same measure; the two-digit year sits before
  the trailing N/D/R suffix.
- **Column order moves between years.** Find columns by name, never position.

## Variable labels by year

TAPR data files carry human-readable labels only from SY 2024-25. For earlier
years the labels live in TEA's per-year data dictionary PDFs (not the glossary,
which contains no variable names at all). `fetch_dictionaries.py` harvests both
sources into one crosswalk:

```bash
python3 fetch_dictionaries.py --years 2013-2025 --output dictionaries
```

writes `dictionaries/variable_labels.csv`, one row per year x variable with
TEA's label text. `--verify-dd` checks the harvest's dataset-code table against
the live site and prints corrections if TEA has renamed anything.

## Assessment Research Portal

`portal_download.py` bulk-downloads STAAR / STAAR Alternate 2 / TELPAS results
from `txresearchportal.com`, a public JSON API. See [`PORTAL.md`](PORTAL.md)
for the full API documentation, including the two request quirks that make the
difference between data and an empty file.

```bash
python3 portal_download.py --health
python3 portal_download.py --list
python3 portal_download.py --estimate --levels D
python3 portal_download.py --levels D --administrations "Spring 2024"
```

Always `--estimate` before a large run. Organizations are batched (default 100
per request; the server rejects 200+), subjects and grades are multi-selected
into single queries, and output carries the TEA campus/district identifier in
an `ID/CDC` column, so it joins to TAPR directly. Runs resume; a manifest
records every slice.

## Post-processing for Stata

Two optional steps prepare the downloads for Stata and build a cross-year
codebook:

```bash
python3 prep_stata.py --input tapr_data --output stata_stage
stata-mp -b do build_codebook.do
```

`prep_stata.py` decompresses, normalises names, strips TEA's apostrophe
guards, detects which columns must stay string (by content — any column holding
a leading-zero numeric), and emits per-year `label var` do-files from the
harvested dictionary. `build_codebook.do` (requires the `datadictionary`
package, `ssc install datadictionary`) imports, appends, and writes per-year
codebooks whose **Changes sheet** lists every variable added, dropped,
relabelled, or retyped between consecutive years, plus presence, drift, and
label-coverage reports.

## Other TEA data

[`SOURCES.md`](SOURCES.md) surveys TEA's other public data products —
discipline, enrollment, accountability, graduation, directory data — with
verified URLs and download mechanics for each.

## Scale and responsible use

A full campus-and-district TAPR sweep is on the order of 10-30 GB gzipped and
takes hours; the pacing is deliberate. The scripts identify themselves with a
contact address in the User-Agent rather than spoofing a browser — if you fork
this, put your own contact there. Keep the default pacing; hammering the
server gets connections dropped and, worse, produces responses that look like
missing data.

## Superseded

`tapr_scraper_full.py` is the original scraper this project replaced. It runs
without errors and downloads files that contain no data. It is kept only as a
reference; [`DEBUGGING.md`](DEBUGGING.md) explains what was wrong with it.

## Author

Eric A. Booth (eric.a.booth@gmail.com).
