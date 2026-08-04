#!/usr/bin/env python3
"""
TEA TAPR bulk downloader (rewrite of tapr_scraper_full.py).

TEA serves TAPR through three download routes, and the original scraper only
knew about one of them:

  SETPICK / LEGACY   (2013-2023)  single request, no wizard
                     prgopt=<YYYY>/tapr/tapr_download.sas
                     level via `sumlev` (C/D/R/S - no county)
                     dataset via `setpick` (year-specific codes: REF, STAAR1..6,
                     PART1/2, STAAR_ADD1..5, TAKS1/2, GRAD, COMP, PERF, PROF,
                     KG, OC/OG, PKEFF, ACCLER, STAARV, PARTV, PERF1/2/3)
                     output: CSV with ONE header row (varnames only)

  SETPICK / ADVANCED (2024-2025)  the same shape, different prgopt
                     prgopt=2024/tapr/Advanced Download/getdata_2024.sas + ccyy
                     Carries the legacy schema forward: one header row, full
                     N/D/R, same year-embedded names (DDA03ARE1024D). This is
                     what lets 2013-2024 be appended without a format break.
                     Undocumented - TEA links it from no page. 2025 is partial
                     (STAAR1 errors; REF/PROF/KG/GRAD/COMP/PERF1 work).

  WIZARD             (2024-)      multi-step POST
                     prgopt=reports/tapr/dd/dd_tapr.sas
                     level via `tapr` (all_c/all_d/all_r/all_co/all_s)
                     dataset via `dsname` (33 codes: REF, STUD, STAF, STAAR_*, ...)
                     REQUIRES `var_type` (N/D/R) or TEA returns identifiers only
                     output: CSV with TWO header rows (labels, then varnames)

The setpick route is the default here because one schema across 2013-2024 is
worth more than the extra categories the wizard offers. Use --route wizard for
those (STUD, STAF, STAAR_ALL, DROP_ATT, PK) and for 2025 STAAR.

Bugs fixed relative to the original:

  1. var_type was never submitted. TEA answers a wizard-route request with no
     var_type by returning ONLY identifier columns. Measured on 2024 campus
     STAAR_ALL: 4 columns / 523 KB without it, 1,100 columns / 20 MB with it.
     Every substantive category the original downloaded was an empty shell.

  2. Legacy years were unreachable. The original pointed every year at the
     wizard endpoint, which answers "This request completed with errors" (a
     160-byte body, HTTP 200) for 2013-2023. The original's bare `except`
     swallowed this and its counter still reported success.

  3. Year discovery was fictional. The wizard step-2 page is byte-identical for
     every ccyy (60,685 bytes, all 33 categories, whether you ask for 1998 or
     2099), so the README's "dynamic discovery" does not validate anything.
     Valid years can only be established by attempting a download.

  4. Regex HTML parsing broke on TEA's inconsistent markup: `value=marykay`
     unquoted on step 3 vs `value="0"` quoted on step 2, and varying attribute
     order. Replaced with html.parser.

  5. No response validation. Throttle pages, SAS error stubs and empty bodies
     were written to disk as .csv files.

  6. No retry. TEA throttles both by dropping the TCP connection and by
     returning HTTP 429, so sustained runs silently lose files.

  7. No resume, and a success counter that incremented on failure.

Usage:
    python3 tapr_download.py --years 2013-2025 --levels C D
    python3 tapr_download.py --route wizard --years 2024-2025 --datasets STAAR_ALL
    python3 tapr_download.py --audit-years 2013-2025    # variable inventory only
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import random
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from html.parser import HTMLParser
from pathlib import Path

import requests

BROKER = "https://rptsvr1.tea.texas.gov/cgi/sas/broker"
TAPR_ROOT = "https://rptsvr1.tea.texas.gov/perfreport/tapr"

# First year served by the modern wizard. 2023 answers on both endpoints; we
# prefer the setpick route for it so that years share one schema shape.
MODERN_FROM = 2024

# The setpick ("Advanced") route continues into 2024-2025 under a different
# prgopt. The SAS program is pinned at the 2024 one and `ccyy` selects the year;
# building the path from the year (2025/.../getdata_2025.sas) returns
# "Error reading SAS output". TEA no longer links this endpoint from any page,
# so treat it as undocumented and verify it still answers before a long run.
ADVANCED_PRGOPT = "2024/tapr/Advanced Download/getdata_2024.sas"

# No HTML page exposes the 2024-2025 setpick list, so we probe this candidate
# set (the 2023 list plus the 2024-era additions) and record what answers.
ADVANCED_CANDIDATES = [
    "REF", "STAAR1", "STAAR2", "STAAR3", "STAAR4", "STAAR5", "STAAR6",
    "PART1", "PART1A", "PART2", "STAAR_ADD1", "STAAR_ADD2", "STAAR_ADD3",
    "STAAR_ADD4", "STAAR_ADD5", "GRAD", "COMP", "PERF1", "PERF2", "PERF3",
    "PROF", "KG", "PKEFF", "OG",
]

# Canonical level codes used by this tool, mapped onto each endpoint's spelling.
LEVELS = {
    "C": ("Campuses", "all_c", "C"),
    "D": ("Districts", "all_d", "D"),
    "R": ("Regions", "all_r", "R"),
    "S": ("State", "all_s", "S"),
    "O": ("Counties", "all_co", None),   # county exists on the modern endpoint only
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# TEA's SAS broker error stub, returned with HTTP 200.
SAS_ERROR = b"This request completed with errors"

IDENT_COLS = {"CAMPUS", "CAMPNAME", "DISTRICT", "DISTNAME", "REGION",
              "REGNNAME", "COUNTY", "CNTYNAME", "SUMLEV"}


# --------------------------------------------------------------------------
# HTML parsing
# --------------------------------------------------------------------------

class FormParser(HTMLParser):
    """Collect form controls regardless of attribute order or quoting style."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden: dict[str, str] = {}
        self.radios: list[tuple[str, str, str]] = []       # (name, value, id)
        self.checkboxes: list[tuple[str, str, bool]] = []  # (name, value, checked)
        self.selects: dict[str, list[str]] = {}
        self.form_action: str | None = None
        self._select: str | None = None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v if v is not None else "") for k, v in attrs}
        if tag == "form":
            action = a.get("action", "")
            if "broker" in action:
                self.form_action = action
        elif tag == "input":
            t = a.get("type", "text").lower()
            n, v = a.get("name", ""), a.get("value", "")
            if t == "hidden" and n:
                self.hidden[n] = v
            elif t == "radio" and n:
                self.radios.append((n, v, a.get("id", "")))
            elif t == "checkbox" and n:
                self.checkboxes.append((n, v, "checked" in a))
        elif tag == "select":
            self._select = a.get("name")
            if self._select:
                self.selects[self._select] = []
        elif tag == "option" and self._select:
            self.selects[self._select].append(a.get("value", ""))

    def handle_endtag(self, tag):
        if tag == "select":
            self._select = None


