# Texas Assessment Research Portal (txresearchportal.com): scripting plan

**Short answer: yes, this is scriptable, and it is a better assessment source
than anything else surveyed.** It is a public JSON API with no authentication,
no API key, and no CAPTCHA on the query path. I mapped the whole surface and
verified it live on 2026-08-03.

**Status: working at statewide, district and campus level, with CSV export.**
§6 records the two non-obvious things that were blocking it.

---

## 1. Why it is worth the effort

The portal exposes cross-tabs that TAPR simply does not have. TAPR gives you a
fixed set of student groups; the portal lets you break results down by twelve
dimensions, several of which appear nowhere in TAPR:

```
sexcode          race             econdisadv_flag    titleia_flag
migrant          lep              bilingual_flag     esl_flag
sped             plan_504         gifted             atrisk
```

`plan_504`, `gifted`, `titleia_flag` and `migrant` are the standouts. It also
covers assessments TAPR aggregates only partially: STAAR Alternate 2 (3-8 and
EOC), TELPAS, and TELPAS Alternate.

It is also where TEA has said assessment aggregates now live, after the
`cfy/dfy/rfy/sfy` flat files were withdrawn (see [SOURCES.md](SOURCES.md)).

---

## 2. API surface

Base URL, from the app bundle:

```
https://api-public.prod.publicfolio.cambiumreports.com/v1/
```

Texas client, verified via `GET /Client/{id}`:

```json
{"id":"c21a285e-d0fa-4c1a-9070-32b0d50178e1","name":"Texas","aggMin":5}
```

`aggMin: 5` is the masking threshold. Cells below five students are suppressed.

Complete endpoint list, extracted from the bundle:

| method | path | purpose |
|---|---|---|
| GET | `/Client/{clientId}` | client metadata, masking threshold |
| GET | `/ClientSettings/{clientId}` | user-guide paths |
| GET | `/QuerySelection/{clientId}` | initial wizard state |
| POST | `/QuerySelection` | advance the wizard one step |
| GET | `/Organization/{id}` | single organization |
| GET | `/Organization/paginated/{clientId}` | **returns 0 records; use the POST below** |
| POST | `/Organization/Query` | organization search and listing |
| POST | `/Query/Run` | execute; 202 + `queryHash` when queued, 200 with data when ready |
| POST | `/Query/Status` | poll execution (`queryHash` as a **query param**, not body) |
| POST | `/Query/Poll` | batch poll |
| POST | `/Query/Download` | CSV export (returns `text/csv`) |
| POST | `/Query/Modify` | modify an existing query |
| POST | `/Query/Offline` | large export, delivered by email |
| POST | `/Query/Verify`, `/Query/ResendCode` | email verification for the offline path |
| POST | `/Query/PDFExport`, `/Query/CheckPDFDownloadStatus` | PDF |
| GET | `/Query/Ready/{hash}`, `/Query/Download/{hash}` | retrieve by hash |
| GET | `/StaticText/...` | UI strings |

No `Authorization` header is required on any of these. The app sends
`withCredentials: true` and a random `User` UUID cookie, but the endpoints
answer without it.

## 3. The organization tree is the join key, and it is clean

`POST /Organization/Query` with a `level` array. Note the plain paginated GET
returns zero records regardless of parameters; only the POST works.

```json
{"clientId":"c21a285e-d0fa-4c1a-9070-32b0d50178e1",
 "level":[2], "pageNumber":1, "pageSize":200,
 "searchText":"AUSTIN ISD"}
```

Verified counts and levels:

| level | name | count |
|---|---|---|
| 1 | REGION | 20 |
| 2 | DISTRICT | 1,314 |
| 3 | INSTITUTION (campus) | 11,363 |

**`entityExternalId` is the TEA identifier**, which makes this a 1:1 join to
TAPR with no crosswalk:

```
district  id=120484  entityExternalId=227901     AUSTIN ISD
campus    id=122690  entityExternalId=057909041  AUSTIN ACAD FOR EXCELL
                     parent=GARLAND ISD (057909)
```

Campus `entityExternalId` is the 9-digit campus id; district is the 6-digit CDN;
the parent chain gives you region. `id` (an Int64) is the portal's internal key
and is what `organizationIds` expects.

## 4. The selection wizard

`GET /QuerySelection/{clientId}` returns step 1. POST the whole object back with
one value flipped to `"selected": true` and it returns the object with the next
step appended. Repeat until no unselected step remains.

Steps and option counts for STAAR 3-8:

