#!/usr/bin/env python3
"""
Texas Assessment Research Portal (txresearchportal.com) API probe.

The portal is a React SPA over a public JSON API at
https://api-public.prod.publicfolio.cambiumreports.com/v1/ — no authentication,
no API key, and no CAPTCHA on the interactive query path. This script maps that
API so a full downloader can be written against it. See PORTAL.md for the
narrative version.

STATUS: the wizard walk, organization tree and query execution all work. The
result table currently comes back empty (`noDataRows: true`), which is one
unidentified field in the /Query/Run body. Use `--dump-body` to emit the exact
request this script sends, then diff it against a real request captured from
browser DevTools. See PORTAL.md §6.

Do NOT automate the offline/email export path (/Query/Offline + /Query/Verify):
it is gated by reCAPTCHA and email verification.

Usage:
    python3 portal_probe.py --map                       # endpoints + client info
    python3 portal_probe.py --orgs district              # list organizations
    python3 portal_probe.py --orgs campus --search AUSTIN
    python3 portal_probe.py --wizard                     # enumerate the step tree
    python3 portal_probe.py --query --district 227901 --admin "Spring 2024"
    python3 portal_probe.py --query --statewide --dump-body body.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api-public.prod.publicfolio.cambiumreports.com/v1"
TEXAS_CLIENT_ID = "c21a285e-d0fa-4c1a-9070-32b0d50178e1"

# Organization hierarchy, verified 2026-08-03:
#   1 REGION (20)  2 DISTRICT (1,314)  3 INSTITUTION/campus (11,363)
LEVELS = {"region": 1, "district": 2, "campus": 3}

# Identify yourself rather than spoofing a browser: the operators should be able
# to contact you instead of blocking you.
USER_AGENT = "TEA-TAPR-download/1.0 (research data collection; contact: eric.a.booth@gmail.com)"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://txresearchportal.com",
    "Referer": "https://txresearchportal.com/",
    "User-Agent": USER_AGENT,
}


class PortalClient:
    def __init__(self, client_id: str = TEXAS_CLIENT_ID, pace: float = 0.5,
                 timeout: int = 300):
        self.client_id = client_id
        self.pace = pace
        self.timeout = timeout

    # -- transport ---------------------------------------------------------

    def _call(self, path: str, body=None, method: str = "GET"):
        req = urllib.request.Request(
            API + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return r.status, raw
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _json(self, path, body=None, method="GET"):
        status, raw = self._call(path, body, method)
        time.sleep(self.pace)
        try:
            return status, json.loads(raw) if raw.strip() else None
        except json.JSONDecodeError:
            return status, raw[:400].decode(errors="replace")

    # -- metadata ----------------------------------------------------------

    def client_info(self):
        """GET /Client/{id} -> {"name": "Texas", "aggMin": 5}.

        aggMin is the masking threshold: cells with fewer students are
        suppressed. Record it alongside any data you keep.
        """
        return self._json(f"/Client/{self.client_id}")[1]

    # -- organizations -----------------------------------------------------

    def organizations(self, level: str, search: str | None = None,
                      page_size: int = 200, max_pages: int | None = None):
        """POST /Organization/Query.

        Note the plain GET /Organization/paginated/{clientId} returns zero
        records whatever you pass it; only this POST works.

        `entityExternalId` on each record is the TEA identifier — 6-digit CDN
        for districts, 9-digit campus id for campuses — so results join to TAPR
        with no crosswalk. `id` is the portal's internal Int64 key and is what
        /Query/Run wants in `organizationIds`.
        """
        out, page = [], 1
        while True:
            body = {"clientId": self.client_id, "level": [LEVELS[level]],
                    "pageNumber": page, "pageSize": page_size}
            if search:
                body["searchText"] = search
            status, d = self._json("/Organization/Query", body, "POST")
            if status != 200 or not isinstance(d, dict):
                raise RuntimeError(f"organization query failed: {status} {d}")
            rows = d.get("data") or []
            out.extend(rows)
            total = d.get("totalRecords", 0)
            if not rows or len(out) >= total:
                break
            page += 1
            if max_pages and page > max_pages:
                break
        return out

    # -- selection wizard --------------------------------------------------

    def walk(self, picks: dict[str, str], verbose: bool = True):
        """Walk the selection wizard to a complete selection object.

        Option IDs are context-specific: editing a saved selection object
        without re-POSTing invalidates the IDs of every downstream step. So we
        always select ONE unselected step, POST, and re-read the result.

        `picks` maps stepName -> option text. Unlisted steps take the first
        option.
        """
        status, qs = self._json(f"/QuerySelection/{self.client_id}")
        if status != 200:
            raise RuntimeError(f"could not start wizard: {status} {qs}")
        while True:
            pending = [s for s in qs["selections"]
                       if not any(v.get("selected") for v in s["values"])]
            if not pending:
                return qs
            step = pending[0]
            want = picks.get(step["stepName"])
            target = next((v for v in step["values"] if v["text"] == want), None)
            if want and target is None and verbose:
                print(f"  ! {step['stepName']}: {want!r} not offered; "
                      f"using {step['values'][0]['text']!r}", file=sys.stderr)
            target = target or step["values"][0]
            target["selected"] = True
            if verbose:
                print(f"  {step['stepName']:<18} -> {target['text']}")
            status, qs = self._json("/QuerySelection", qs, "POST")
            if status != 200:
                raise RuntimeError(f"wizard step failed: {status} {qs}")

    def describe_wizard(self, picks: dict[str, str]):
        """Walk once and report every step with its full option list."""
        qs = self.walk(picks, verbose=False)
        for s in qs["selections"]:
            vals = s["values"]
            sel = [v["text"] for v in vals if v.get("selected")]
            print(f"\n--- step {s.get('stepNumber')}: {s['stepName']} "
                  f"(n={len(vals)}, multiselect={s.get('allowFieldMutiSelect')}) ---")
            print(f"    selected: {sel}")
            for v in vals:
                print(f"      {v['text']}")
        return qs

    # -- query execution ---------------------------------------------------

    def build_body(self, selection: dict, orgs: list[dict] | None = None,
                   statewide: bool = False):
        """Assemble a /Query/Run body.

        Any stale `queryHash` must be stripped: re-running a body that still
        carries one makes the job report Errored.
        """
        orgs = orgs or []
        body = {k: v for k, v in selection.items() if k != "queryHash"}
        body.update({
            "organizationIds": [o["id"] for o in orgs],   # Int64, not the GUID
            "selectedOrganizations": orgs,
            "stateFlag": bool(statewide),
            "queryProcessEngine": "ReportAsync",
            "drillDownStack": [],
            "dynamicFilter": [],
            "dynamicBreakdown": [],
            "bypassDefaultColumnVisibility": False,
            "bypassStateSelection": False,
        })
        return body

    def run(self, body: dict, poll_every: float = 3.0, max_polls: int = 40):
        """POST /Query/Run, poll if queued, return the ready payload.

        202 means queued and carries request.queryHash; poll /Query/Status
        (queryHash goes in the QUERY STRING, not the body) until Finished, then
        re-POST /Query/Run to collect the result.
        """
        status, d = self._json("/Query/Run", body, "POST")
        if status == 200:
            return d
        if status != 202:
            raise RuntimeError(f"/Query/Run failed: {status} {d}")
        qh = d["request"]["queryHash"]
        for _ in range(max_polls):
            time.sleep(poll_every)
            _, s = self._json(
                "/Query/Status?queryHash=" + urllib.parse.quote(qh), None, "POST")
            state = s.get("status") if isinstance(s, dict) else None
            if state == "Errored":
                raise RuntimeError(f"query errored: {json.dumps(s)[:300]}")
            if state == "Finished":
                break
        else:
            raise TimeoutError(f"query did not finish: {qh}")
        status, d = self._json("/Query/Run", body, "POST")
        return d

    def download_csv(self, body: dict) -> bytes:
        status, raw = self._call("/Query/Download", body, "POST")
        if status != 200:
            raise RuntimeError(f"/Query/Download failed: {status} {raw[:200]}")
        return raw


# --------------------------------------------------------------------------

DEFAULT_PICKS = {
    "assessment": "STAAR 3-8",
    "report": "Standard Summary",
    "administrations": "Spring 2024",
    "subjects": "Mathematics",
    "versions": "STAAR All",
    "grades": "Grade 5",
}


def cmd_map(c: PortalClient):
    print("Texas Assessment Research Portal API\n")
    print(f"  base       {API}")
    print(f"  clientId   {c.client_id}")
    info = c.client_info()
    print(f"  client     {json.dumps(info)}")
    print(f"\n  aggMin = {info.get('aggMin')} -> cells below this are masked\n")
    print("  organization levels:")
    for name, ordinal in LEVELS.items():
        _, d = c._json("/Organization/Query",
                       {"clientId": c.client_id, "level": [ordinal],
                        "pageNumber": 1, "pageSize": 1}, "POST")
        n = d.get("totalRecords") if isinstance(d, dict) else "?"
        sample = (d.get("data") or [{}])[0] if isinstance(d, dict) else {}
        print(f"    {ordinal} {name:<9} n={n:<7} "
              f"e.g. {sample.get('name','')} ({sample.get('entityExternalId','')})")


def cmd_orgs(c: PortalClient, level: str, search: str | None, limit: int):
    rows = c.organizations(level, search, max_pages=(1 if search else None))
    print(f"{len(rows)} {level}(s)" + (f" matching {search!r}" if search else ""))
    for o in rows[:limit]:
        parent = (o.get("parent") or {})
        print(f"  id={o['id']:<8} tea_id={o['entityExternalId']:<11} "
              f"{o['name'][:34]:<34} parent={parent.get('name','')[:24]}")
    if len(rows) > limit:
        print(f"  ... {len(rows)} total")


def cmd_query(c: PortalClient, args):
    picks = dict(DEFAULT_PICKS)
    for key in ("assessment", "report", "subjects", "versions", "grades"):
        if getattr(args, key, None):
            picks[key] = getattr(args, key)
    if args.admin:
        picks["administrations"] = args.admin

    print("Walking selection wizard:")
    selection = c.walk(picks)

    orgs = []
    if args.district or args.campus:
        tea_id = args.district or args.campus
        level = "district" if args.district else "campus"
        found = c.organizations(level, search=None, max_pages=None)
        orgs = [o for o in found if o.get("entityExternalId") == tea_id]
        if not orgs:
            raise SystemExit(f"no {level} with TEA id {tea_id}")
        print(f"\norganization: {orgs[0]['name']} "
              f"(id={orgs[0]['id']}, tea_id={orgs[0]['entityExternalId']})")

    body = c.build_body(selection, orgs, statewide=args.statewide)
    if args.dump_body:
        with open(args.dump_body, "w") as f:
            json.dump(body, f, indent=1)
        print(f"\nrequest body written to {args.dump_body}")
        print("Diff this against a real /Query/Run captured in browser DevTools "
              "to find the field that populates rows (PORTAL.md section 6).")

    print("\nRunning query...")
    result = c.run(body)
    table = (result.get("tables") or [{}])[0]
    print(f"  isError    {result.get('isError')}")
    print(f"  noDataRows {table.get('noDataRows')}")
    print(f"  columns    {len(table.get('columns') or [])}")
    print(f"  rows       {len(table.get('rows') or [])}")

    breakdowns = result.get("availalbleBreakdowns") or []
    if breakdowns:
        print(f"\n  {len(breakdowns)} breakdown dimensions available:")
        for b in breakdowns:
            print(f"    {b.get('fieldName')}")

    csv = c.download_csv(body)
    print(f"\n  CSV export: {len(csv):,} bytes")
    if args.out and len(csv) > 2:
        with open(args.out, "wb") as f:
            f.write(csv)
        print(f"  written to {args.out}")
    elif len(csv) <= 2:
        print("  (empty — see PORTAL.md section 6)")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--map", action="store_true", help="show API and client metadata")
    p.add_argument("--orgs", choices=list(LEVELS), help="list organizations")
    p.add_argument("--search", help="filter organizations by name")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--wizard", action="store_true", help="enumerate wizard steps")
    p.add_argument("--query", action="store_true", help="run a query")
    p.add_argument("--district", help="district by 6-digit TEA CDN, e.g. 227901")
    p.add_argument("--campus", help="campus by 9-digit TEA id")
    p.add_argument("--statewide", action="store_true")
    p.add_argument("--assessment"), p.add_argument("--report")
    p.add_argument("--admin", help='administration, e.g. "Spring 2024"')
    p.add_argument("--subjects"), p.add_argument("--versions"), p.add_argument("--grades")
    p.add_argument("--out", help="write CSV here")
    p.add_argument("--dump-body", help="write the /Query/Run body to this file")
    p.add_argument("--pace", type=float, default=0.5)
    a = p.parse_args(argv)

    c = PortalClient(pace=a.pace)
    if a.map:
        cmd_map(c)
    elif a.orgs:
        cmd_orgs(c, a.orgs, a.search, a.limit)
    elif a.wizard:
        c.describe_wizard(DEFAULT_PICKS)
    elif a.query:
        cmd_query(c, a)
    else:
        p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
