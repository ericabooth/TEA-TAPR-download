# Handoff and maintenance guide

Instructions for taking over this project: what it does, how to run it, what
will bite you, and what to do next.

Read this file first, then [`DEBUGGING.md`](DEBUGGING.md) before touching the
downloaders, and [`PLAN.md`](PLAN.md) before touching the Stata side.

---

## 1. What this project is

It downloads Texas Education Agency (TEA) school data and turns it into a
longitudinal campus-and-district panel, with the metadata needed to know
whether a variable means the same thing from one year to the next.

Two data sources:

- **TAPR** (Texas Academic Performance Report), 2013 through SY 2024-25, from
  TEA's SAS "broker" CGI at `rptsvr1.tea.texas.gov`.
- **Texas Assessment Research Portal** (`txresearchportal.com`), a separate JSON
  API carrying STAAR / STAAR Alternate 2 / TELPAS with student-group breakdowns
  TAPR does not have.

Neither source offers bulk download or an official API. Both are automated here.

**Year convention, used everywhere:** `ccyy` is the **spring** year, i.e. the end
of the school year. `2025` means school year 2024-25. Every script, filename and
document follows this. Get it wrong and you will be off by one throughout.

---

## 2. Repository layout

| file | role |
|---|---|
| `tapr_download.py` | TAPR downloader. Routes each year to the correct one of TEA's three interfaces. Validates, retries, resumes, writes a manifest. |
| `fetch_dictionaries.py` | Harvests TEA's per-year variable labels into one crosswalk. Required before labels can be compared across years. |
| `prep_stata.py` | Stages downloaded CSVs for Stata: decompresses, cleans names, strips apostrophe guards, detects string columns, emits `label var` do-files. |
| `build_codebook.do` | Imports, appends, builds the cross-time data dictionary and the drift/integrity reports. |
| `portal_download.py` | Assessment Research Portal bulk downloader. Batched, resumable, manifested. |
| `portal_probe.py` | Exploration tool for the same API (endpoint map, organization tree, wizard). Not for bulk downloads. |
| `tapr_scraper_full.py` | **Superseded. Do not use.** The original scraper. It silently downloads empty files. Kept only as a reference for what was wrong. |
| `tapr_example_district.py` | Minimal single-file example. Useful as a connectivity check. |
| `readme.md` | Public-facing documentation for the download tools. Deliberately does not reference this file or the internal build plan; keep it that way. |
| `DEBUGGING.md` | Every defect found, with the evidence. Read before changing a downloader. |
| `PLAN.md` | The longitudinal build design and the Stata workflow. |
| `SOURCES.md` | Other TEA data products, surveyed and link-checked. |
| `PORTAL.md` | Assessment Portal API map. |

Output directories, all gitignored: `tapr_data/`, `portal_data/`,
`dictionaries/`, `stata_stage/`, `stata_build/`, `stata_docs/`.

---

## 3. Prerequisites

- Python 3.9+. `tapr_download.py` needs `requests`; everything else is standard
  library.
- `pdftotext` (from poppler) for `fetch_dictionaries.py`. `brew install poppler`.
- Stata 16+ with the `datadictionary` package for `build_codebook.do`:
  `ssc install datadictionary`. Run `set maxvar 20000` — the do-file does this
  already, and it is required because TAPR STAAR files reach ~4,300 columns.

### Bootstrapping a fresh machine

The repository carries **code only**. Every data directory (`tapr_data/`,
`portal_data/`, `dictionaries/`, `stata_stage/`, `stata_build/`, `stata_docs/`)
is gitignored and must be regenerated locally. The scripts fail with explicit
instructions rather than confusing errors when run out of order, but the
intended sequence is:

```bash
pip install requests
brew install poppler                      # pdftotext, for the label harvest
python3 tapr_download.py --health         # 6 probes; all must pass
python3 portal_download.py --health       # 3 probes
```

Then reproduce the working dataset — see section 6 for the exact commands that
recreate what existed at handoff, or go straight to the full sweep in section 8.
Manifests and checksums are created fresh by the downloads themselves; nothing
about them needs to be carried over.

---

## 4. Domain knowledge you need before changing anything

These are not style preferences. Each one was found by something breaking
silently, and each will break again if removed.

