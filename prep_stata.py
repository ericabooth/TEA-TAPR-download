#!/usr/bin/env python3
"""
Stage downloaded TAPR files for Stata, and write the index Stata reads.

Stata cannot read gzip, so this decompresses into a staging directory. It also
records, per file, the facts Stata would otherwise have to guess:

  n_header_rows   1 for the setpick routes, 2 for the wizard route (which puts
                  human labels on row 1 and variable names on row 2)
  varname_row     which row `import delimited, varnames()` should use
  has_labels      whether row 1 carries label text worth keeping
  id_var          the finest-grain identifier present (CAMPUS, else DISTRICT,
                  else REGION) -- chosen by PRIORITY, not position, because TEA
                  reordered the identifier block to COUNTY, REGION, DISTRICT
                  from 2021
  apostrophe_ids  whether the id column is written as '001902 (2023 and the
                  2024-25 Advanced route do this; it silently breaks merges)

Labels from row 1 are written to a separate long file (`labels.csv`) keyed on
year x level x dataset x varname, because that is exactly the input the
label-drift crosswalk needs and it is easier to build here than in Stata.

Usage:
    python3 prep_stata.py --input tapr_data --output stata_stage
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import re
import sys
from pathlib import Path

IDENT_PRIORITY = ("CAMPUS", "DISTRICT", "REGION", "SUMLEV")

# Stata reserved words and anything that cannot begin a variable name.
STATA_RESERVED = {
    "_all", "_b", "byte", "_coef", "_cons", "double", "float", "if", "in",
    "int", "long", "_n", "_N", "_pi", "_pred", "_rc", "_se", "_skip", "using",
    "with", "by", "class", "global", "local", "matrix", "scalar", "str",
}


def looks_like_varnames(row) -> bool:
    cells = [c.strip() for c in row if c.strip()]
    if not cells:
        return False
    ok = sum(1 for c in cells if re.fullmatch(r"[A-Za-z][A-Za-z0-9_|]{0,31}", c))
    return ok / len(cells) > 0.9


def stata_safe(name: str) -> str:
    """Make a TEA column name legal as a Stata variable name.

    TEA emits pipes in some header cells and, on the wizard route, names that
    collide with Stata reserved words once lowercased.
    """
    n = name.strip().lower()
    n = re.sub(r"[^a-z0-9_]+", "_", n).strip("_")
    if not n:
        n = "v"
    if n[0].isdigit():
        n = "v" + n
    if n in STATA_RESERVED:
        n = n + "_"
    return n[:32]


def process(path: Path, out_dir: Path):
    m = re.search(r"tapr_(\d{4})_([CDRSO])_(.+)\.csv(?:\.gz)?$", path.name)
    if not m:
        return None
    year, level, dataset = int(m.group(1)), m.group(2), m.group(3)

    raw = gzip.open(path, "rb").read() if path.suffix == ".gz" else path.read_bytes()
    rows = [r for r in csv.reader(io.StringIO(raw.decode("latin-1")))
            if r and any(c.strip() for c in r)]
    if len(rows) < 2:
        return None

    two = (len(rows) > 2 and looks_like_varnames(rows[1])
           and not looks_like_varnames(rows[0]))
    n_hdr = 2 if two else 1
    names = rows[n_hdr - 1]
    labels = rows[0] if two else [""] * len(names)
    data = rows[n_hdr:]

    safe = [stata_safe(n) for n in names]
    # de-duplicate after truncation/normalisation
    seen, final = {}, []
    for s in safe:
        if s in seen:
            seen[s] += 1
            s = f"{s[:29]}_{seen[s]}"
        else:
            seen[s] = 0
        final.append(s)

    upper = [n.strip().upper() for n in names]
    id_var = next((w for w in IDENT_PRIORITY if w in upper), None)
    id_idx = upper.index(id_var) if id_var else None

    # Which columns must stay STRING in Stata? Detect by CONTENT, not by a name
    # list. Any column holding a leading-zero numeric ("001902", "07") is a
    # zero-padded code, and destringing it both loses the padding and makes the
    # column numeric in some years and string in others -- which is exactly what
    # makes `append` fail with "long in master but str10 in using data".
    # A hardcoded name list missed PAIRCAMP; content detection cannot.
    ncols = len(names)
    zero_padded = [False] * ncols
    sample = data[:400]
    for r in sample:
        for j in range(min(len(r), ncols)):
            if not zero_padded[j]:
                v = r[j].strip().lstrip("'")
                if len(v) > 1 and v[0] == "0" and v.isdigit():
                    zero_padded[j] = True

    # Strip TEA's apostrophe text-guard from EVERY column that carries it, not
    # just the finest-grain id. Campus files apostrophe-guard DISTRICT, COUNTY
    # and REGION too, and stripping only CAMPUS leaves district as "'001902" --
    # 7 characters against a 6-character district id. That produced 35,932
    # bogus campus/district mismatches before this was fixed. The apostrophe is
    # an Excel text guard, never data.
    ncols_all = max(len(names), max((len(r) for r in data[:400]), default=0))
    apos_col = [False] * ncols_all
    for r in data[:400]:
        for j in range(min(len(r), ncols_all)):
            if not apos_col[j] and r[j][:1] == "'":
                apos_col[j] = True
    apostrophe = any(apos_col)
    if apostrophe:
        cols = [j for j, v in enumerate(apos_col) if v]
        for r in data:
            for j in cols:
                if j < len(r) and r[j][:1] == "'":
                    r[j] = r[j][1:]

    dest = out_dir / f"tapr_{year}_{level}_{dataset}.csv"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(final)
        w.writerows(data)

    lab_rows = [{"year": year, "level": level, "dataset": dataset,
                 "position": i + 1, "varname": final[i], "tea_name": names[i].strip(),
                 "tea_label": labels[i].strip() if i < len(labels) else ""}
                for i in range(len(final))]

    strvars = " ".join(final[j] for j in range(ncols) if zero_padded[j])

    return ({"year": year, "level": level, "dataset": dataset,
             "file": dest.name, "n_header_rows": n_hdr,
             "has_labels": int(two), "n_vars": len(final), "n_obs": len(data),
             "id_var": (id_var or "").lower(), "apostrophe_ids": int(apostrophe),
             "n_stringvars": sum(zero_padded), "string_vars": strvars},
            lab_rows)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", default="tapr_data")
    p.add_argument("--output", default="stata_stage")
    p.add_argument("--datasets", nargs="*", default=None,
                   help="limit to these dataset codes, e.g. REF PROF STAAR1")
    p.add_argument("--levels", nargs="*", default=None)
    p.add_argument("--labels", default="dictionaries/variable_labels.csv",
                   help="crosswalk from fetch_dictionaries.py; label .do files "
                        "are emitted only if this exists")
    a = p.parse_args(argv)

    src, out = Path(a.input), Path(a.output)
    out.mkdir(parents=True, exist_ok=True)
    files = sorted(src.rglob("*.csv.gz")) + sorted(src.rglob("tapr_*.csv"))

    # variable -> label by year, harvested by fetch_dictionaries.py. Applying
    # labels is what lets -datadictionary- detect a REWORDED label across years;
    # without them every legacy year is unlabelled and the change log can only
    # ever report added/dropped/retyped.
    xwalk = {}
    lp = Path(a.labels)
    if lp.exists():
        for r in csv.DictReader(open(lp)):
            xwalk[(r["year"], r["varname"].strip().lower())] = r["label"]
        print(f"label crosswalk: {len(xwalk):,} year x variable entries\n")
    else:
        print(f"no label crosswalk at {lp} (run fetch_dictionaries.py first)\n")

    index, labels = [], []
    for f in files:
        m = re.search(r"tapr_(\d{4})_([CDRSO])_(.+)\.csv(?:\.gz)?$", f.name)
        if not m:
            continue
        if a.datasets and m.group(3).upper() not in {d.upper() for d in a.datasets}:
            continue
        if a.levels and m.group(2) not in a.levels:
            continue
        r = process(f, out)
        if r:
            index.append(r[0])
            labels.extend(r[1])
            if xwalk:
                meta = r[0]
                do = out / f"labels_{meta['dataset']}_{meta['level']}_{meta['year']}.do"
                n_lab = 0
                with open(do, "w") as fh:
                    fh.write("* generated by prep_stata.py -- do not edit\n")
                    for lr in r[1]:
                        lab = (lr["tea_label"]
                               or xwalk.get((str(meta["year"]), lr["varname"]), ""))
                        if not lab:
                            continue
                        # Stata caps variable labels at 80 characters
                        lab = lab.replace('"', "'")[:80]
                        fh.write(f'capture label var {lr["varname"]} "{lab}"\n')
                        n_lab += 1
                meta["n_labelled"] = n_lab
            print(f"  staged {r[0]['file']:<34} {r[0]['n_obs']:>7,} x {r[0]['n_vars']:>5}"
                  f"  hdr={r[0]['n_header_rows']} id={r[0]['id_var'] or '?'}"
                  f" strvars={r[0]['n_stringvars']}"
                  f" labelled={r[0].get('n_labelled', 0)}"
                  f"{'  APOSTROPHE-IDS' if r[0]['apostrophe_ids'] else ''}", flush=True)

    if not index:
        # A fresh clone has no data directories. Fail loudly here rather than
        # writing an empty index and letting build_codebook.do fail obscurely.
        print(f"no input files matched under {src}/", file=sys.stderr)
        print("expected files like tapr_2019_D_REF.csv.gz -- download them "
              "first:", file=sys.stderr)
        print("  python3 tapr_download.py --years 2013-2025 --levels C D",
              file=sys.stderr)
        return 1

    # MERGE with any previous run rather than overwriting. Staging is normally
    # done in several passes (different datasets or levels), and an index that
    # only describes the last pass leaves Stata blind to everything staged
    # before it.
    idx_cols = ["year", "level", "dataset", "file", "n_header_rows", "has_labels",
                "n_vars", "n_obs", "id_var", "apostrophe_ids", "n_stringvars",
                "string_vars", "n_labelled"]
    merged = {}
    idx_path = out / "index.csv"
    if idx_path.exists():
        for r in csv.DictReader(open(idx_path)):
            merged[(r["year"], r["level"], r["dataset"])] = r
    for d in index:
        merged[(str(d["year"]), d["level"], d["dataset"])] = d
    rows = sorted(merged.values(),
                  key=lambda d: (d["dataset"], d["level"], int(d["year"])))
    with open(idx_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=idx_cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in idx_cols})

    lab_cols = ["year", "level", "dataset", "position", "varname",
                "tea_name", "tea_label"]
    lab_path = out / "labels.csv"
    lmerged = {}
    if lab_path.exists():
        for r in csv.DictReader(open(lab_path)):
            lmerged[(r["year"], r["level"], r["dataset"], r["position"])] = r
    for d in labels:
        lmerged[(str(d["year"]), d["level"], d["dataset"], str(d["position"]))] = d
    with open(lab_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=lab_cols)
        w.writeheader()
        for k in sorted(lmerged, key=lambda k: (k[2], k[1], int(k[0]), int(k[3]))):
            r = lmerged[k]
            w.writerow({c: r.get(c, "") for c in lab_cols})
    index = rows

    print(f"\nindex now covers {len(index)} files "
          f"({len(labels):,} variable rows added this pass)")
    print(f"  {out/'index.csv'}")
    print(f"  {out/'labels.csv'}")
    # `index` now mixes freshly-built dicts (int year) with rows re-read from a
    # previous index.csv (str year), so coerce before comparing or sorting.
    ap = [d for d in index if int(d["apostrophe_ids"])]
    if ap:
        pairs = sorted({(int(d["year"]), d["dataset"]) for d in ap})
        print(f"\n  apostrophe-guarded ids stripped in {len(ap)} files, "
              f"years {sorted({y for y, _ in pairs})}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
