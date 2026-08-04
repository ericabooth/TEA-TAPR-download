# TAPR scraper: debugging and link-check report

Date of checks: 2026-08-03. Every claim below was verified against the live
TEA server, not inferred from the markup.

## Summary

`tapr_scraper_full.py` runs to completion and prints "Downloaded:" for every
file, but it is not producing usable data. Two defects account for almost all
of it:

1. It never submits the `var_type` field, so TEA returns files containing
   identifier columns and nothing else.
2. It points every year at an endpoint that only serves 2024 and 2025, so
   2013-2023 return an error stub that the script writes to disk as a `.csv`.

Both are silent. The script's own success counter reports 100% success.

`tapr_download.py` (new) fixes these and covers 2013-2025.

---

## The site still works

The TEA SAS broker at `https://rptsvr1.tea.texas.gov/cgi/sas/broker` is live and
the multi-step flow the original script models is still the right shape for
recent years. `tapr_example_district.py` runs successfully today and produces a
valid 135 KB file. The failures below are not "the site changed and broke the
scraper" — most of them were latent from the start.

---

## Defect 1: `var_type` is never submitted (critical, silent)

The step-3 page carries three checkboxes the original script does not look for:

```html
<input type="checkbox" class="var_type" name="var_type" id="var_type1" value="N" checked>
<input type="checkbox" class="var_type" name="var_type" id="var_type2" value="D" checked>
<input type="checkbox" class="var_type" name="var_type" id="var_type3" value="R" checked>
```

N = numerator, D = denominator, R = rate. All three are checked by default in
the browser. Submitting the form without them is not equivalent to submitting
them — TEA returns only the identifier columns.

Measured on 2024 Campus `STAAR_ALL`:

| request | bytes | columns | content |
|---|---|---|---|
| without `var_type` (what the script sends) | 522,795 | 4 | `CAMPUS, CAMPNAME, DISTRICT, DISTNAME` — no data |
| with `var_type=N,D,R` | 20,099,454 | 1,100 | full extract |

Only `REF` and a few other reference datasets have no `var_type` control, which
is why the example script appears to work: it downloads `REF`.

This matches the two options TEA describes on its own download pages — "TAPR
Data in Excel (Rates Only)" versus "Advanced TAPR Data (Numerators,
Denominators & Rates)". Without `var_type` you are asking for neither.

## Defect 2: 2013-2023 are served by a different application

TEA runs **three** TAPR download routes, not one:

| route | years | entry | level field | dataset field | header rows |
|---|---|---|---|---|---|
| **Wizard** | 2024- | `prgopt=reports/tapr/dd/dd_tapr.sas`, 3-step POST | `tapr` = `all_c`/`all_d`/`all_r`/`all_co`/`all_s` | `dsname`, 33 fixed codes | **2** (labels, then varnames) |
| **Legacy setpick** | 2013-2023 | `prgopt=<YYYY>/tapr/tapr_download.sas`, single request | `sumlev` = `C`/`D`/`R`/`S` | `setpick`, year-specific codes | **1** (varnames only) |
| **Advanced setpick** | 2024-2025 | `prgopt=2024/tapr/Advanced Download/getdata_2024.sas` + `ccyy`, single request | `sumlev` | `setpick` | **1** |

The third route matters more than it looks, and I missed it on the first pass.
It carries the **legacy schema forward into 2024-2025**: one header row, full
numerator/denominator/rate coverage, and the same year-embedded variable names
(`DDA03ARE1024D`). Verified on 2024 district `STAAR1`: 1,892 columns, 270 `D` /
810 `N` / 810 `R`.

That means a 2013-2024 panel can be built from **one** consistent format
instead of breaking at the 2024 boundary. `tapr_download.py` now uses the
setpick route for every year by default; `--route wizard` selects the other one
when you want the 2024+ categories it alone offers (`STUD`, `STAF`,
`STAAR_ALL`, `DROP_ATT`, `PK`, ...).