class LabelParser(HTMLParser):
    """input id -> visible <label> text, for human-readable dataset names.

    TEA closes its label elements with `<label>` rather than `</label>`:

        <label for='dd1'>District Reference<label>

    The step-2 page has 65 opening label tags and zero closing ones, so a
    parser that waits for an end tag collects nothing. We flush on the next
    opening tag and again at close().
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.labels: dict[str, str] = {}
        self._for: str | None = None
        self._buf: list[str] = []

    def _flush(self):
        if self._for is not None:
            txt = " ".join("".join(self._buf).split())
            if txt:
                self.labels.setdefault(self._for, txt)
        self._for, self._buf = None, []

    def handle_starttag(self, tag, attrs):
        if tag == "label":
            self._flush()   # the malformed `<label>` that closes the previous one
            self._for = dict((k.lower(), v) for k, v in attrs).get("for")
            self._buf = []

    def handle_data(self, data):
        if self._for is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "label":
            self._flush()

    def close(self):
        super().close()
        self._flush()


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------

@dataclass
class Result:
    year: int
    level: str
    level_name: str
    dataset: str
    endpoint: str                  # legacy | modern
    label: str = ""
    status: str = "pending"        # ok | skipped | invalid | error
    path: str = ""
    tea_filename: str = ""
    bytes: int = 0
    sha256: str = ""
    n_rows: int = 0
    n_cols: int = 0
    n_header_rows: int = 0
    n_keys: int = 0
    var_types: list[str] = field(default_factory=list)
    message: str = ""
    seconds: float = 0.0


# --------------------------------------------------------------------------
# Downloader
# --------------------------------------------------------------------------

class TaprDownloader:
    def __init__(self, output_dir="tapr_data", pace=2.5, retries=5,
                 timeout=300, compress=True, dictionaries=True,
                 route="auto", prefer_wizard=False):
        self.out = Path(output_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.pace, self.retries, self.timeout = pace, retries, timeout
        self.compress, self.dictionaries = compress, dictionaries
        self.route, self.prefer_wizard = route, prefer_wizard
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT})
        self.results: list[Result] = []
        self._legacy_forms: dict[int, dict] = {}

    # -- transport ---------------------------------------------------------

    def _pause(self, mult=1.0):
        time.sleep(self.pace * mult * random.uniform(0.8, 1.3))

    def _req(self, method, url, **kw):
        """One request, with backoff.

        TEA sheds load by resetting the connection rather than returning 429,
        so ConnectionError has to be treated as throttling and retried.
        """
        kw.setdefault("timeout", self.timeout)
        last = None
        for attempt in range(1, self.retries + 1):
            try:
                r = self.s.request(method, url, **kw)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                r.raise_for_status()
                return r
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt == self.retries:
                    break
                wait = min(150, 5 * 2 ** attempt) * random.uniform(0.8, 1.2)
                print(f"        retry {attempt}/{self.retries} in {wait:.0f}s "
                      f"({type(e).__name__})", flush=True)
                time.sleep(wait)
        raise RuntimeError(f"failed after {self.retries} attempts: {last}")

    # -- legacy endpoint (2013-2023) --------------------------------------

    def legacy_form(self, year: int) -> dict:
        """Learn a year's setpick/sumlev options.

        For 2013-2023 these are read from that year's DownloadData.html. The
        list genuinely changes year to year (TAKS1/TAKS2 in 2013-14, COMP from
        2015, GRAD/KG/OC from 2018, the COVID-era ACCLER/STAARV/PKEFF block in
        2021-22), so it must be read per year rather than assumed.

        For 2024-2025 no such page exists, so we fall back to the candidate
        list and let the per-dataset validation weed out what TEA does not
        serve. Known gap: STAAR1 answers for 2024 but not 2025.
        """
        if year in self._legacy_forms:
            return self._legacy_forms[year]
        if year >= MODERN_FROM:
            info = {"hidden": {}, "setpick": list(ADVANCED_CANDIDATES),
                    "sumlev": ["C", "D", "R", "S"], "advanced": True}
        else:
            r = self._req("GET", f"{TAPR_ROOT}/{year}/download/DownloadData.html")
            fp = FormParser(); fp.feed(r.text)
            info = {
                "hidden": fp.hidden,
                "setpick": [v for n, v, _ in fp.radios if n == "setpick"],
                "sumlev": [v for n, v, _ in fp.radios if n == "sumlev"],
                "advanced": False,
            }
        self._legacy_forms[year] = info
        return info

    def legacy_download(self, year, level, setpick):
        """Single request on the setpick route; returns CSV directly.

        Both eras share one output schema: a single header row of variable
        names, year-embedded names (DDA03ARE1024D), and full numerator /
        denominator / rate coverage. That is what makes 2013-2024 appendable
        without the two-header-row special case the wizard route needs.
        """
        form = self.legacy_form(year)
        data = dict(form["hidden"])
        data.update({"sumlev": LEVELS[level][2], "setpick": setpick})
        data.setdefault("_service", "marykay")
        data.setdefault("_program", "perfrept.perfmast.sas")
        data.setdefault("_debug", "0")
        if form.get("advanced"):
            data["prgopt"] = ADVANCED_PRGOPT
            data["ccyy"] = str(year)
        else:
            data.setdefault("prgopt", f"{year}/tapr/tapr_download.sas")
            data.setdefault("year4", str(year))
            data.setdefault("year2", str(year)[2:])
            data.setdefault("topic", "acct")
            data.setdefault("title", "Data Download")
        r = self._req("POST", BROKER, data=data)
        return r.content, self._tea_filename(r), 0, []

    # -- modern endpoint (2024-) ------------------------------------------

    def modern_datasets(self, year, level):
        tapr = LEVELS[level][1]
        r = self._req("GET", BROKER, params={
            "_service": "marykay", "_program": "perfrept.perfmast.sas",
            "_debug": "0", "ccyy": year, "tapr": tapr,
            "prgopt": "reports/tapr/dd/dd_tapr.sas"})
        fp = FormParser(); fp.feed(r.text)
        lp = LabelParser(); lp.feed(r.text); lp.close()
        cats = [{"value": v, "label": lp.labels.get(i, v)}
                for n, v, i in fp.radios if n == "dsname"]
        # TEA's county page is broken: it ships `bylev=&bylev.` (an unresolved
        # SAS macro variable) and an empty `sumlev`, so every download from it
        # returns the broker error stub. Detect it rather than emitting 33
        # confusing per-dataset failures.
        if not fp.hidden.get("sumlev") or "&" in fp.hidden.get("bylev", ""):
            raise RuntimeError(
                f"TEA's {tapr} page is serving unresolved SAS macros "
                f"(sumlev={fp.hidden.get('sumlev')!r}, "
                f"bylev={fp.hidden.get('bylev')!r}); downloads from it cannot "
                f"succeed. This is a defect on TEA's side.")
        return cats, fp.hidden

    def modern_download(self, year, level, dsname, step2_hidden):
        data = dict(step2_hidden)
        data.update({"dsname": dsname, "step": "3"})
        r = self._req("POST", BROKER, data=data)
        fp = FormParser(); fp.feed(r.text)
        keys = [v for n, v, _ in fp.checkboxes if n == "key"]
        var_types = [v for n, v, _ in fp.checkboxes if n == "var_type"]
        fmts = fp.selects.get("datafmt", ["csv"])
        action = fp.form_action or "/cgi/sas/broker/"
        # An empty `keys` list is legitimate: region-level REF, for example,
        # offers no selectable elements but still serves a file. Submit anyway.

        post = [(k, v) for k, v in fp.hidden.items()]
        post += [("key", k) for k in keys]
        # The critical fix. Omitting var_type yields identifier columns only.
        post += [("var_type", v) for v in var_types]
        post.append(("datafmt", "csv" if "csv" in fmts else fmts[0]))

        self._pause(0.5)
        url = requests.compat.urljoin(BROKER, action)
        r2 = self._req("POST", url, data=post)
        return r2.content, self._tea_filename(r2), len(keys), var_types

    @staticmethod
    def _tea_filename(r) -> str:
        cd = r.headers.get("Content-Disposition", "")
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";\r\n]+)"?', cd)
        return m.group(1).strip() if m else ""

    # -- validation --------------------------------------------------------

    @staticmethod
    def validate(content: bytes, endpoint: str, expect_data: bool = True):
        """Return (ok, n_rows, n_cols, n_header_rows, message).

        Necessary because TEA returns HTTP 200 for every failure mode: SAS error
        stubs, empty bodies and HTML pages all arrive as 200.

        `expect_data` should be False when the dataset offered no `var_type`
        controls. Some extracts are legitimately identifier-only (region-level
        REF is just REGION and REGNNAME); the identifier-only check is a
        regression guard against the var_type bug, not a universal rule.
        """
        if not content:
            return False, 0, 0, 0, "empty response"
        head = content[:300].lstrip()
        if SAS_ERROR in content[:300]:
            return False, 0, 0, 0, "SAS broker error (dataset absent for this year/level)"
        if head[:60].lower().startswith((b"<!doctype", b"<html", b"<head", b"<hr")):
            return False, 0, 0, 0, "HTML returned instead of data"
        try:
            rows = list(csv.reader(io.StringIO(content.decode("latin-1"))))
        except Exception as e:  # noqa: BLE001
            return False, 0, 0, 0, f"csv parse error: {e}"
        rows = [r for r in rows if r and any(c.strip() for c in r)]
        if len(rows) < 2:
            return False, 0, 0, 0, f"only {len(rows)} row(s)"

        # Modern files carry a label row above the varname row; legacy do not.
        # Detect by structure rather than by year, so the check still holds if
        # TEA changes the layout again.
        n_hdr = 2 if (endpoint == "wizard" and len(rows) > 2
                      and TaprDownloader._looks_like_varnames(rows[1])
                      and not TaprDownloader._looks_like_varnames(rows[0])) else 1
        names = rows[n_hdr - 1]
        n_cols = len(names)
        if len(rows) <= n_hdr:
            return False, 0, n_cols, n_hdr, "header only, no data rows"
        if expect_data and {c.strip().upper() for c in names} <= IDENT_COLS:
            return False, len(rows) - n_hdr, n_cols, n_hdr, \
                "identifier columns only (var_type not submitted)"
        return True, len(rows) - n_hdr, n_cols, n_hdr, ""

    @staticmethod
    def _looks_like_varnames(row) -> bool:
        """TEA varnames are short, no spaces, no punctuation beyond underscore."""
        cells = [c.strip() for c in row if c.strip()]
        if not cells:
            return False
        ok = sum(1 for c in cells if re.fullmatch(r"[A-Za-z][A-Za-z0-9_|]{0,31}", c))
        return ok / len(cells) > 0.9

    # -- data dictionary ---------------------------------------------------

    def fetch_dictionary(self, year, level, dsname, dest: Path) -> bool:
        """Save TEA's per-dataset codebook (modern endpoint only, plain GET).

        Worth keeping for every year: modern labels embed the year
        ("District 2024 Flag - Charter Operator"), so a varname x year label
        crosswalk is required before years can be appended.
        """
        try:
            r = self._req("GET", BROKER, params={
                "_service": "marykay", "_program": "perfrept.perfmast.sas",
                "_debug": "0", "ccyy": year, "sumlev": LEVELS[level][2] or "D",
                "dsname": dsname, "dd": dsname.lower(), "asvab": "",
                "prgopt": "reports/tapr/dd/dd_tapr_dictionary.sas"})
            if len(r.content) < 500:
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.content)
            return True
        except Exception:  # noqa: BLE001 - best effort
            return False

    # -- orchestration -----------------------------------------------------

    def _path(self, year, level, dataset) -> Path:
        ext = ".csv.gz" if self.compress else ".csv"
        return (self.out / str(year) / LEVELS[level][0]
                / f"tapr_{year}_{level}_{dataset}{ext}")

    def one(self, year, level, dataset, label, step2_hidden=None, force=False) -> Result:
        endpoint = self._endpoint_for(year)
        res = Result(year=year, level=level, level_name=LEVELS[level][0],
                     dataset=dataset, endpoint=endpoint, label=label)
        path = self._path(year, level, dataset)
        if path.exists() and not force:
            res.status, res.path, res.bytes = "skipped", str(path), path.stat().st_size
            res.message = "already present"
            return res

        t0 = time.time()
        try:
            if endpoint == "wizard":
                content, tea_name, n_keys, vts = self.modern_download(
                    year, level, dataset, step2_hidden or {})
            else:
                content, tea_name, n_keys, vts = self.legacy_download(
                    year, level, dataset)
            res.tea_filename, res.n_keys, res.var_types = tea_name, n_keys, vts

            ok, nr, nc, nh, msg = self.validate(content, endpoint,
                                                expect_data=bool(vts))
            res.n_rows, res.n_cols, res.n_header_rows, res.message = nr, nc, nh, msg
            if not ok:
                res.status = "invalid"
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                if self.compress:
                    with gzip.open(path, "wb") as f:
                        f.write(content)
                else:
                    path.write_bytes(content)
                res.status, res.path = "ok", str(path)
                res.bytes = path.stat().st_size
                res.sha256 = hashlib.sha256(content).hexdigest()
                if self.dictionaries and endpoint == "wizard":
                    dd = path.parent / "_dictionary" / f"{dataset}.html"
                    if not dd.exists():
                        self._pause(0.4)
                        self.fetch_dictionary(year, level, dataset, dd)
        except Exception as e:  # noqa: BLE001
            res.status, res.message = "error", f"{type(e).__name__}: {e}"
        res.seconds = time.time() - t0
        return res

    def datasets_for(self, year, level):
        """(dataset code, label, step2_hidden) triples available for a year/level.

        Two routes, and they are NOT interchangeable: the setpick route uses
        codes like STAAR1/PERF1, the wizard uses STAAR_ALL/STUD. Route choice
        therefore changes which `--datasets` values are valid.
        """
        if self.route == "wizard" or (self.route == "auto" and year >= MODERN_FROM
                                      and self.prefer_wizard):
            cats, hidden = self.modern_datasets(year, level)
            return [(c["value"], c["label"], hidden) for c in cats]
        if LEVELS[level][2] is None:
            return []      # county exists only on the wizard route, and is broken
        form = self.legacy_form(year)
        if LEVELS[level][2] not in form["sumlev"]:
            return []
        return [(sp, "", None) for sp in form["setpick"]]

    def _endpoint_for(self, year):
        if self.route == "wizard" or (self.route == "auto" and year >= MODERN_FROM
                                      and self.prefer_wizard):
            return "wizard"
        return "advanced" if year >= MODERN_FROM else "legacy"

    def run(self, years, levels, datasets=None, force=False):
        for year in years:
            for level in levels:
                try:
                    avail = self.datasets_for(year, level)
                except Exception as e:  # noqa: BLE001
                    print(f"\n=== {year} / {LEVELS[level][0]} === cannot enumerate: {e}",
                          flush=True)
                    continue
                if not avail:
                    print(f"\n=== {year} / {LEVELS[level][0]} === not offered "
                          f"({self._endpoint_for(year)} endpoint)", flush=True)
                    continue
                if datasets:
                    want = {d.upper() for d in datasets}
                    avail = [a for a in avail if a[0].upper() in want]
                print(f"\n=== {year} / {LEVELS[level][0]} === "
                      f"{self._endpoint_for(year)}, {len(avail)} datasets", flush=True)
                for code, label, hidden in avail:
                    r = self.one(year, level, code, label, hidden, force)
                    self.results.append(r)
                    mark = {"ok": "OK", "skipped": "--", "invalid": "!!",
                            "error": "XX"}.get(r.status, "??")
                    info = (f"{r.n_rows:>7,} x {r.n_cols:>5,}  {r.bytes/1e6:>7.2f} MB"
                            if r.status == "ok" else r.message[:62])
                    print(f"  [{mark}] {code:<12} {info}", flush=True)
                    self._pause()
                self.manifest()
        self.manifest()
        return self.results

    # -- outputs -----------------------------------------------------------

    def manifest(self):
        (self.out / "manifest.json").write_text(
            json.dumps([asdict(r) for r in self.results], indent=1))
        with open(self.out / "manifest.csv", "w", newline="") as f:
            w = csv.writer(f)
            cols = ["year", "level", "level_name", "dataset", "endpoint", "label",
                    "status", "path", "tea_filename", "bytes", "sha256", "n_rows",
                    "n_cols", "n_header_rows", "n_keys", "var_types", "message"]
            w.writerow(cols)
            for r in self.results:
                d = asdict(r)
                d["var_types"] = "|".join(r.var_types)
                w.writerow([d[c] for c in cols])

    def audit(self, years, levels, datasets=None):
        """Emit a varname x year inventory without keeping the data.

        This is the input to the label-drift crosswalk: it records, for every
        (year, level, dataset), the exact column names and label text TEA used
        that year, so renames and reorderings can be diffed rather than guessed.
        """
        rows = []
        for year in years:
            for level in levels:
                try:
                    avail = self.datasets_for(year, level)
                except Exception as e:  # noqa: BLE001
                    print(f"{year} {level}: {e}", flush=True)
                    continue
                if datasets:
                    want = {d.upper() for d in datasets}
                    avail = [a for a in avail if a[0].upper() in want]
                for code, label, hidden in avail:
                    endpoint = self._endpoint_for(year)
                    try:
                        if endpoint == "wizard":
                            content, _, _, _ = self.modern_download(year, level, code, hidden or {})
                        else:
                            content, _, _, _ = self.legacy_download(year, level, code)
                        ok, nr, nc, nh, msg = self.validate(content, endpoint,
                                                            expect_data=False)
                        if not ok:
                            print(f"  {year} {level} {code}: {msg}", flush=True)
                            self._pause(); continue
                        # Filter blank rows the same way validate() does, so the
                        # header index it returned lines up with these rows.
                        rdr = [r for r in csv.reader(io.StringIO(content.decode("latin-1")))
                               if r and any(c.strip() for c in r)]
                        names = rdr[nh - 1]
                        labels = rdr[0] if nh == 2 else [""] * len(names)
                        for pos, (n, lb) in enumerate(zip(names, labels), start=1):
                            rows.append({"year": year, "level": level, "dataset": code,
                                         "endpoint": endpoint, "position": pos,
                                         "varname": n.strip(),
                                         "varname_lower": n.strip().lower(),
                                         "tea_label": lb.strip()})
                        print(f"  {year} {level} {code}: {nc} vars", flush=True)
                    except Exception as e:  # noqa: BLE001
                        print(f"  {year} {level} {code}: {type(e).__name__}: {e}", flush=True)
                    self._pause()
        dest = self.out / "variable_inventory.csv"
        with open(dest, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["year", "level", "dataset", "endpoint",
                                              "position", "varname", "varname_lower",
                                              "tea_label"])
            w.writeheader(); w.writerows(rows)
        print(f"\nWrote {len(rows):,} variable-year rows to {dest}")
        return rows

    def summary(self):
        from collections import Counter
        c = Counter(r.status for r in self.results)
        tot = sum(r.bytes for r in self.results if r.status == "ok")
        print("\n" + "=" * 64)
        for k in ("ok", "skipped", "invalid", "error"):
            if c.get(k):
                print(f"  {k:<9} {c[k]:>5}")
        print(f"  {'MB':<9} {tot/1e6:>8.1f}")
        bad = [r for r in self.results if r.status in ("invalid", "error")]
        if bad:
            print(f"\n  {len(bad)} not retrieved:")
            for r in bad[:30]:
                print(f"    {r.year} {r.level} {r.dataset:<12} {r.message[:58]}")
        print("=" * 64)


def parse_years(spec):
    out = []
    for p in spec.split(","):
        p = p.strip()
        if "-" in p:
            a, b = p.split("-"); out.extend(range(int(a), int(b) + 1))
        elif p:
            out.append(int(p))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Download TEA TAPR flat files.")
    ap.add_argument("--years", default="2013-2025")
    ap.add_argument("--levels", nargs="*", default=["C", "D"], choices=list(LEVELS))
    ap.add_argument("--datasets", nargs="*", default=None)
    ap.add_argument("--output", default="tapr_data")
    ap.add_argument("--pace", type=float, default=2.5)
    ap.add_argument("--no-compress", action="store_true")
    ap.add_argument("--no-dictionary", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--audit-years", default=None,
                    help="write variable_inventory.csv for these years and exit")
    ap.add_argument("--route", choices=["auto", "setpick", "wizard"], default="auto",
                    help="auto/setpick use the Advanced setpick route for every "
                         "year (one header row, N/D/R, appendable 2013-2024); "
                         "wizard uses the 2024+ dsname wizard (two header rows, "
                         "different dataset codes)")
    a = ap.parse_args(argv)

    dl = TaprDownloader(a.output, a.pace, compress=not a.no_compress,
                        dictionaries=not a.no_dictionary,
                        route=("wizard" if a.route == "wizard" else "auto"),
                        prefer_wizard=(a.route == "wizard"))
    if a.audit_years:
        dl.audit(parse_years(a.audit_years), a.levels, a.datasets)
        return 0
    years = parse_years(a.years)
    print(f"TEA TAPR downloader\n  years:    {years[0]}-{years[-1]} ({len(years)})"
          f"\n  levels:   {', '.join(LEVELS[l][0] for l in a.levels)}"
          f"\n  datasets: {a.datasets or 'all'}\n  output:   {a.output}")
    dl.run(years, a.levels, a.datasets, a.force)
    dl.summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