| step | n | values |
|---|---|---|
| assessment | 7 | STAAR 3-8, STAAR Alternate 2 3-8, STAAR Alternate 2 EOC, STAAR Cumulative, STAAR EOC, TELPAS, TELPAS Alternate |
| report | 6 | Standard Summary, Standard Combined Summary, Standard Constructed Response Summary, Group Summary: Performance Levels & Reporting Categories, Item Analysis Summary, Score Codes Summary |
| administrations | 36 | Spring 2026 back through Spring 2012, plus off-cycle March/April/May/June administrations |
| subjects | 4 | Mathematics, Reading, Science, Social Studies |
| versions | 3 | STAAR, STAAR Spanish, STAAR All |
| grades | 6 | Grade 3 through Grade 8 |

Two things that cost me time and will cost you the same:

- **Option IDs are context-specific.** Editing `administrations` in a saved
  object without re-POSTing invalidates the downstream `subjects`/`versions`/
  `grades` IDs. Always walk the wizard sequentially and re-POST after every
  pick.
- **36 administrations is not 36 school years.** Several years have multiple
  administrations (June 2019, Spring 2019, May 2019, April 2019). Map them to
  school years deliberately rather than assuming one per year.

## 5. Query execution flow

```
POST /Query/Run       -> 202 + request.queryHash   (queued)
                      -> 200 + tables[]            (cached/ready)
POST /Query/Status?queryHash=<hash>   -> {"status": "Finished" | "Errored" | ...}
POST /Query/Run       (again, same body)           -> 200 + tables[]
POST /Query/Download  (same body)                  -> text/csv
```

Required body fields beyond the walked selection object, established by
trial:

```json
{ ...walkedQuerySelection,
  "organizationIds": [120484],            // Int64, NOT the GUID
  "selectedOrganizations": [ {...full org object from /Organization/Query...} ],
  "stateFlag": false,                     // true for statewide, and then no orgs
  "queryProcessEngine": "ReportAsync",
  "drillDownStack": [], "dynamicFilter": [], "dynamicBreakdown": [],
  "bypassDefaultColumnVisibility": false, "bypassStateSelection": false }
```

Errors are informative if you read them properly:

- `"Malformed Request Model"` (400) means a required top-level field is absent.
  `queryProcessEngine` and `stateFlag` were the two that mattered.
- Passing a GUID in `organizationIds` returns an explicit
  `"could not be converted to System.Int64"`.
- **Reusing a body that still contains an old `queryHash` makes the run report
  `Errored`.** Strip it before re-running with different selections.

## 6. What was blocking it

Both blockers were found the same way: drive the portal UI in a browser,
intercept the real `/Query/Run`, and diff it against what the script sends.
Neither produces an error message; both return HTTP 200 with an empty table.

### Blocker 1: `fileImportIds`

The app's body carried five fields mine did not: `fileImportIds`, `columnSet`,
`asOfDate`, `availableAspects`, `lastStepNumber`. An ablation test isolates the
one that matters:

| removed from a working body | result |
|---|---|
| `lastStepNumber` | still works |
| `aspect` | still works |
| `columnSet` | still works |
| **`fileImportIds`** | **HTTP 400** |

`fileImportIds` identifies the data import, it is required, and **the wizard
walk already returns it**. `GET /QuerySelection/{clientId}` starts with
`fileImportIds: []` and the server fills it in as steps are selected.

**Rule: never hand-assemble a run body.** Walk the wizard, take the object it
returns, strip `queryHash`, and add only the execution fields.

### Blocker 2: the `selectedOrganizations` shape

`/Organization/Query` returns records shaped like:

```json
{"id": 127907, "name": "ABILENE ISD", "organizationLevelId": 2,
 "entityKey": "...", "entityExternalId": "221901", "parent": {...}, ...}
```

`/Query/Run` will not accept that. It wants a four-field projection with
**different key names**:

```json
{"organizationId": 127907, "organizationName": "ABILENE ISD",
 "entityExternalId": "221901", "organizationLevelId": 2}
```

Pass the record through unchanged and you get HTTP 200, `isError: false`,
`noDataRows: true`, and a 2-byte CSV. No error, no hint. `PortalClient.org_ref()`
does the remap.

### A quirk to design around: batch your organizations

Single-organization queries are unreliable. `HOUSTON ISD` alone needed a second
attempt; `AUSTIN ISD` alone returns nothing across six attempts and both cache
settings, yet returns data immediately when paired with any other district. The
UI reproduces this: `ABBOTT ISD` alone showed "There is no data available for
this query", then showed 21 tests once a second district was added.

Batching is the fix, and it is the right design regardless — one request for
many organizations rather than 1,314 requests. Verified:

```
50 districts in one request  -> 45 data rows
50 campuses  in one request  -> 12 data rows   (most campuses have no grade 3)
```

Output carries the TEA identifier directly in an `ID/CDC` column:

```
"A+ ACADEMY","057829","Spring 2024","3","105","1402",...
"A B DUNCAN COLLEGIATE EL","077901101","Spring 2024","3","42","1453",...
```