### 4.1 TEA answers HTTP 200 for every failure

Error stubs, throttle pages, empty bodies and HTML all arrive as 200. A status
code proves nothing. `Content-Type` on a successful download is literally
`text/&content_type.-separated-values` — an unresolved SAS macro — so that
proves nothing either. **Always validate structure.**

The specific stub to recognise is a 160-byte body beginning
`<HR><H1>This request completed with errors.</H1>`.

### 4.2 TEA throttles two different ways

It drops the TCP connection *and* returns real `HTTP 429` pages. Both must be
retried. Pacing is roughly 2.5 s between requests; do not lower it.

**This matters for correctness, not just politeness.** A throttled response
looks exactly like "this year has no data". Two early conclusions in this
project were wrong for that reason. Any negative finding about data
availability must be re-run slowly before you believe it.

### 4.3 Three TAPR interfaces, not one

| route | years | dataset codes | header rows |
|---|---|---|---|
| Legacy setpick | 2013-2023 | `setpick`: REF, STAAR1-6, PROF, PERF... (varies by year) | 1 |
| Advanced setpick | SY 2023-24 fully; SY 2024-25 non-assessment only | same `setpick` codes | 1 |
| Wizard | SY 2023-24 onward | `dsname`: 33 codes, REF, STUD, STAAR_ALL... | 2 |

`--route auto` picks correctly. The dataset codes are **not interchangeable**
between routes, so `--datasets` values depend on which route runs.

Two traps in the Advanced route:

- The SAS program is pinned at the 2024 one, `2024/tapr/Advanced Download/getdata_2024.sas`,
  and `ccyy` selects the year. Building the path from the year returns
  "Error reading SAS output".
- TEA links it from **no page at all**. It is undocumented and already
  half-broken (no assessment data for SY 2024-25). Expect it to disappear.
  Anything you need from it, download and keep.

### 4.4 The wizard route requires `var_type`

The step-3 page carries three checkboxes named `var_type` (N numerator,
D denominator, R rate). Omit them and TEA returns **identifier columns only** —
a file that opens fine, parses fine, and contains no data. Measured on 2024
campus `STAAR_ALL`: 4 columns / 523 KB without, 1,100 columns / 20 MB with.

This was the original scraper's headline bug and it is the single easiest way
to silently ruin a run.

### 4.5 Identifier columns are zero-padded strings

`DISTRICT` is 6 characters, `CAMPUS` is 9, `COUNTY` 3, `REGION` 2, all
zero-padded. Read any of them as numeric and `001902` becomes `1902`.

Do **not** rely on a hardcoded list of identifier names. `prep_stata.py`
detects them from content: any column holding a leading-zero numeric is a code.
A name list missed `PAIRCAMP`, which then destrung to numeric in some years and
stayed string in others and made `append` fail outright.

### 4.6 TEA apostrophe-guards ids in 2021-2024

Values are written `'001902` on the setpick routes for those years, clean
otherwise. Strip the apostrophe from **every** column that carries it, not just
the finest-grain id: campus files guard `DISTRICT`, `COUNTY` and `REGION` too.
Stripping only `CAMPUS` produced 35,932 bogus campus/district mismatches.

### 4.7 Find the id column by name, never by position

TEA reordered the identifier block to `COUNTY, REGION, DISTRICT` from 2021.
Code that takes "the first identifier-looking column" picks `REGION` and every
district then looks like a duplicate. Select by priority: CAMPUS, else
DISTRICT, else REGION.

### 4.8 The year is embedded in variable names

`DDA03ARE1019D` (2019) and `DDA03ARE1023D` (2023) are the same measure. The
two-digit year sits immediately before the trailing `N`/`D`/`R`. Stripping it
gives a stable stem.

How much that buys, measured across the downloaded subset
(`stata_docs/stem_rule.csv`):

| dataset | distinct names | stems | names carrying a year | collapse |
|---|---|---|---|---|
| STAAR1 | 19,434 | 2,486 | 99.99% | 87.2% |
| PROF | 818 | 479 | 43.9% | 41.4% |
| REF | 13 | 13 | 0% | 0% |

So the stem rule is decisive for the STAAR files, does about half the job for
profile datasets, and is irrelevant for REF. Budget a hand-maintained crosswalk
for the remainder — for PROF that is roughly 340 genuine add/drop events.

