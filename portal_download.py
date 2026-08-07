#!/usr/bin/env python3
"""
Texas Assessment Research Portal bulk downloader.

Downloads STAAR / STAAR Alternate 2 / TELPAS aggregate results from
txresearchportal.com's public JSON API, at state, district and campus level,
with the twelve student-group breakdown dimensions TAPR does not carry
(`gifted`, `plan_504`, `titleia_flag`, `migrant`, and the rest).

See PORTAL.md for the API map. The two things that make the difference between
data and a 2-byte file, both of which fail silently with HTTP 200:

  1. `fileImportIds` is required, and the wizard walk supplies it. Build the run
     body FROM the walked selection object; never assemble it field by field.
  2. `selectedOrganizations` needs {organizationId, organizationName,
     entityExternalId, organizationLevelId} -- NOT the shape
     /Organization/Query returns.

Output CSVs carry the TEA identifier in an `ID/CDC` column (6-digit CDN for
districts, 9-digit campus id for campuses), so they join to TAPR with no
crosswalk.

Do NOT automate the offline/email export path (/Query/Offline + /Query/Verify):
it is gated by reCAPTCHA and email verification.

Usage:
    python3 portal_download.py --list                     # show the crawl space
    python3 portal_download.py --estimate --levels D      # cost, without running
    python3 portal_download.py --levels D --administrations "Spring 2024"
    python3 portal_download.py --levels D --assessments "STAAR 3-8" \
        --reports "Group Summary: Performance Levels & Reporting Categories"
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
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

API = "https://api-public.prod.publicfolio.cambiumreports.com/v1"
TEXAS_CLIENT_ID = "c21a285e-d0fa-4c1a-9070-32b0d50178e1"

# "S" is statewide: it has no organization list, it is requested with
# stateFlag=true and an empty organizationIds, so its ordinal is None.
LEVELS = {"S": ("State", None), "R": ("Regions", 1),
          "D": ("Districts", 2), "C": ("Campuses", 3)}

# Organizations per /Query/Run. Measured: 150 succeeds, 200/250/300 return
# HTTP 500 consistently (retried, not transient). 100 leaves headroom.
MAX_BATCH = 150
DEFAULT_BATCH = 100

USER_AGENT = ("TEA-TAPR-download/1.0 (research data collection; "
              "contact: eric.a.booth@gmail.com)")

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://txresearchportal.com",
    "Referer": "https://txresearchportal.com/",
    "User-Agent": USER_AGENT,
}


@dataclass
class Slice:
    assessment: str
    report: str
    administration: str
    level: str
    status: str = "pending"
    path: str = ""
    n_orgs: int = 0
    n_batches: int = 0
    n_rows: int = 0
    n_cols: int = 0
    bytes: int = 0
    sha256: str = ""
    seconds: float = 0.0
    message: str = ""


class PortalDownloader:
    def __init__(self, out="portal_data", pace=1.0, batch=DEFAULT_BATCH,
                 retries=6, timeout=900, client_id=TEXAS_CLIENT_ID):
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.pace = pace
        self.batch = min(batch, MAX_BATCH)
        self.retries = retries
        self.timeout = timeout
        self.client_id = client_id
        self.results: list[Slice] = []
        self._orgs: dict[str, list] = {}

    # -- transport ---------------------------------------------------------

    def _pause(self, mult=1.0):
        time.sleep(self.pace * mult * random.uniform(0.8, 1.25))

    def _call(self, path, body=None, method="GET", raw=False):
        last = None
        for attempt in range(1, self.retries + 1):
            req = urllib.request.Request(
                API + path, method=method,
                data=json.dumps(body).encode() if body is not None else None,
                headers=HEADERS)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    d = r.read()
                    return r.status, (d if raw else (json.loads(d) if d.strip() else None))
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}"
                # 400 is a request-shape bug; retrying will not help.
                if e.code == 400:
                    raise RuntimeError(f"{last}: {e.read()[:300].decode(errors='replace')}")
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__}: {e}"
            if attempt < self.retries:
                time.sleep(min(90, 4 * 2 ** attempt) * random.uniform(0.8, 1.2))
        raise RuntimeError(f"{path} failed after {self.retries} attempts: {last}")

    # -- organizations -----------------------------------------------------

    @staticmethod
    def org_ref(o):
        """Project an /Organization/Query record into the shape /Query/Run wants.

        Not cosmetic: passing the record through unchanged returns an empty
        table with HTTP 200 and no error.
        """
        return {"organizationId": o["id"], "organizationName": o["name"],
                "entityExternalId": o["entityExternalId"],
                "organizationLevelId": o["organizationLevelId"]}

    def organizations(self, level: str):
        if level in self._orgs:
            return self._orgs[level]
        ordinal = LEVELS[level][1]
        out, page = [], 1
        while True:
            _, d = self._call("/Organization/Query",
                              {"clientId": self.client_id, "level": [ordinal],
                               "pageNumber": page, "pageSize": 500}, "POST")
            rows = d.get("data") or []
            out.extend(rows)
            if not rows or len(out) >= d.get("totalRecords", 0):
                break
            page += 1
            self._pause(0.3)
        self._orgs[level] = out
        return out

    # -- wizard ------------------------------------------------------------

    def walk(self, assessment=None, report=None, administration=None,
             multiselect_all=True):
        """Walk the selection wizard to a completed selection object.

        Multi-select steps (subjects, grades) are selected in FULL, which is a
        large win: one query covers all 4 subjects x 6 grades rather than 24
        separate queries. Verified to return subjects as column blocks and
        grades as rows.

        Steps are chosen one at a time and re-POSTed, because option ids are
        context-specific -- editing a saved object invalidates downstream ids.
        """
        want = {"assessment": assessment, "report": report,
                "administrations": administration}
        _, qs = self._call(f"/QuerySelection/{self.client_id}")
        guard = 0
        while True:
            guard += 1
            if guard > 20:
                raise RuntimeError("wizard did not converge")
            pending = [s for s in qs["selections"]
                       if not any(v.get("selected") for v in s["values"])]
            if not pending:
                return qs
            step = pending[0]
            name = step["stepName"]
            target = want.get(name)
            if target is None and multiselect_all and step.get("allowFieldMutiSelect"):
                for v in step["values"]:
                    v["selected"] = True
            else:
                t = next((v for v in step["values"] if v["text"] == target), None)
                if target and t is None:
                    raise LookupError(f"{name}: {target!r} not offered "
                                      f"(have: {[v['text'] for v in step['values']][:6]})")
                (t or step["values"][0])["selected"] = True
            _, qs = self._call("/QuerySelection", qs, "POST")
            self._pause(0.3)

    def crawl_space(self):
        """Enumerate (assessment -> reports, administrations) without running."""
        _, qs = self._call(f"/QuerySelection/{self.client_id}")
        assessments = [v["text"] for v in qs["selections"][0]["values"]]
        space = {}
        for a in assessments:
            _, q = self._call(f"/QuerySelection/{self.client_id}")
            for v in q["selections"][0]["values"]:
                v["selected"] = (v["text"] == a)
            _, q = self._call("/QuerySelection", q, "POST")
            reports = [v["text"] for s in q["selections"] if s["stepName"] == "report"
                       for v in s["values"]]
            # administrations depend on the report; probe with the first
            admins = []
            if reports:
                try:
                    full = self.walk(assessment=a, report=reports[0])
                    admins = [v["text"] for s in full["selections"]
                              if s["stepName"] == "administrations" for v in s["values"]]
                except Exception:  # noqa: BLE001
                    pass
            space[a] = {"reports": reports, "administrations": admins}
            self._pause(0.4)
        return space

    # -- execution ---------------------------------------------------------

    def build_body(self, selection, orgs, statewide=False, breakdown=None):
        body = {k: v for k, v in selection.items() if k != "queryHash"}
        body.update({
            "organizationIds": [o["id"] for o in orgs],
            "selectedOrganizations": [self.org_ref(o) for o in orgs],
            "stateFlag": bool(statewide),
            "queryProcessEngine": "ReportAsync",
            "aspect": "Default",
            "drillDownStack": [],
            "dynamicFilter": [],
            "dynamicBreakdown": breakdown or [],
            "bypassDefaultColumnVisibility": False,
        })
        return body

    def run_query(self, body):
        """Run to completion and return the CSV bytes.

        The retry loop matters: a freshly computed query can answer 200 with an
        empty table before its result is available, and succeed on a later
        attempt. A single try silently under-reports.
        """
        for attempt in range(self.retries):
            status, d = self._call("/Query/Run", body, "POST")
            if status == 202:
                qh = d["request"]["queryHash"]
                for _ in range(40):
                    time.sleep(3)
                    _, s = self._call(
                        "/Query/Status?queryHash=" + urllib.parse.quote(qh),
                        None, "POST")
                    st = s.get("status") if isinstance(s, dict) else None
                    if st == "Errored":
                        raise RuntimeError(f"query errored: {json.dumps(s)[:200]}")
                    if st == "Finished":
                        break
                continue
            table = (d.get("tables") or [{}])[0]
            if table.get("rows"):
                _, csv_bytes = self._call("/Query/Download", body, "POST", raw=True)
                return csv_bytes, len(table.get("rows") or []), len(table.get("columns") or [])
            self._pause(4)
        return b"", 0, 0

    # -- slices ------------------------------------------------------------

    @staticmethod
    def _safe(s):
        return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")[:80]

    def _path(self, sl: Slice) -> Path:
        return (self.out / self._safe(sl.assessment) / self._safe(sl.report)
                / f"{self._safe(sl.administration)}_{sl.level}.csv.gz")

    def do_slice(self, assessment, report, administration, level, force=False) -> Slice:
        sl = Slice(assessment, report, administration, level)
        path = self._path(sl)
        if path.exists() and not force:
            sl.status, sl.path, sl.bytes = "skipped", str(path), path.stat().st_size
            sl.message = "already present"
            return sl
        t0 = time.time()
        try:
            selection = self.walk(assessment, report, administration)
            if level == "S":
                orgs_batches = [[]]
                statewide = True
            else:
                orgs = self.organizations(level)
                sl.n_orgs = len(orgs)
                orgs_batches = [orgs[i:i + self.batch]
                                for i in range(0, len(orgs), self.batch)]
                statewide = False
            sl.n_batches = len(orgs_batches)

            header, data_rows, ncols = None, [], 0
            for bi, batch in enumerate(orgs_batches, 1):
                body = self.build_body(selection, batch, statewide=statewide)
                raw, nrows, nc = self.run_query(body)
                ncols = max(ncols, nc)
                if not raw or len(raw) < 4:
                    continue
                rows = [r for r in csv.reader(
                    io.StringIO(raw.decode("latin-1"))) if r and any(c.strip() for c in r)]
                if not rows:
                    continue
                if header is None:
                    header = rows[0]
                elif rows[0] != header:
                    # Batches are concatenated, so a header that drifts between
                    # them would silently misalign columns. Refuse rather than
                    # write a corrupt file.
                    raise RuntimeError(
                        f"header changed at batch {bi}/{len(orgs_batches)}: "
                        f"{len(rows[0])} cols vs {len(header)}")
                data_rows.extend(rows[1:])
                self._pause()

            if header is None:
                sl.status, sl.message = "empty", "no data for this slice"
                sl.seconds = time.time() - t0
                return sl

            ragged = sum(1 for r in data_rows if len(r) != len(header))
            if ragged:
                raise RuntimeError(f"{ragged} ragged rows against a "
                                   f"{len(header)}-column header")

            buf = io.StringIO()
            w = csv.writer(buf, lineterminator="\n")
            w.writerow(header)
            w.writerows(data_rows)
            payload = buf.getvalue().encode("utf-8")

            path.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(path, "wb") as f:
                f.write(payload)
            sl.status, sl.path = "ok", str(path)
            sl.n_rows, sl.n_cols = len(data_rows), len(header)
            sl.bytes = path.stat().st_size
            sl.sha256 = hashlib.sha256(payload).hexdigest()
        except Exception as e:  # noqa: BLE001
            sl.status, sl.message = "error", f"{type(e).__name__}: {e}"
        sl.seconds = time.time() - t0
        return sl

    def run(self, assessments=None, reports=None, administrations=None,
            levels=("D",), force=False, estimate_only=False):
        space = self.crawl_space()
        plan = []
        for a, info in space.items():
            if assessments and a not in assessments:
                continue
            for r in info["reports"]:
                if reports and r not in reports:
                    continue
                for adm in info["administrations"]:
                    if administrations and adm not in administrations:
                        continue
                    for lv in levels:
                        plan.append((a, r, adm, lv))

        n_req = 0
        for a, r, adm, lv in plan:
            if lv == "S":
                n_req += 1
            else:
                n = len(self.organizations(lv))
                n_req += -(-n // self.batch)
        print(f"\nplanned slices: {len(plan)}")
        print(f"estimated /Query/Run requests: {n_req:,} "
              f"(batch={self.batch}, ~{self.pace:.1f}s pacing)")
        est_h = n_req * (self.pace + 4) / 3600
        print(f"rough wall clock: {est_h:.1f} h\n")
        if estimate_only:
            for a, r, adm, lv in plan[:20]:
                print(f"   {a} | {r[:44]} | {adm} | {LEVELS[lv][0]}")
            if len(plan) > 20:
                print(f"   ... {len(plan)} total")
            return []

        for i, (a, r, adm, lv) in enumerate(plan, 1):
            sl = self.do_slice(a, r, adm, lv, force)
            self.results.append(sl)
            mark = {"ok": "OK", "skipped": "--", "empty": "  ",
                    "error": "XX"}.get(sl.status, "??")
            detail = (f"{sl.n_rows:>7,} x {sl.n_cols:>4}  {sl.bytes/1e6:>6.2f} MB "
                      f"({sl.n_batches} batches, {sl.seconds:.0f}s)"
                      if sl.status == "ok" else sl.message[:56])
            print(f"[{mark}] {i}/{len(plan)} {a[:18]:<18} {adm:<12} "
                  f"{LEVELS[lv][0][:9]:<9} {detail}", flush=True)
            self.manifest()
        self.manifest()
        return self.results

    # -- outputs -----------------------------------------------------------

    # -- endpoint health ---------------------------------------------------

    def health(self):
        """Probe the portal API. Cheap: three requests.

        The API is unofficial and unversioned, so the useful thing to know is
        WHICH contract broke, not merely that a download later came back empty.
        Each probe validates content, since this API can answer 200 with an
        empty payload.
        """
        checks = []

        def probe(name, fn):
            try:
                detail = fn()
                checks.append((name, True))
                print(f"  [OK]   {name:<30} {detail}", flush=True)
            except Exception as e:  # noqa: BLE001
                checks.append((name, False))
                print(f"  [FAIL] {name:<30} {type(e).__name__}: {str(e)[:70]}",
                      flush=True)
            self._pause(0.5)

        print("\nAssessment Portal API health\n")

        def _client():
            _, d = self._call(f"/Client/{self.client_id}")
            if not isinstance(d, dict) or d.get("name") != "Texas":
                raise RuntimeError(f"unexpected client record: {str(d)[:80]}")
            return f"name=Texas, aggMin={d.get('aggMin')} (masking threshold)"
        probe("client record", _client)

        def _orgs():
            _, d = self._call("/Organization/Query",
                              {"clientId": self.client_id, "level": [2],
                               "pageNumber": 1, "pageSize": 1}, "POST")
            n = d.get("totalRecords", 0) if isinstance(d, dict) else 0
            if n < 1000:
                raise RuntimeError(f"district count {n}; expected ~1,314")
            return f"{n:,} districts enumerable"
        probe("organization tree", _orgs)

        def _wizard():
            _, d = self._call(f"/QuerySelection/{self.client_id}")
            vals = [v["text"] for s in (d.get("selections") or [])
                    for v in s.get("values", [])]
            if len(vals) < 5:
                raise RuntimeError(f"only {len(vals)} assessments offered")
            return f"{len(vals)} assessments: {', '.join(vals[:3])}..."
        probe("selection wizard", _wizard)

        failed = [c for c in checks if not c[1]]
        print(f"\n{'PASS' if not failed else 'FAIL'}: "
              f"{len(checks) - len(failed)}/{len(checks)} probes healthy")
        return failed

    def manifest(self):
        """Write the manifest, MERGING with earlier runs.

        Runs are incremental -- a different assessment, administration or level
        each time -- so overwriting would discard the checksums and dimensions
        of every slice downloaded before. Keyed on
        (assessment, report, administration, level); the current run wins,
        except that a skip never displaces a richer earlier record.
        """
        man = self.out / "manifest.json"
        merged = {}
        if man.exists():
            try:
                for r in json.loads(man.read_text()):
                    merged[(r.get("assessment"), r.get("report"),
                            r.get("administration"), r.get("level"))] = r
            except Exception:  # noqa: BLE001
                pass
        for s in self.results:
            d = asdict(s)
            key = (d["assessment"], d["report"], d["administration"], d["level"])
            if d["status"] == "skipped" and key in merged:
                continue
            merged[key] = d
        rows = [merged[k] for k in sorted(merged, key=lambda k: tuple(str(x) for x in k))]
        man.write_text(json.dumps(rows, indent=1))

        cols = ["assessment", "report", "administration", "level", "status", "path",
                "n_orgs", "n_batches", "n_rows", "n_cols", "bytes", "sha256",
                "seconds", "message"]
        with open(self.out / "manifest.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for d in rows:
                w.writerow([d.get(c, "") for c in cols])

    def summary(self):
        from collections import Counter
        c = Counter(s.status for s in self.results)
        tot = sum(s.bytes for s in self.results if s.status == "ok")
        rows = sum(s.n_rows for s in self.results if s.status == "ok")
        print("\n" + "=" * 64)
        for k in ("ok", "skipped", "empty", "error"):
            if c.get(k):
                print(f"  {k:<9} {c[k]:>5}")
        print(f"  {'rows':<9} {rows:>10,}")
        print(f"  {'MB':<9} {tot/1e6:>10.1f}")
        bad = [s for s in self.results if s.status == "error"]
        if bad:
            print(f"\n  {len(bad)} errors:")
            for s in bad[:20]:
                print(f"    {s.assessment} | {s.administration} | {s.message[:50]}")
        print("=" * 64)


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Download TEA assessment results from txresearchportal.com.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--list", action="store_true", help="show the crawl space and exit")
    p.add_argument("--health", action="store_true",
                   help="probe the portal API (three requests) and exit")
    p.add_argument("--estimate", action="store_true", help="show cost, do not download")
    p.add_argument("--assessments", nargs="*", default=None)
    p.add_argument("--reports", nargs="*", default=None)
    p.add_argument("--administrations", nargs="*", default=None)
    p.add_argument("--levels", nargs="*", default=["D"], choices=list(LEVELS),
                   help="S state, R region, D district, C campus")
    p.add_argument("--output", default="portal_data")
    p.add_argument("--batch", type=int, default=DEFAULT_BATCH,
                   help=f"organizations per request (max {MAX_BATCH}; "
                        f"200+ returns HTTP 500)")
    p.add_argument("--pace", type=float, default=1.0)
    p.add_argument("--force", action="store_true")
    a = p.parse_args(argv)

    dl = PortalDownloader(a.output, pace=a.pace, batch=a.batch)

    if a.health:
        failed = dl.health()
        return 1 if failed else 0
    if a.list:
        space = dl.crawl_space()
        for asmt, info in space.items():
            print(f"\n{asmt}")
            print(f"  reports ({len(info['reports'])}):")
            for r in info["reports"]:
                print(f"    {r}")
            adm = info["administrations"]
            print(f"  administrations ({len(adm)}): {', '.join(adm[:8])}"
                  f"{' ...' if len(adm) > 8 else ''}")
        return 0

    print("Texas Assessment Research Portal downloader")
    print(f"  levels:  {', '.join(LEVELS[l][0] for l in a.levels)}")
    print(f"  output:  {a.output}")
    dl.run(a.assessments, a.reports, a.administrations, a.levels,
           a.force, estimate_only=a.estimate)
    if not a.estimate:
        dl.summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