6-digit CDN for districts, 9-digit campus id for campuses. No crosswalk needed.

The `run()` retry loop is also load-bearing: a freshly computed query can answer
200 with an empty table before the result is really available. A single attempt
silently under-reports.

### Verified working

```
python3 portal_probe.py --query --statewide
python3 portal_probe.py --query --all-districts --batch 25
python3 portal_probe.py --query --all-campuses  --batch 25
```

## 7. The downloader

`portal_download.py` implements all of the above. It is a sibling of
`tapr_download.py`, not part of it: this is a JSON API with async job semantics,
not an HTML form scrape.

```bash
python3 portal_download.py --list                    # assessments, reports, administrations
python3 portal_download.py --estimate --levels D     # slices and request count
python3 portal_download.py --levels D --administrations "Spring 2024"
```

Output: `portal_data/<assessment>/<report>/<administration>_<level>.csv.gz`
plus `manifest.csv` (status, rows, cols, bytes, sha256, seconds per slice).
Runs resume; existing slices are skipped.

### What keeps the crawl tractable

**Multi-select.** `subjects` and `grades` carry `allowFieldMutiSelect: true`,
and the API honours it: selecting all 4 subjects and all 6 grades returns one
table with subjects as column blocks and grades as rows. That is 24 queries
collapsed into 1. Verified at 74 columns per district-administration.

**Batching.** Organizations go in groups. Measured ceiling:

| batch size | result |
|---|---|
| 60, 80, 100, 150 | works |
| 200, 250, 300 | **HTTP 500**, consistently, across retries |
| 500, 1314 | HTTP 500 |

The default is 100, the cap is 150. District level is 14 requests per slice,
campus level about 76.

### Guards worth knowing about

Batches are concatenated into one file per slice, so two failure modes get
explicit checks rather than silent corruption:

- **Header drift between batches** aborts the slice. Concatenating tables whose
  columns disagree would misalign every row after the first batch.
- **Ragged rows** against the header abort the slice.

The retry loop in `run_query()` is also load-bearing: a freshly computed query
can answer 200 with an empty table before the result is actually available.
A single attempt silently under-reports.

### Verified

STAAR 3-8, Group Summary, all 4 subjects x all 6 grades per query:

| level | administration | rows | cols | batches | time |
|---|---|---|---|---|---|
| State | Spring 2019 | 6 | 94 | 1 | 8s |
| State | Spring 2024 | 6 | 74 | 1 | - |
| District | Spring 2023 | 6,947 | 74 | 14 | 77s |
| District | Spring 2024 | 6,957 | 74 | 14 | - |
| Campus | Spring 2024 | 21,860 | 74 | 76 | 411s |

0 ragged rows throughout. `ID/CDC` widths are exactly 6 at district level and
exactly 9 at campus level, so the join to TAPR is direct. 6,937 distinct
campuses and ~1,190 of 1,314 districts appear, which is expected: high-school
only campuses and charters have no STAAR 3-8.

Note the state file has 94 columns in 2019 and 74 in 2024. Column counts move
between years because TEA changes what it reports, the same comparability
problem documented for TAPR in [PLAN.md](PLAN.md). Budget for it.

Cost basis for planning: a district slice is ~80s, a campus slice ~7 minutes.

## 8. Where the limits are, and where to stop

The site loads Google reCAPTCHA, and the API has `/Query/Verify` and
`/Query/ResendCode` alongside `/Query/Offline`. Read together, that says the
**offline path — large exports delivered by email — is gated by CAPTCHA and
email verification**. Do not automate around that gate. If a slice is big enough
to need the offline path, either narrow the slice, run it interactively, or
contact TEA/Cambium about bulk access.

The interactive query path used above hits no CAPTCHA, which is why it is the
one worth scripting.

Two other things to settle before building at scale:

1. **Check the portal's terms of use.** It is a public research portal and the
   API is explicitly named `api-public`, but "no auth" is not the same as
   "unlimited automated use". Worth five minutes and, for a sustained crawl, an
   email to TEA.
2. **Identify yourself.** Send a descriptive `User-Agent` with a contact
   address rather than a spoofed Chrome string, so the operators can reach you
   instead of blocking you.

---

## Reproducing what I verified

Scratch files from this session, if useful:
`walk.py` (wizard walker), `walked.json` (completed selection),
`goodbody.json` (the run body that executes cleanly but returns no rows).

Verified live on 2026-08-03: base URL, Texas `clientId` and `aggMin`, the full
endpoint list, all three organization levels with counts, `entityExternalId` as
the TEA id at district and campus, the wizard step tree with option counts, the
twelve breakdown dimensions, the 202/poll/re-run flow, and the CSV endpoint
returning `text/csv`.
