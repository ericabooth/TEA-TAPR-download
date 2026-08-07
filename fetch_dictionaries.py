#!/usr/bin/env python3
"""
Build the variable -> label crosswalk for every TAPR year.

This is the piece that makes cross-year label comparison possible. TAPR data
files ship labels only on the wizard route (SY 2024-25); every earlier year
ships bare variable names. The labels for those years exist, but not where you
would first look:

  NOT the glossary.  `/perfreport/tapr/<year>/glossary.pdf` is prose about
                     indicators and contains ZERO variable names. Verified by
                     searching for D_RATING, DFLCHART, DPETALLC, DAD_POST,
                     ASVAB_STATUS and DPETECOP in the 2019 and 2023 glossaries:
                     no hits in either.

  YES the data dictionary.  `/perfreport/tapr/<year>/datadict.pdf` is a matrix:
                     rows are indicators ("At Meets Grade Level or Above"),
                     columns are student groups, and each cell is the variable
                     name. That is exactly the mapping we need.

  ALSO the HTML dictionary endpoint, for recent years only. `dd_tapr_dictionary.sas`
                     returns structured Name/Type/Length/Description, but only
                     `dd=ref` and `dd=kg` answer for legacy years; the rest
                     require ccyy >= 2024.

So: PDF for 2013-2023, HTML endpoint for 2024-2025, and both are written to one
long CSV keyed on year x varname.

Usage:
    python3 fetch_dictionaries.py --years 2013-2025 --output dictionaries
"""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BROKER = "https://rptsvr1.tea.texas.gov/cgi/sas/broker"
TAPR_ROOT = "https://rptsvr1.tea.texas.gov/perfreport/tapr"
HEADERS = {"User-Agent": "TEA-TAPR-download/1.0 (research; eric.a.booth@gmail.com)"}

# dsname -> dd code, scraped from the Data Dictionary button on each step-3 page.
DD_CODES = {
    "REF": "ref", "STUD": "student", "STAF": "staff",
    "STAAR_GR3": "performance", "STAAR_GR4": "performance",
    "STAAR_GR5": "performance", "STAAR_GR6": "performance",
    "STAAR_GR7": "performance", "STAAR_GR8": "performance",
    "STAAR_GR38": "performance", "STAAR_ALL": "performance",
    "STAAR_EOC": "performance", "STAAR_SP": "progress",
    "PART": "participation", "BIL1": "bil", "BIL2": "bil",
    "KG": "kg", "PK": "pk", "DROP_ATT": "attendgrad",
    "COMP4": "attendgrad", "COMP5": "attendgrad", "COMP6": "attendgrad",
    "RHSP": "attendgrad", "FHSP": "attendgrad", "GRAD": "gradprofile",
    "GRAD1": "ccmr", "GRAD2": "ccmr", "GRAD3": "ccmr",
    "GRAD4": "ccmrrelated", "APIB": "ccmrrelated", "CAD": "ccmrrelated",
    "ADV": "postsec", "TXIHE": "postsec",
}

# A TAPR variable name: uppercase, at least 5 characters, no spaces.
VARNAME = re.compile(r"^[A-Z][A-Z0-9_]{4,}$")


def fetch(url: str, retries: int = 4) -> bytes:
    last = None
    for a in range(retries):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=HEADERS), timeout=180) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(4 * (a + 1))
    raise RuntimeError(f"{url}: {last}")


# --------------------------------------------------------------------------
# HTML dictionary (2024-2025)
# --------------------------------------------------------------------------

def parse_html_dict(html: str):
    """Pull Name/Type/Length/Description out of the tooltip blocks."""
    t = re.sub(r"<(script|style).*?</\1>", "", html, flags=re.S | re.I)
    out = []
    for m in re.finditer(r'<span class="tooltiptext".*?>(.*?)</span>', t, re.S | re.I):
        pairs = re.findall(
            r'<div class="dd_hdr">\s*([^<]*?)\s*</div>\s*<div class="dd_desc">\s*(.*?)\s*</div>',
            m.group(1), re.S | re.I)
        d = {k.rstrip(":").strip().lower():
             re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", v)).strip()
             for k, v in pairs}
        if d.get("name"):
            out.append(d)
    return out