### 4.9 Legacy files carry no labels; the glossary will not help

Data files ship labels only on the wizard route (SY 2024-25). For earlier years
labels come from `/perfreport/tapr/<year>/datadict.pdf`.

They are **not** in `/perfreport/tapr/<year>/glossary.pdf`. That file is prose
about indicators and contains zero variable names — verified by searching the
2019 and 2023 glossaries for `D_RATING`, `DFLCHART`, `DPETALLC`, `DAD_POST`,
`ASVAB_STATUS` and `DPETECOP`: no hits in either. Do not spend time on it.

`fetch_dictionaries.py` uses the PDF for 2013-2023 and TEA's HTML dictionary
endpoint for 2024-25, and merges them, because each carries variables the other
omits — `D_RATING` is in the HTML dictionary and absent from the PDF entirely.

### 4.10 County level is broken on TEA's side

`tapr=all_co` serves a page containing unresolved SAS macros
(`bylev=&bylev.`, empty `sumlev`), so every county download fails. The page
lists all 33 datasets and looks healthy. The legacy route never offered county
at all. County-level TAPR is effectively unavailable.

### 4.11 Assessment Portal: two silent failure modes

Both return HTTP 200 with an empty table and no error.

- `fileImportIds` is required. The wizard walk supplies it. **Build the run body
  from the walked selection object**; assembling it field by field drops that
  key and everything returns nothing.
- `selectedOrganizations` needs a different shape than `/Organization/Query`
  returns: `{organizationId, organizationName, entityExternalId,
  organizationLevelId}`, not `{id, name, ...}`.

Also: batch organizations. Single-org queries are unreliable (Houston needed a
second attempt; Austin alone returns nothing but returns data when paired).
Measured batch ceiling: 150 works, 200+ returns HTTP 500 consistently. Default
is 100.

---

## 5. Running the pipeline

### 5.1 Download TAPR

```bash
python3 tapr_download.py --years 2013-2025 --levels C D
```

Scope with `--datasets`. Runs resume: existing files are skipped. Expect hours
and 10-30 GB gzipped for everything at campus level.

### 5.2 Verify

Two different checks, both worth running.

```bash
python3 tapr_download.py --verify --years 2013-2025 --levels D
```

Re-downloads and checks structure against the live site: rectangularity, that
every offered `key` was submitted, unique ids, blank ids, entity coverage
against that year's REF. Prints a receipts grid of columns per dataset per year
and writes `integrity_report.csv`.

```bash
python3 tapr_download.py --check --output tapr_data
```

Checks files already on disk without downloading: gzip decodes, CSV parses,
rows match header width, SHA-256 matches the manifest.

### 5.3 Harvest labels

```bash
python3 fetch_dictionaries.py --years 2013-2025 --output dictionaries
```

Writes `dictionaries/variable_labels.csv`. Takes roughly 15 minutes and caches
the PDFs, so re-runs are fast.

### 5.4 Stage and build the codebook

```bash
python3 prep_stata.py --input tapr_data --output stata_stage --datasets REF PROF --levels C D
```

```bash
stata-mp -b do build_codebook.do
```

Outputs land in `stata_docs/`:

| file | contents |
|---|---|
| `codebook_<DS>_<LV>.xlsx` | per-wave codebook; the **`Changes` sheet** lists every variable added, dropped, relabelled, retyped or reformatted between consecutive years |
| `codebook_ref_panel.xlsx` | the appended panel documented with `wave(year)` |
| `label_coverage.csv` | share of variables carrying a TEA label, per dataset-year |
| `variable_presence.csv` | first year, last year, gaps |
| `variable_drift.csv` | label, type and format changes |
| `stem_rule.csv` | how much churn the stem rule absorbs, per dataset |
| `build_codebook.log` | full run log |

`stata_build/` holds the per-wave `.dta` files, `ref_panel.dta` and
`varmeta.dta`.

### 5.5 Download from the Assessment Portal

```bash
python3 portal_download.py --list
python3 portal_download.py --estimate --levels D
python3 portal_download.py --levels D --administrations "Spring 2024"
```

Always `--estimate` first. A full crawl is 7 assessments x their reports x up
to 42 administrations, and a campus slice needs ~76 requests and about 7
minutes.

