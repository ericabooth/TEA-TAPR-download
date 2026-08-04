# Plan: longitudinal TEA campus + district panel

Goal: one reproducible pipeline that turns TEA's per-year TAPR extracts into a
campus-year and district-year panel spanning 2013-2025 (and optionally back to
2004 via AEIS), with variable labels, provenance and comparability flags
carried through into Stata.

This plan assumes the fixes in [DEBUGGING.md](DEBUGGING.md) are in place.

---

## 1. The problem this has to solve

TAPR files cannot simply be appended. Four things change across years:

1. **The year is embedded in the variable name.** `DDA03ARE1019D` (2019) and
   `DDA03ARE1023D` (2023) are the same measure. Files from different years
   share almost no column names.
2. **Labels embed the year and reword.** `"District 2024 Flag - Charter
   Operator (Y/N)"`.
3. **Variables appear, disappear and move position.** `DAD_POST` is present
   2014-2019 and 2022, absent 2020-2021 and 2023. `DISTRICT` is column 1 in
   most years and column 3 in 2021-2023.
4. **The file format changes at the 2024 boundary — but this one is avoidable.**
   The wizard route serves 2024-2025 with two header rows and different dataset
   codes. The Advanced setpick route serves the same years with the *legacy*
   schema: one header row, full N/D/R, same year-embedded names. Use the
   setpick route (the downloader's default) and 2013-2024 share one format.
   2022 still ships mixed-case names, and 2023 IDs still carry a leading
   apostrophe, as do 2024-2025 IDs on the Advanced route.

The good news: (1) is fully mechanical, and it is the bulk of the problem.

### The stem rule

The two-digit year sits immediately before the trailing `N`/`D`/`R` suffix.
Removing it yields a stem that is stable across years:

```
DDA03ARE1019D  ->  DDA03ARE10@D
DDA03ARE1023D  ->  DDA03ARE10@D
```

Verified: **1,890 of 1,892** stems in the 2023 district STAAR1 file match 2019
stems exactly. This one rule harmonises the large majority of TAPR variables
without any hand-maintained mapping.

Two things to watch:

- **Legacy files contain prior-year columns.** The 2019 STAAR1 file has 4,285
  columns: roughly half tagged `19` and half tagged `18`. The year in the name
  is the year the *measure* refers to, not the year of the file. Parse it and
  keep it — do not assume file-year equals measure-year.
- The handful of stems that do not parse are identifiers (`DISTRICT`,
  `CAMPUS`, ...) and a small residue that goes in the manual crosswalk.

### The rest

Everything the stem rule does not cover goes into a small, hand-maintained
crosswalk keyed on `varname x year`. Based on the district `REF` audit, this is
on the order of tens of entries, not thousands — things like `SECS` (2013
only), `OC` becoming `OG` in 2021, `PERF` splitting into `PERF1/2/3`.

---

## 2. Architecture: where Python ends and Stata begins

The split is by what each tool is genuinely better at, not by preference.

| Stage | Tool | Rationale |
|---|---|---|
| 1. Download, retry, throttle, validate, checksum | **Python** | HTTP, form parsing, backoff. Already built. |
| 2. Variable inventory (`--audit-years`) | **Python** | Reads headers only; no need to load data. |
| 3. Wide -> long melt | **Python** | See below. This is the one place I would *not* use Stata. |
| 4. Stem parsing + crosswalk application | **Python** or Stata | Mechanical; do it wherever the melt happens. |
| 5. Import, label, value-label, notes, chars | **Stata** | Native and unmatched. |
| 6. Append years, merge datasets, build panel | **Stata** | `merge`/`append`/`xtset`, plus your existing workflow. |
| 7. Documentation and drift reporting | **Stata** | `datadictionary`, `describe, replace`, `labelbook`. |
| 8. Analysis-ready extracts | **Stata** | Downstream work already lives there. |

**On stage 3.** A single campus STAAR file is ~9,100 rows x 4,300 columns.
Stata's `reshape long` on thousands of stubs is very slow, and a fully-wide
campus-year file across all 33 datasets would approach or exceed the 32,767
variable ceiling in Stata/SE. Melting in Python (pandas or polars) and handing
Stata a long file avoids both problems. If you would rather keep everything in
Stata, `frames` plus a `foreach` over datasets is workable, but expect the
build to take hours rather than minutes.

**Everything after stage 3 is Stata.** Labels, notes, characteristics, value
labels and the panel logic are exactly what Stata is for, and keeping them
there means the metadata survives into every downstream `.dta`.

---

## 3. Target data model

Three artefacts per level (campus, district):

**`campus_year_long.dta`** — the firehose.

| variable | type | notes |
|---|---|---|
| `campus` | str9 | zero-padded, apostrophe stripped |
| `district` | str6 | |
| `year` | int | measure year, parsed from the varname, not the file |
| `file_year` | int | year of the source file (provenance) |
| `stem` | str32 | year-stripped variable stem |
| `var_type` | str1 | N / D / R |
| `value` | double | |
| `miss_reason` | byte | encoded reason when value is missing |

**`campus_year_core.dta`** — one row per campus-year, a curated ~50-150
variables (enrollment, demographics, rating, headline STAAR rates). This is
what most analysis actually uses. Built from the long file by a documented
selection script, not by hand.

**`varmeta.dta`** — one row per `stem x year`, holding TEA's label text for
that year, the decoded attributes (level, student group, grade, subject,
performance standard), the source dataset, and a `comparable` flag.

Keeping the firehose long and the analytic file wide is the compromise that
avoids both the variable ceiling and the pain of querying a long file for
routine work.

---

## 4. Handling label and format drift in Stata

This is the part you flagged, and it deserves first-class treatment rather than
a cleanup step.

### 4.1 Capture what TEA actually said, every year

`tapr_download.py --audit-years 2013-2025` writes `variable_inventory.csv`:

```
year, level, dataset, endpoint, position, varname, varname_lower, tea_label
```

For 2024-2025 `tea_label` is TEA's own row-1 label. For 2013-2023 the files
carry no labels at all, so labels have to come from TEA's per-year glossary
(`/perfreport/tapr/<year>/glossary.pdf` or `glossary.html`) and from the
data-dictionary pages the downloader saves for modern years. Budget real time
for this: **legacy years ship no human-readable labels in the data**.

### 4.2 Snapshot metadata per year and diff it

Build one `.dta` per year, then use Stata's own metadata as the diff surface:

```stata
* machine-readable metadata snapshot for one year
use "build/district_`yr'.dta", clear
describe, replace     // -> one row per variable: position, name, type, format, varlab, vallab
gen int year = `yr'
save "meta/desc_`yr'.dta", replace
```

`describe, replace` turns the variable metadata into a dataset, which is what
makes systematic diffing possible. Append the snapshots and the drift report
falls out:

```stata
clear
forvalues yr = 2013/2025 {
    append using "meta/desc_`yr'.dta"
}
* label text that changed for a variable that persisted
bysort name (year): gen byte labchange = varlab != varlab[_n-1] & _n > 1
* type or format changes
bysort name (year): gen byte typechange = type != type[_n-1] & _n > 1
* first and last year each variable appears
bysort name (year): egen first_yr = min(year)
bysort name (year): egen last_yr  = max(year)
```

### 4.3 Human-readable documentation with `datadictionary`

Use `datadictionary` to produce the readable codebook for each year's build and
for the final panel. It is a user-written command, so confirm the exact syntax
with `help datadictionary` after installing — the option names have varied
between versions:

```stata
ssc install datadictionary        // if not already present
use "build/district_2025.dta", clear
datadictionary using "docs/dd_district_2025.xlsx", replace
```

Run it per year and keep the outputs under version control, so a diff of two
years' dictionaries is a reviewable artefact rather than something regenerated
ad hoc.

Two complements worth pairing with it:

- `labelbook` catches **value-label** drift, which `describe, replace` will not.
  This matters here: fields like `OUTCOME` ("Meets Requirements" / "Needs
  Assistance") and `D_RATING` are categorical text whose permitted values shift
  across years.
- `codebook, compact` gives ranges and missing counts, which is the fastest way
  to spot a year where a rate variable silently changed scale (proportion vs
  percent) or where `-1` was left unconverted.

### 4.4 Carry provenance on the variable itself

Stata characteristics are the right place for per-variable provenance, because
they survive `save`, `merge` and `append`:

```stata
foreach v of varlist _all {
    char `v'[tea_stem]      "`=stem[`v']'"
    char `v'[tea_name_`yr'] "`=origname[`v']'"
    char `v'[tea_label_`yr'] "`=tealabel[`v']'"
    char `v'[source_file]   "`file'"
}
notes _dta: built `c(current_date)' from `file' (sha256 `hash')
```

Then a variable in the final panel can answer, on its own, "what was this
called in 2016, and what did TEA call it then?"

### 4.5 Missing-value semantics

TEA overloads missingness. Map it to extended missing values so the reason
survives:

| source | meaning | Stata |
|---|---|---|
| `-1` (TAPR) | masked for small counts / privacy | `.a` |
| `"<10"` (discipline files) | masked, count below threshold | `.a` |
| blank or single space | not applicable that year | `.b` |
| `*` | suppressed, other reason | `.c` |
| `.` | genuinely missing | `.` |
| variable absent that year | not collected | `.d` |

The masking codes are **not consistent across TEA products**: TAPR uses numeric
`-1`, the PEIMS discipline downloads use the string `"<10"`. A `destring` that
does not handle `"<10"` explicitly will convert every masked discipline cell to
plain missing, erasing the distinction between "suppressed because small" and
"genuinely unknown". Check the code for each source rather than assuming.

Encoding "not collected" as `.d` rather than `.` matters: it is the difference
between "this campus had no value" and "Texas did not measure this yet", and
the two should never be pooled in a trend.

---

## 5. Known comparability breaks to flag, not silently paper over

Carry these as a documented `comparable` flag on `varmeta`, and as a
`_dta` note on every panel file:

- **2020**: STAAR cancelled (COVID). No assessment results; accountability
  ratings not issued.
- **2021**: partial testing, heavily reduced participation. TEA added
  COVID-specific datasets (`ACCLER`, `STAARV`, `PARTV`, `PKEFF`). Treat
  2021 rates as not comparable to either side.
- **2022**: mixed-case variable names in the source files.
- **2023**: STAAR redesign — new item types and a rescaled test. Cross-year
  comparisons of STAAR performance through 2023 are not valid without
  explicit caveat. Also: no accountability ratings in `REF`, and the leading
  apostrophe on IDs.
- **2024-2025**: route change. On the Advanced setpick route the schema stays
  continuous with 2013-2023; on the wizard route you get two-row headers,
  different dataset codes and `REGNNAME`. 2025 STAAR is not served by the
  Advanced route, so STAAR for 2025 has to come from the wizard — the one place
  the panel genuinely has to switch routes mid-series. Flag it.
- **2013-2014**: TAKS still being phased out (`TAKS1`, `TAKS2` datasets).
- **Student-group definitions** shift over the period, notably the
  emergent-bilingual / English-learner terminology change and revisions to
  economically-disadvantaged and at-risk definitions. These change the
  denominator, not just the label.

---

## 6. Entity churn across a 13-year panel

Campuses and districts open, close, consolidate and get renumbered. A panel
keyed on `campus` alone will silently drop and resurrect units.

Plan:

1. Build a `campus_spine.dta` from the union of all years' `REF` files: one row
   per `campus x year` observed, plus name, district, grade span, charter flag.
2. Flag entities whose `district` changes across years (annexation,
   consolidation) and whose name changes materially.
3. Cross-check against AskTED (TEA's school directory) for open/close dates,
   and against the NCES Common Core of Data crosswalk if federal linkage is
   ever wanted.
4. Decide explicitly, and document, whether the panel is balanced (spine x all
   years, with `.d` for not-yet-existing) or unbalanced. I would recommend
   unbalanced with an explicit `first_year`/`last_year` on the spine, because a
   balanced panel invites accidental treatment of "did not exist" as zero.

---

## 7. Build phases

**Phase 1 — acquisition (done, needs a full run).**
Run `tapr_download.py --years 2013-2025 --levels C D` and let it complete.
Expect on the order of 10-30 GB gzipped at campus level across all datasets and
years; the 2024 campus `STAAR_ALL` file alone is 6.5 MB gzipped / 20 MB raw.
Review `manifest.csv` for any non-`ok` rows before proceeding.

**Phase 2 — inventory and crosswalk.**
`--audit-years 2013-2025` for every level and dataset. Apply the stem rule.
Everything that fails to parse goes into `crosswalk.csv` by hand. This is the
one genuinely manual step and it should be reviewed by someone who knows the
subject matter.

**Phase 3 — melt to long.**
Python, one file at a time, writing partitioned long files by year and level.
Parse `stem`, measure `year`, `var_type` out of each column name here.

**Phase 4 — Stata ingest and labelling.**
Import long files, apply `varmeta` labels, set characteristics, map missing
codes, `datadictionary` per year.

**Phase 5 — panel assembly.**
Build the spine, append years, `xtset campus year`, run duplicate and
continuity checks, produce `campus_year_core.dta`.

**Phase 6 — extend backwards (optional).**
AEIS covers 2003-04 through 2011-12 at
`https://rptsvr1.tea.texas.gov/perfreport/aeis/<year>/`. It uses a *third*
download mechanism (`xplore/DownloadSelData.html`), and its variable naming and
assessment regime (TAKS, TAAS) differ enough that I would treat pre-2013 as a
separate companion panel rather than appending it to the TAPR panel. Decide
this deliberately.

---

## 8. What is not in TAPR

See [SOURCES.md](SOURCES.md) for the surveyed inventory of other TEA data
products, which are machine-readable, and which need a separate downloader.
The short version: discipline data, PEIMS ad-hoc enrollment and staff
cross-tabs, accountability data tables, and the graduation/dropout series all
live behind different interfaces and belong in separate modules of the same
pipeline — not in the TAPR downloader.

School finance / SOF data is deliberately out of scope.