Three caveats on the Advanced route, all verified:

- **The SAS program is pinned at the 2024 one.** `ccyy` selects the year.
  Building the path from the year — `2025/tapr/Advanced Download/getdata_2025.sas` —
  returns `Error reading SAS output`. This cost me a wrong conclusion before I
  tested the combinations.
- **TEA links it from nowhere.** `/perfreport/tapr/2024/download/DownloadData.html`
  and every variant I tried are 404, and the year index pages point only at the
  wizard. It is undocumented, so confirm it still answers before a long run.
- **2025 is partial.** `STAAR1` returns the SAS error stub for `ccyy=2025`
  while `REF`, `PROF`, `KG`, `GRAD`, `COMP` and `PERF1` all return data.
- **IDs carry a leading apostrophe on this route** (`'001902`, `'07`, `'001`),
  for 2024 and 2025, where the wizard route returns them clean. Same trap as
  legacy 2023; strip it on import.

The original script uses the modern endpoint for all years. For 2013-2023 the
broker replies with HTTP 200 and a 160-byte body:

```
<HR><H1>This request completed with errors.</H1> Set _DEBUG=131 and resubmit ...
```

The script's bare `except` swallows this, and `total_downloaded += 1` runs
regardless of outcome, so the run reports success.

The legacy endpoint is a **single POST** — no wizard — and returns CSV directly:

```
POST /cgi/sas/broker
  _service=marykay  _program=perfrept.perfmast.sas  _debug=0
  prgopt=2019/tapr/tapr_download.sas  year4=2019  year2=19
  topic=acct  title=Data Download
  sumlev=D  setpick=REF
```

Verified working for every year 2013-2023 at district level.

Note the legacy endpoint has **no county level**. County (`all_co`) exists only
from 2024.

## Defect 3: "dynamic discovery" does not discover anything

The README claims the script adapts to whatever TEA offers for a given year.
It does not. The modern step-2 page is **byte-identical (60,685 bytes) for
every value of `ccyy`** — 1998, 2024 and 2099 all return the same 33 categories.
Year validity cannot be established from the menu; only an attempted download
settles it.

The legacy pages, by contrast, genuinely do vary by year, and the new script
reads them per year:

| year | `setpick` options |
|---|---|
| 2013 | 13, incl. `TAKS1`,`TAKS2` |
| 2014 | 12, `TAKS*` still present |
| 2015-2017 | 12, `COMP` appears, `TAKS*` gone |
| 2018 | 19, `GRAD`,`KG`,`OC`,`STAAR_ADD1-4` appear |
| 2019-2020 | 20, `STAAR_ADD5` appears |
| 2021-2022 | 27, COVID-era `STAARV`,`ACCLER`,`PARTV`,`PART1A`,`PERF1/2/3`,`PKEFF`,`OG` |
| 2023 | 24, `STAARV`/`ACCLER`/`PARTV` dropped |

## Defect 4: brittle regex HTML parsing

TEA's markup is not internally consistent. On step 2:

```html
<input type="hidden" name="_service" value="marykay">
```

On step 3, the same field:

```html
<input type='hidden' name='_service' value=marykay>
```

The quoted-value regex misses the second form; the script only survives because
a second unquoted regex happens to catch it. Attribute order also varies. The
rewrite uses `html.parser`, which is agnostic to both.

Related: `key` checkbox values contain pipes (`REGION|REGNNAME`), so the count
of `key` values is not the column count. Eight keys produced twelve columns in
the district REF file.

TEA also closes its `<label>` elements with another **opening** tag:

```html
<label for='dd1'>District Reference<label>
```

The step-2 page has 65 opening `<label>` tags and **zero** closing ones. A
parser that waits for `</label>` collects nothing, which is why the dataset
labels came back empty in the first pass of the rewrite. The parser now flushes
on the next opening tag and at end of document, and recovers all 33 labels
("District Reference", "Attendance, Chronic Absenteeism, and Annual Dropout",
and so on). The original script's regex sidestepped this by accident, since it
matched text up to the next `<`.