---

## 6. State at handoff

Data directories are **not in the repository** — this section describes the
machine where the work was done, as of 2026-08-07. To recreate the same
working set on a fresh clone (roughly two hours at default pacing):

```bash
python3 tapr_download.py --years 2013-2024 --levels C D --datasets REF STAAR1 PROF
python3 tapr_download.py --route wizard --years 2025 --levels C D --datasets REF STAAR_ALL STUD
python3 tapr_download.py --years 2024 --levels C D
python3 fetch_dictionaries.py --years 2013-2025 --output dictionaries
python3 prep_stata.py --input tapr_data --output stata_stage --datasets REF PROF --levels C D
python3 prep_stata.py --input tapr_data --output stata_stage --datasets STAAR1 --levels D
stata-mp -b do build_codebook.do
```

The third command is deliberate: it re-secures the full SY 2023-24 grab through
the at-risk Advanced route. Verify afterwards with `--verify` (live) and
`--check` (on disk); every number quoted below should reproduce.

**Downloaded (`tapr_data/`, 108 files, 197 MB, 535,554 data rows):**

| dataset | levels | years |
|---|---|---|
| REF | C, D | 2013-2025 |
| PROF, STAAR1 | C, D | 2013-2024 |
| COMP, GRAD, KG, PART1, PART2, PERF1-3, PKEFF, STAAR2-6, STAAR_ADD1 | C, D | 2024 only |
| STAAR_ALL, STUD | C, D | 2025 only |

The single-year 2024 datasets are the full SY 2023-24 grab through the Advanced
route, taken deliberately because that endpoint is at risk.

**Portal (`portal_data/`, 5 slices):** STAAR 3-8 Group Summary for Spring 2023,
Spring 2024 and Spring 2019 at state, district and campus. Proof of concept
only.

**Verification results:**

```
District, 2013-2025, 260 dataset-years : rectangular PASS, key capture PASS
Campus, 2019/2024/2025, 83 dataset-years: all four checks PASS
Files on disk, 108                      : gzip + rectangular PASS
REF panel                               : 130,406 rows x 32 vars
  isid district year (district rows)    : UNIQUE, n = 15,722
  isid campus year   (campus rows)      : UNIQUE, n = 114,684
  substr(campus,1,6) != district        : 0 mismatches
  string <-> numeric type flips         : 0
```

**Cross-time changes detected:** REF 84, PROF 1,652. Of 702 label transitions,
658 are genuine rewordings and 44 are artefacts of missing dictionary coverage.

---

## 7. Known issues and open items

| item | status |
|---|---|
| TEA publishes no REF data dictionary for 2016 or 2017 | Real gap, confirmed with three retries. Those years sit at 10% label coverage. Left as an honest hole rather than inferred. |
| County level (`--levels O`) | Broken on TEA's side. Unusable. |
| Advanced route, SY 2024-25 assessments | Every STAAR/PART dataset errors. `--route auto` sends that year to the wizard. |
| Manifest checksums | **Resolved, and moot on a fresh machine.** The 78 files downloaded before the manifest learned to merge were backfilled with `--check --backfill`; all 108 verified at handoff. Fresh downloads record checksums natively, so a new clone never encounters this. `--check --backfill` remains available if a manifest is ever lost. |
| Portal `changed` column in the `Variables` sheet | Always blank; the change log lives in the separate `Changes` sheet. Not a defect, just where to look. |
| `datadict_addl.pdf` | Does not exist before 2018. Expected, not an error. |

---

## 8. Next steps, in priority order

1. **Finish the TAPR download.** The subset on disk covers three datasets deeply
   and the rest only for 2024. Run the full sweep at both levels. Verify with
   `--verify`, then `--check`.

2. **Extend the label harvest to every dataset.** `fetch_dictionaries.py`
   already pulls all years; the codebook currently documents REF and PROF.
   Adding STAAR1 gives roughly 19,000 labelled variables per year and makes the
   stem-rule crosswalk reviewable.

3. **Build the crosswalk.** Start from `stata_docs/codebook_PROF_D.xlsx`
   (`Changes` sheet) and `variable_presence.csv`. Apply the stem rule first, then
   hand-map the residue. For PROF that is about 340 genuine add/drop events; for
   REF a few dozen.

