# What TAPR misses: other TEA data for a longitudinal campus/district panel

Surveyed 2026-08-03. School finance / Summary of Finance (SOF) is deliberately
out of scope.

**Verification note.** The TAPR endpoint behaviour in [DEBUGGING.md](DEBUGGING.md)
I verified by hand, request by request. The sources below were verified by
automated agents issuing live `curl` requests on the date above and reporting
observed HTTP status. That is good evidence, not proof — re-check any URL before
you build a downloader on it, and treat the byte counts as indicative.

---

## Priority 1: build these next

### 1. Discipline (PEIMS Chapter 37) — the biggest single gap

Not in TAPR at all, and available as **bulk statewide CSV at a stable GET URL**,
which makes it the easiest high-value addition.

```
https://rptsvr1.tea.texas.gov/cgi/sas/broker
  ?_service=marykay
  &_program=adhoc.download_static_summary.sas
  &district=&agg_level=CAMPUS          # or allDISTRICT
  &referrer=Download_All_Campuses.html
  &test_flag=&_debug=0
  &school_yr=24                        # 08..25
  &report_type=csv
  &Download_All_Campuses_Summaries=Next
```

- **Coverage**: 2007-08 through 2024-25 at campus and district (2005-06 and
  2006-07 return 404 at campus level; region and state go back to 2005-06).
- **Size**: campus 2023-24 is ~155 MB; district ~26 MB.
- **Shape**: long/tidy — one row per campus x `SECTION` x `HEADING NAME` x
  `INDICATOR`. 19 stable `SECTION` values covering ISS, OSS, DAEP
  (discretionary and mandatory), JJAEP, expulsion, incident counts by reason,
  and cumulative enrollment.
- **Keys**: district file has a clean 6-digit `DISTRICT`. The campus file has
  `CAMPUS` (9-digit) but **no clean district column** — `DISTRICT NAME AND
  NUMBER` is a concatenated string like `CAYUGA ISD 001902`. Derive the CDN as
  `substr(CAMPUS,1,6)`, not by parsing the string.
- **Gotcha**: I confirmed by hand that the district file has **exactly 6 banner
  lines**, the header on line 7, and data from line 8, plus trailing footer
  notes. Do not hand this straight to `import delimited` — skip rows
  explicitly, and re-count for the campus file rather than assuming it matches.
- **Masking**: discipline data is masked aggressively, and it uses a **different
  code than TAPR** — the string `"<10"`, not `-1`. Confirmed in the 2023-24
  district file. A naive `destring` turns every masked cell into a plain
  missing and silently loses the distinction between "small" and "unknown".

A wide-format companion, the Discipline Action Group (DAG) summaries, exists at
state/region/district level for 2007-08 through 2023-24 via
`adhoc.download_static_DAG_summary.sas`. **TEA states this product will not be
updated from 2024-25 onward**, so treat it as a closed historical series.

Federal **CRDC** (`civilrightsdata.ed.gov`) is a useful complement for
discipline by disability and race, at 2009-10, 2011-12, 2013-14, 2015-16,
2017-18, 2020-21 and 2021-22. It keys on NCES `LEAID`/`NCESSCH`, not TEA ids,
so it needs a crosswalk (see §5). 2023-24 is not yet released.

### 2. Accountability (A-F ratings, domains, Closing the Gaps)

TAPR carries only the single rating letter (`C_RATING` / `D_RATING`). The
component scores, domain scale scores, distinction designations and Closing the
Gaps tables are a separate download, reachable by **plain GET**:

```
https://rptsvr1.tea.texas.gov/cgi/sas/broker/
  ?_service=marykay&_program=perfrept.perfmast.sas&_debug=0
  &prgopt=reports/acct/dd/dd_get_data.sas
  &ccyy=2025&sumlev=C&dsname=RATE&datafmt=C
  &key=RATE&key=D1&key=D2&key=D3&key=FLAG&key=GRD&key=LAS&key=PAIR&key=PE
```

- `dsname=RATE` — overall rating plus all domain grades and scale scores.
  Verified 2018-2025, campus and district. `ccyy=2017` errors; 2013-2017 use the
  legacy index-system tables.
- `dsname=D3_DATA` — Closing the Gaps data table (646 columns, campus x student
  group, wide). **Returns `CAMPUS` only, no names or district**, so merge back
  to `RATE` or a directory file.
- `dsname=D1_STAAR1/2/3`, `D2_A`, `D2_B` — domain component tables.
- `dsname=DDES` — distinction designations.
- ESSA federal identification lists (Comprehensive / Targeted / Additional
  Targeted Support) are separate files.