## Defect 5: nothing validates the response

TEA answers **every** failure mode with HTTP 200 — SAS error stubs, empty
bodies, HTML pages. The original writes whatever comes back to a `.csv`. There
is also no check that the content is CSV: `Content-Type` on a successful
download is literally

```
text/&content_type.-separated-values
```

an unresolved SAS macro variable, so it cannot be used to detect success.

## Defect 6: TEA throttles, and the script has no retry

TEA throttles in **two** different ways, and a scraper has to survive both:

- It drops the TCP connection (`RemoteDisconnected`). Roughly 75 rapid requests
  triggered this during testing, along with a run of apparently-empty category
  pages — exactly what a naive scrape would silently record as "no data for
  these years".
- It returns a real `HTTP 429 Too Many Requests` HTML page. Observed on the
  discipline endpoint after a burst.

With ~2.5 s pacing and exponential-backoff retry, the same requests succeed.
The new script retries on both. Note that the 429 arrives as an HTML body, so
a scraper that only checks the status code of the *final* response — or that
writes whatever it receives — will store an error page as data.

Any conclusion about missing data drawn from an unpaced run should be
distrusted.

## Defect 7: bookkeeping

- `total_downloaded += 1` increments even when the download raised.
- Existing files are re-downloaded on every run; there is no resume.
- `time.sleep(1)` is skipped when `category_limit` breaks the loop.
- Filenames come from a greedy `filename=(.+)` match, then have characters
  stripped, so distinct datasets can collide.

---

## Defect on TEA's side: the county level is broken

`tapr=all_co` (All Counties) on the modern endpoint serves a page containing
**unresolved SAS macro variables**:

```
<input type='hidden' name='bylev'  value="&bylev.">
<input type='hidden' name='sumlev' value="">
```

`bylev` is literally the macro reference `&bylev.` and `sumlev` is empty, so
every download launched from that page returns the broker error stub. The page
lists all 33 datasets and looks perfectly healthy, which makes this an easy
trap. County-level TAPR is effectively unavailable, and the legacy endpoint
never offered county at all (`sumlev` is only C/D/R/S).

The new script detects this and reports it once per year rather than emitting
33 identical failures.

## Level availability, verified

| level | 2013-2023 (legacy) | 2024-2025 (modern) |
|---|---|---|
| Campus | yes | yes |
| District | yes | yes |
| Region | yes | yes |
| State | yes, but no `REF` dataset | yes, 32 datasets (no `REF`) |
| County | **not offered** | **broken (see above)** |

Two related traps at region and state level, both of which the first version of
my rewrite got wrong:

- Region-level `REF` legitimately has **no selectable data elements and no
  `var_type` controls** — it is just `REGION, REGNNAME`. Bailing out when the
  key list is empty loses a valid file, and flagging identifier-only output as
  corrupt gives a false positive. The identifier-only check is only meaningful
  when the dataset actually offered `var_type` controls.
- State level genuinely has no `REF` dataset (32 categories rather than 33),
  so a `REF` request there is expected to fail, not a bug.

## Broken link on TEA's site

`https://rptsvr1.tea.texas.gov/perfreport/tapr/<year>/download/DownloadData2.html`
("STAAR Performance Results (Rates Only)") returns **404** for 2018-2023. This
is TEA's own broken link, not a scraper bug. The data is reachable through the
`setpick=STAAR*` options on `DownloadData.html`.

Beware the link text on those pages: `DownloadData.html` is labelled
**"Advanced TAPR Data (Numerators, Denominators & Rates)"** and
`xplore/DownloadSelData.html` is the rates-only Excel option — the opposite of
what the filenames suggest.

## Link check results