4. **Melt to long, in Python.** A campus STAAR file is ~9,100 x 4,300. Stata's
   `reshape` on thousands of stubs is impractically slow, and a fully wide
   campus-year file across all datasets would approach the variable ceiling.
   Write long files partitioned by year and level, parse `stem`, measure `year`
   and `var_type` out of the column names there, and hand Stata a long file.
   See `PLAN.md` §2.

5. **Assemble the panel.** Build the campus/district spine from the union of all
   years' REF files. Use an **unbalanced** panel with explicit
   `first_year`/`last_year`: 1,282 districts are ever observed, 1,162 appear in
   all 13 years, and 1 has a genuine gap. A balanced panel would code "did not
   exist" as missing.

6. **Map missing-value codes.** TEA's codes are not consistent across products:
   TAPR uses numeric `-1`, the PEIMS discipline downloads use the string
   `"<10"`. Map to extended missings so the reason survives — `.a` masked,
   `.b` not applicable, `.c` suppressed, `.d` not collected. Never let "not
   collected" pool with "no value".

7. **Add the next source.** `SOURCES.md` has the surveyed inventory. Discipline
   is the best next target: not in TAPR at all, bulk CSV at a stable GET URL,
   campus and district, 2007-08 through 2024-25. Note it has 6 banner lines
   before the header and uses `"<10"` for masking.

8. **Finish the portal downloader run.** The tool works at all three levels.
   Decide the slice you actually need and price it with `--estimate` first.

---

## 9. Maintenance

**Before any large run**, run the health checks. Each one probes the live
endpoints and validates content, so a TEA-side change shows up as a named
failure instead of a mysterious empty download later:

```bash
python3 tapr_download.py --health
python3 portal_download.py --health
```

`--health` on the TAPR side probes all three routes, the wizard step-2 page,
the wizard's year list (new school years appear there first) and the
dictionary endpoint — six probes, ~30 seconds. The portal side is three
requests. Both exit nonzero on any failure, so they can gate an automated run.

As of 2026-08-07 all probes pass: TEA 6/6, portal 3/3.

**What each probe is standing guard over:**

- *advanced setpick* probes: the `2024/tapr/Advanced Download/getdata_2024.sas`
  route is unlinked and undocumented — the most likely thing to vanish. When it
  does, schema continuity for SY 2023-24 goes with it; the data already on disk
  is the fallback.
- *wizard year list*: currently `2024, 2025`. A new value here is the signal to
  run the new-school-year procedure below.
- The `dd` codes in `fetch_dictionaries.py` have their own check, since a
  renamed code does not fail the harvest — it silently returns zero variables:

```bash
python3 fetch_dictionaries.py --verify-dd
```

This diffs the hardcoded table against the live site (~35 requests) and prints
the corrected mapping if anything drifted. Passing 33/33 as of 2026-08-07.

- The portal's batch ceiling (150 works, 200+ returns HTTP 500) is measured,
  not documented by TEA; if bulk requests start failing at 100, re-measure.

**When a new school year is published:**

1. Check the wizard year list at
   `https://rptsvr1.tea.texas.gov/perfreport/tapr/tapr_dd_download.html?year=<latest>`.
2. Run the download for the new `ccyy`. `--route auto` sends anything past
   `ADVANCED_THROUGH` (currently 2024) to the wizard.
3. Run `fetch_dictionaries.py` for the new year.
4. Re-run `prep_stata.py` and `build_codebook.do`. The `Changes` sheet will show
   what moved. Run `fetch_dictionaries.py --verify-dd` first if the harvest
   reports any dataset at zero variables.
5. Read `label_coverage.csv`. A new year at low coverage means TEA has not
   published that year's dictionary yet, not that the labels changed.

**Rules for anyone modifying the downloaders:**

- Never trust an HTTP status. Validate structure.
- Never widen `--pace` downward without testing; throttling corrupts results,
  not just speed.
- Never remove the retry loops. In the portal downloader a fresh query can
  answer 200 with an empty table before its result is available.
- Never hand-assemble a portal run body. Build it from the walked selection.
- Re-run any negative finding slowly before recording it.
- Keep manifests merging, not overwriting. Three separate scripts had this bug;
  it silently discards the history of every earlier run.