Note this uses the *same* broker with a different `prgopt`, so it slots into the
existing downloader as another module rather than a new tool.

### 3. PEIMS ad-hoc reports — enrollment, special populations, staff

These give cross-tabs TAPR does not (grade x race x economic status, disability
category, home language, teacher FTE by course). All are GETs on
`adhoc.std_driver1.sas` or `adhoc.addispatch.sas` returning `application/csv`.

| report | code | levels | years |
|---|---|---|---|
| Student enrollment cross-tabs | `adhoc.addispatch.sas`, `major=st&minor=e` | **campus** available | 2011-12 to 2025-26 |
| Special education by primary disability | `RptClass=SpecEd` | district only | 2012-13 to 2025-26 |
| Emergent bilingual by home language | `RptClass=LepLang` | district only | 2012-13 to 2025-26 |
| Economically disadvantaged | `adhoc` econ report | district | 2011-12 to 2025-26 |
| Teacher FTE by subject/course | `RptClass=FteEnroll` | **district only until 2025-26** | 2012-13 to 2025-26 |
| Staff FTE and salary | `adpeb` | state/region/county/district | 2011-12 to 2025-26 |
| Superintendent salary | `adpea` | district | 2012-13 to 2025-26 |

**The important limitation**: most of these are *district-only*. Enrollment
(`adste`) is the main one with a genuine campus option. If your analysis is
campus-level, check the level availability before promising a variable.

Same 5-10 line banner-preamble quirk as the discipline files.

### 4. Graduation, dropout, attrition, CCMR

TAPR has `GRAD*`, `DROP_ATT` and `COMP4/5/6`, but the standalone accountability
research files carry longer series and more student-group detail:

- Four-year longitudinal graduation/completion/dropout, campus and district,
  XLSX, classes of 2016-2024 at
  `tea.texas.gov/.../campus-data-download-4yr-<YEAR>.xlsx`; earlier classes via
  the SAS broker.
- Five- and six-year extended longitudinal rates (5-year back to class of 2008).
- Annual dropout data download, 2002-03 to 2023-24, by grade span.
- Annual attrition rates (grade 9 cohort), campus and district, 2017-18 to
  2024-25.
- Annual leavers data (19 PEIMS exit/withdrawal reasons), 2019-20 to 2023-24.
- SAT/ACT and AP/IB downloads, campus and district by student group.
- CCMR component data via the accountability download.
- THECB higher-education outcomes (8th-grade cohort, HS graduates enrolled in
  Texas higher ed) — separate agency, region and district level.

The narrative *Secondary School Completion and Dropouts* report itself is PDF;
the data appendices are the machine-readable part.

### 5. Directory, crosswalk and geography — do this early, not late

A 13-year panel needs entity metadata that TAPR does not carry.

- **AskTED download** (`tealprod.tea.state.tx.us/Tea.AskTed.Web/Forms/DownloadFile.aspx`):
  current campus and district directory. ASP.NET WebForms — requires scraping
  `__VIEWSTATE`/`__VIEWSTATEGENERATOR` and POSTing back with a cookie jar. No
  stable GET URL, and it emits the **live** directory, not historical years.
  Note it prepends an apostrophe to every id, same trap as TAPR 2023.
- **TEA ArcGIS Hub annual campus layers** — a much better historical source
  than AskTED, because each year is a separate snapshot with geometry *and*
  `USER_NCES_School_ID` / `USER_NCES_District_ID`. Layers exist for 2009-2013,
  2015, 2017-2018, and 2019-20 through 2024-25. **2014 and 2016 do not exist.**
  Attribute names differ between the 2009-2018 and 2019+ layers.
- **Consolidations, annexations and name changes**, 1983-84 to 2024-25 — the
  authoritative record for entity churn. Reported as not machine-readable, so
  budget manual transcription; this is exactly the file you need to handle
  districts that merge mid-panel.
- **Campus/District Type data** — NCES urban-centric locale codes at campus
  level, XLSX, 2016-17 to 2023-24. Two different URL patterns split at 2020-21;
  data is on the third worksheet.
- **NCES CCD** / Urban Institute Education Data API for federal linkage;
  Census TIGER/Line unified school district boundaries for geography.

### 6. AEIS — extending back to 2002-03

AEIS is the pre-TAPR system and has a **third** download mechanism, a
per-year CGI that is genuinely simple:

```
https://rptsvr1.tea.texas.gov/cgi/perfreport/2011aeis.cgi?level=c&file=stud&suf=.dat
```

- `level` = `c|d|r|s` (no county), `file` = `ref|stud|staf|othr|taks1..15|cad|comp|...`
- Years 2003-2011 (`2003aeis.cgi` .. `2011aeis.cgi`). **`2012aeis.cgi` is 404** —
  there is no bulk file for 2011-12, which leaves a one-year hole between AEIS
  and TAPR.
- `suf=.lyt` returns the record layout for the same file — fetch this first and
  build the column list from it. Two different layout formats (1994-2004 vs
  2005-2011); a parser needs both.
- Campus files carry only `CAMPUS`; join `ref` to get district, county, region.

My recommendation in [PLAN.md](PLAN.md) stands: keep AEIS as a companion panel
rather than appending it. The assessment regime (TAKS/TAAS) and the accountability
system are different enough that a naive append produces a misleading series.

---

## Priority 2: useful, with caveats

- **Snapshot** (district profiles, 1994-95 to 2023-24) — a convenient
  pre-aggregated district summary, form-POST only.
- **TPRS** (Texas Performance Reporting System, 2013-2017) — STAAR results with
  no accountability subsetting, which TAPR does not provide.
- **Texas Assessment Research Portal** (Cambium, `txresearchportal.com`) —
  promoted to Priority 1 after investigation. It is a public JSON API with no
  auth and no CAPTCHA on the query path, its organization tree carries the TEA
  campus and district ids directly, and it offers twelve student-group
  breakdown dimensions including `plan_504`, `gifted`, `titleia_flag` and
  `migrant`, none of which exist in TAPR. It is also where TEA says assessment
  aggregates now live. See **[PORTAL.md](PORTAL.md)** for the full API map and
  scripting plan.
- **Teacher retention / attrition / new hires workbooks** (TEA EDRS) — rich, but
  several are **statewide only**, so check before assuming district detail.
- **PEIMS student transfer reports** — campus-to-campus directed pairs; unusual
  and useful for mobility work.

## Priority 3: not machine-readable

Dashboards and PDF-only products. Do not plan a pipeline around these:
TEA discipline Power BI dashboards, TXschools.gov, the per-entity accountability
report viewer, School Report Cards, AEIS multi-year history reports, Comparable
Improvement reports, statewide item analysis and raw-score conversion tables,
and the IGC/IBC lists.

## A specific warning: STAAR aggregate flat files are gone

The `cfy/dfy/rfy/sfy` STAAR aggregate `.dat`/`.zip` files (FY2018, 2019, 2021,
2022, 2023 — campus, district, region, state, by grade and course) have been
**removed from tea.texas.gov**. The landing page returns 403 and every
individual file 404s. TEA's own archived note says aggregate files "will no
longer be produced" and directs users to the Research Portal.

Copies are reportedly retrievable from the Internet Archive using the raw-replay
prefix (`https://web.archive.org/web/2024id_/<original URL>`). If these files
matter to you, **retrieve them now and keep your own copy** — this is the one
item on the list with real availability risk. Re-verify before relying on it,
crawl gently (the Archive rate-limits), and note FY2020 does not exist.

## Student-level microdata

Student-level STAAR/TELPAS records exist through the Texas Education Research
Center, under an application and data-use agreement. Out of scope for an
automated pipeline, but worth knowing it is the fallback when aggregate masking
makes a question unanswerable.

---

## Suggested module structure

Keep these as separate downloaders sharing one core, rather than bolting them
onto the TAPR script:

```
tea/
  core.py          session, retry, pacing, validation, manifest   (extract from tapr_download.py)
  tapr.py          modern + legacy TAPR                            (done)
  accountability.py  dd_get_data.sas: RATE, D1_*, D2_*, D3_DATA, DDES
  discipline.py    adhoc.download_static_summary.sas
  peims.py         adhoc.std_driver1.sas / addispatch.sas
  gradrate.py      XLSX downloads
  directory.py     AskTED + ArcGIS annual campus layers
  aeis.py          {YYYY}aeis.cgi with .lyt layouts
  portal.py        txresearchportal.com JSON API                   (see PORTAL.md)
```

`portal.py` is the odd one out: it is a JSON API with async job semantics rather
than an HTML form scrape, so it shares the pacing and manifest logic from
`core.py` but none of the parsing.

They share the throttling behaviour, the banner-preamble problem, the
zero-padded-id problem and the masking problem, so the core is worth factoring
out before adding the second source.