| URL | status |
|---|---|
| `/cgi/sas/broker` (modern wizard, 2024-25) | 200, working |
| `/cgi/sas/broker` (legacy, `prgopt=<YYYY>/tapr/tapr_download.sas`, 2013-23) | 200, working |
| `/perfreport/tapr/<2013..2025>/index.html` | 200 |
| `/perfreport/tapr/<2013..2023>/download/DownloadData.html` | 200 |
| `/perfreport/tapr/<2018..2023>/download/DownloadData2.html` | **404** |
| `/perfreport/tapr/<2004..2012>/download/DownloadData.html` | 404 (AEIS era) |
| `/perfreport/tapr/` | 200, meta-refresh to tea.texas.gov |
| `/perfreport/aeis/index.html` | 200, links years 2000-2012 |
| `/perfreport/tapr/tapr_dd_download.html?year=2025` | 200; `ccyy` dropdown offers **only 2024, 2025** |
| data dictionary `prgopt=reports/tapr/dd/dd_tapr_dictionary.sas` | 200, plain GET, modern years |

---

## Structural findings that matter downstream

These are not bugs, but they determine how the panel has to be built.

**The year is embedded in every variable name.** Legacy STAAR variables are
named like `DDA03ARE1019D` (2019) and `DDA03ARE1023D` (2023) — same measure,
different name, because positions 10-11 carry the two-digit year. Files from
different years therefore share almost no column names and cannot simply be
appended.

**Labels embed the year too**, on the modern endpoint:
`"District 2024 Flag - Charter Operator (Y/N)"`.

**Variables come and go, and columns move.** District `REF`, 2013-2025:

```
variable       13 14 15 16 17 18 19 20 21 22 23 24 25
district       x  x  x  x  x  x  x  x  x  x  x  x  x
d_rating       x  x  x  x  x  x  x  x  x  x  .  x  x
outcome        .  x  x  x  x  x  x  x  x  x  .  x  x
dad_post       .  x  x  x  x  x  x  .  .  x  .  x  x
asvab_status   .  .  .  .  .  x  x  x  x  x  x  x  x
regnname       .  .  .  .  .  .  .  .  .  .  .  x  x
secs           x  .  .  .  .  .  .  .  .  .  .  .  .
```

Column *position* also moves — `DISTRICT` sits at position 1 in most years but
position 3 in 2021-2023. Never rely on column order.

**2022 shipped mixed-case variable names** (`D_rating`, `asvab_status`) where
every other year used uppercase. Normalise case on import.

**2023 IDs carry a leading apostrophe**: `'001902` instead of `001902`. This
year only. An unstripped apostrophe silently breaks every merge involving 2023.

**`-1` is the suppression/masking code** in the legacy numerator and denominator
columns, not a real value.

**2023 has no accountability ratings** in `REF` (`D_RATING`, `OUTCOME` and
`DAD_POST` are all absent), consistent with the litigation over that year's
release.

---

## What the new script does

`tapr_download.py` replaces `tapr_scraper_full.py`:

- routes each year to the correct endpoint automatically
- submits `var_type`
- reads legacy `setpick`/`sumlev` options per year rather than assuming them
- parses with `html.parser`
- validates every response (SAS error stub, HTML, empty, header-only,
  identifier-only) and refuses to save a bad file
- retries with exponential backoff and paces requests
- skips files already downloaded, so runs resume
- gzips output
- saves TEA's data dictionary for modern years
- writes `manifest.csv` / `manifest.json` with status, dimensions and SHA-256
- `--audit-years` writes `variable_inventory.csv`: one row per
  year x level x dataset x variable, with TEA's label text — the input to the
  label-drift crosswalk

Verified end to end:

```
2013 D REF   legacy   1,228 x  9
2019 D REF   legacy   1,201 x 11
2023 D REF   legacy   1,209 x  8
2024 D REF   modern   1,207 x 12
2025 D REF   modern   1,208 x 12
2024 C REF   modern   9,082 x 22
2024 C STAAR_ALL      9,082 x 914     6.5 MB gzipped
```