def html_dictionary(year: int, level: str, pace: float, dd_subset=None):
    """Pull every dd dictionary that answers for this year.

    `dd_subset` lets the caller skip codes already known not to answer. For
    legacy years only `ref` and `kg` respond, so probing all 33 dsnames every
    year would be ~360 pointless requests.
    """
    rows, seen = [], set()
    items = [(ds, dd) for ds, dd in DD_CODES.items()
             if dd_subset is None or dd in dd_subset]
    for dsname, dd in items:
        url = BROKER + "?" + urllib.parse.urlencode({
            "_service": "marykay", "_program": "perfrept.perfmast.sas", "_debug": "0",
            "ccyy": year, "sumlev": level, "dsname": dsname, "dd": dd, "asvab": "",
            "prgopt": "reports/tapr/dd/dd_tapr_dictionary.sas"})
        try:
            recs = parse_html_dict(fetch(url).decode("latin-1", errors="replace"))
        except Exception as e:  # noqa: BLE001
            print(f"      {dsname:<11} {type(e).__name__}", flush=True)
            continue
        n_new = 0
        for r in recs:
            key = r["name"].upper()
            if key in seen:
                continue
            seen.add(key)
            n_new += 1
            rows.append({"year": year, "level": level, "varname": key,
                         "label": r.get("description", ""),
                         "vartype": r.get("type", ""), "varlen": r.get("length", ""),
                         "section": dd, "subsection": "", "measure_year": "",
                         "source": "html_dict"})
        if recs:
            print(f"      {dsname:<11} dd={dd:<13} {len(recs):>5} vars "
                  f"({n_new} new)", flush=True)
        time.sleep(pace)
    return rows


# --------------------------------------------------------------------------
# PDF data dictionary (2013-2023)
# --------------------------------------------------------------------------

def parse_datadict_pdf(path: Path, year: int, level_hint: str):
    """Parse the datadict.pdf matrix into varname -> label rows.

    Layout, stable across years:

        STAAR Performance Rates by Tested Grade, Subject, ...   <- section
           Grade 3 Reading                                      <- subsection
              At Approaches Grade Level or Above 2019 SDA03ARE1S19R DDA...  <- row

    A row is any line carrying two or more variable-name tokens. The label is
    whatever precedes the first token, minus a trailing 4-digit year (which is
    the measure year, captured separately). Section and subsection are tracked
    by indentation of the most recent non-row lines.
    """
    txt = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                         capture_output=True, text=True, timeout=300).stdout
    rows, section, subsection, last_label = [], "", "", ""
    for raw in txt.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        toks = line.split()
        names = [t for t in toks if VARNAME.match(t)]

        if len(names) >= 2:
            first = toks.index(names[0])
            label = re.sub(r"\s+", " ", " ".join(toks[:first])).strip()
            myear = ""
            m = re.search(r"\b(19|20)(\d{2})\s*$", label)
            if m:
                myear = m.group(0).strip()
                label = label[:m.start()].strip()
            # continuation rows repeat the prior indicator with a different year
            if not label:
                label = last_label
            else:
                last_label = label
            full = " > ".join(x for x in (section, subsection, label) if x)
            if not label:
                continue
            for n in names:
                rows.append({"year": year, "level": level_hint, "varname": n.upper(),
                             "label": full, "vartype": "", "varlen": "",
                             "section": section, "subsection": subsection,
                             "measure_year": myear, "source": "datadict_pdf"})
            continue

        # not a data row: treat as a heading, keyed on indentation
        indent = len(line) - len(line.lstrip())
        text = re.sub(r"\s+", " ", line.strip())
        if len(text) < 3 or VARNAME.match(text):
            continue
        if re.match(r"^(TEXAS EDUCATION AGENCY|Texas Academic|Page |\d+ of \d+)", text):
            continue
        # Section headings sit flush left; subsections are indented. The
        # threshold matters: `indent <= 3` also caught "   Grade 3 Reading"
        # and wiped the section it belonged under.
        if indent == 0:
            section, subsection = text, ""
        elif indent <= 6:
            subsection = text
    return rows


def pdf_dictionary(year: int, out_dir: Path, pace: float):
    rows = []
    for stem, hint in (("datadict", ""), ("datadict_addl", "")):
        url = f"{TAPR_ROOT}/{year}/{stem}.pdf"
        dest = out_dir / f"{stem}_{year}.pdf"
        if not dest.exists():
            try:
                data = fetch(url)
            except Exception as e:  # noqa: BLE001
                print(f"      {stem}.pdf  not available ({type(e).__name__})", flush=True)
                continue
            if len(data) < 5000 or not data[:5] == b"%PDF-":
                print(f"      {stem}.pdf  not a PDF ({len(data)}b)", flush=True)
                continue
            dest.write_bytes(data)
        try:
            r = parse_datadict_pdf(dest, year, hint)
        except Exception as e:  # noqa: BLE001
            print(f"      {stem}.pdf  parse failed: {type(e).__name__}: {e}", flush=True)
            continue
        uniq = len({x["varname"] for x in r})
        print(f"      {stem}.pdf  {dest.stat().st_size/1e6:>5.1f} MB  "
              f"{len(r):>6,} rows, {uniq:>6,} distinct vars", flush=True)
        rows.extend(r)
        time.sleep(pace)
    return rows


def verify_dd_codes(pace: float, ccyy: int = 2025) -> int:
    """Re-scrape the dsname -> dd mapping from TEA's own pages and diff it
    against the hardcoded DD_CODES table.

    DD_CODES was scraped from the Data Dictionary button on each wizard step-3
    page. If TEA renames a dd code, the harvest does not fail -- it silently
    returns zero variables for that dataset and label coverage quietly rots.
    Run this after any TEA site change, and whenever a harvest reports a
    dataset at 0 vars that used to answer.
    """
    hidden_q = re.compile(
        r"<input[^>]+type=['\"]hidden['\"][^>]+name=['\"]([^'\"]+)['\"][^>]+value=['\"]([^'\"]*)['\"]", re.I)
    hidden_u = re.compile(
        r"<input[^>]+type=['\"]hidden['\"][^>]+name=['\"]([^'\"]+)['\"][^>]+value=([^'\" >]+)", re.I)

    step2 = fetch(BROKER + "?" + urllib.parse.urlencode({
        "_service": "marykay", "_program": "perfrept.perfmast.sas", "_debug": "0",
        "ccyy": ccyy, "tapr": "all_d",
        "prgopt": "reports/tapr/dd/dd_tapr.sas"})).decode("latin-1", errors="replace")
    hf = dict(hidden_q.findall(step2))
    hf.update(dict(hidden_u.findall(step2)))
    dsnames = re.findall(
        r"<input[^>]+name=['\"]dsname['\"]\s+value=['\"]([^'\"]+)['\"]", step2, re.I)
    if not dsnames:
        print("FAIL: no dsnames on the step-2 page; the wizard itself changed")
        return 1

    live, drift = {}, []
    for ds in dsnames:
        data = dict(hf)
        data.update({"dsname": ds, "step": "3"})
        body = urllib.parse.urlencode(data).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    BROKER, data=body, headers=HEADERS), timeout=120) as r:
                t = r.read().decode("latin-1", errors="replace")
        except Exception as e:  # noqa: BLE001
            print(f"  {ds:<12} step-3 fetch failed: {type(e).__name__}")
            continue
        i = t.find("dd_tapr_dictionary")
        m = re.search(r"[?&]dd=([^&']*)", t[max(0, i - 400):i + 200]) if i >= 0 else None
        live[ds] = m.group(1) if m else None
        known = DD_CODES.get(ds)
        mark = "ok" if live[ds] == known else "DRIFT"
        if live[ds] != known:
            drift.append(ds)
        print(f"  {ds:<12} table={str(known):<14} live={str(live[ds]):<14} {mark}",
              flush=True)
        time.sleep(pace)

    gone = sorted(set(DD_CODES) - set(dsnames))
    new = sorted(set(dsnames) - set(DD_CODES))
    if gone:
        print(f"\n  dsnames in the table but no longer offered: {gone}")
    if new:
        print(f"  dsnames offered but missing from the table:  {new}")
    if drift or gone or new:
        print(f"\nFAIL: DD_CODES is out of date. Update the table in "
              f"{__file__} from the `live=` column above.")
        return 1
    print(f"\nPASS: all {len(dsnames)} dd codes match the live site (ccyy={ccyy})")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--years", default="2013-2025")
    p.add_argument("--output", default="dictionaries")
    p.add_argument("--levels", nargs="*", default=["D"],
                   help="levels for the HTML endpoint (PDF covers all levels at once)")
    p.add_argument("--pace", type=float, default=1.0)
    p.add_argument("--html-from", type=int, default=2024,
                   help="first year to use the HTML endpoint instead of the PDF")
    p.add_argument("--verify-dd", action="store_true",
                   help="diff the hardcoded dd-code table against the live "
                        "site and exit (~35 requests)")
    a = p.parse_args(argv)

    if a.verify_dd:
        return verify_dd_codes(a.pace)

    years = []
    for part in a.years.split(","):
        if "-" in part:
            lo, hi = part.split("-")
            years.extend(range(int(lo), int(hi) + 1))
        elif part.strip():
            years.append(int(part))

    out = Path(a.output)
    out.mkdir(parents=True, exist_ok=True)
    if not subprocess.run(["which", "pdftotext"], capture_output=True).stdout.strip():
        print("pdftotext not found (brew install poppler); PDF years will be skipped",
              file=sys.stderr)

    allrows = []
    legacy_dd = None          # which dd codes answer for pre-wizard years
    for y in years:
        print(f"\n{y}:", flush=True)
        if y >= a.html_from:
            for lv in a.levels:
                allrows.extend(html_dictionary(y, lv, a.pace))
            continue

        # Legacy: the PDF is the main source, but a few HTML dictionaries still
        # answer and carry variables the PDF omits entirely -- D_RATING is in
        # `dd=ref` and appears nowhere in datadict.pdf (verified: 0 hits).
        pdf_rows = pdf_dictionary(y, out, a.pace)
        if legacy_dd is None:
            probe = html_dictionary(y, a.levels[0], a.pace)
            legacy_dd = sorted({r["section"] for r in probe})
            print(f"      dd codes answering for legacy years: {legacy_dd}", flush=True)
            html_rows = probe
        else:
            html_rows = html_dictionary(y, a.levels[0], a.pace, dd_subset=legacy_dd)
        # HTML wins where both have it: it is authoritative and carries type/length
        have = {r["varname"] for r in html_rows}
        allrows.extend(html_rows)
        allrows.extend(r for r in pdf_rows if r["varname"] not in have)

    # one row per year x varname; PDF rows repeat a name across student-group
    # columns, and the label is identical for all of them
    seen, uniq = set(), []
    for r in allrows:
        k = (r["year"], r["varname"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)

    dest = out / "variable_labels.csv"
    cols = ["year", "level", "varname", "label", "vartype", "varlen",
            "section", "subsection", "measure_year", "source"]
    with open(dest, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in sorted(uniq, key=lambda r: (r["year"], r["varname"])):
            w.writerow({c: r.get(c, "") for c in cols})

    import collections
    byyear = collections.Counter(r["year"] for r in uniq)
    print(f"\n{'year':<8}{'labelled vars':>15}")
    for y in sorted(byyear):
        print(f"{y:<8}{byyear[y]:>15,}")
    print(f"\nwrote {dest}  ({len(uniq):,} year x variable rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
