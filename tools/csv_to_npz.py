#!/usr/bin/env python3
"""Convert Scope Grab CSV captures to the compact NPZ, checked value by value.

    python tools/csv_to_npz.py FOLDER [FOLDER ...]            # dry run: report only
    python tools/csv_to_npz.py FOLDER --write                  # write .npz beside each CSV
    python tools/csv_to_npz.py FOLDER --write --delete         # ...and remove CSVs that check out
    python tools/csv_to_npz.py FOLDER --repack --write         # re-store explicit times as a grid

Walks each folder recursively. A file is a capture if its header starts
`time_s,` and every other column is a CHn column; anything else (an analysis
table, a ledger export) is left alone. The .txt sidecar is never touched - the
NPZ is found by the same stem.

What a CSV still knows. The scope sent integer codes; the CSV holds them as
volts rounded to seven digits. Every value in a channel therefore sits on a
lattice v = y_orig + code * y_inc, and the lattice can be recovered from the
values alone: the smallest gap between two distinct values is one step, or a
whole number of them, and a least-squares fit of value against step number
pins y_inc and y_orig to far better than the rounding. The codes are then
stored as Scope Grab stores a fresh capture. Time is fitted the same way to
t = x_orig + i * x_inc.

Where that does not hold - an averaged file, whose mean of codes is not a code,
or anything else off the lattice - the column is stored as float64 volts
instead: still exact, just less compact.

The check, per file, before anything is deleted. The NPZ is read back through
scope_grab.load_capture (the reader the panel and, by copy, EOM-ILC use) and
every value compared with the CSV's: it has to agree to within half a unit in
the CSV's last printed digit - the CSV's own rounding, so the NPZ says exactly
what the CSV said - with a floor of a millionth of a step for values printed
near zero, where float noise in the original computation is larger than the
digit. Same columns, same row count. A file that fails keeps its CSV and is
listed at the end.

Files modified in the last --min-age minutes are skipped (default 30): a
capture the bench panel is writing right now is not one to convert. --exclude
skips a subfolder by name.

A log of every file - sizes, how each column was stored, the worst error as a
fraction of the allowed one - is written to FOLDER/csv_to_npz_<stamp>.log.
"""
import argparse
import datetime
import importlib.util
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

TIME_DIGITS = 9        # scope_grab.TIME_FMT = %.9e
VOLT_DIGITS = 6        # scope_grab.VOLT_FMT = %.6e
MAX_DIVISOR = 64       # how far below the smallest gap a step is looked for


def _sg():
    spec = importlib.util.spec_from_file_location(
        "scope_grab", os.path.join(REPO, "scope_grab.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scope_grab"] = mod
    spec.loader.exec_module(mod)
    return mod


def half_ulp(v, digits):
    """Half a unit in the last digit %.<digits>e printed for each value."""
    a = np.abs(v)
    out = np.zeros_like(a)
    nz = a > 0
    out[nz] = 0.5 * 10.0 ** (np.floor(np.log10(a[nz])) - digits)
    return out


def allowed(v, digits, step):
    return half_ulp(v, digits) * (1 + 1e-9) + 1e-6 * abs(step)


def _wfit(k, u, w):
    """Weighted least squares u = a + b k, centred so it is well conditioned."""
    km = np.dot(w, k) / w.sum()
    um = np.dot(w, u) / w.sum()
    kc = k - km
    b = float(np.dot(w * kc, u - um) / np.dot(w * kc, kc))
    return float(um - b * km), b


def _minimax(k, u, tol, a0, b0):
    """(a, b) minimising the worst |u - a - b k| / tol, starting from a
    least-squares (a0, b0); None without scipy. Least squares is not enough on
    its own: every printed value is off its true one by up to exactly one
    tolerance, so the band the line has to thread is as narrow as it can be,
    and a least-squares line MEASURED 1.08x over it at the ends of a 2000-code
    trigger channel. A linear programme in the corrections (da, db), scaled so
    every coefficient is order one, finds the line that fits if one does."""
    try:
        from scipy.optimize import linprog
    except ImportError:
        return None
    r = u - (a0 + b0 * k)
    sa = float(tol.min())
    sb = sa / max(float(np.abs(k).max()), 1.0)
    ca, cb = sa / tol, sb * k / tol
    one = np.ones_like(tol)
    A = np.vstack([np.column_stack([-ca, -cb, -one]),
                   np.column_stack([ca, cb, -one])])
    rhs = np.concatenate([-r / tol, r / tol])
    res = linprog([0, 0, 1], A_ub=A, b_ub=rhs,
                  bounds=[(None, None), (None, None), (0, None)], method="highs")
    if not res.success:
        return None
    return a0 + res.x[0] * sa, b0 + res.x[1] * sb


def fit_lattice(v, digits=VOLT_DIGITS):
    """(codes, (y_inc, y_ref, y_orig)) putting every value of `v` on one
    lattice within its printed precision, or None if no lattice does."""
    u = np.unique(v)
    if len(u) == 1:
        return np.zeros(len(v), dtype=np.uint16), (1.0, 0.0, float(u[0]))
    gaps = np.diff(u)
    # Not the smallest gap itself: a gap between two values printed at 5 V
    # carries their rounding (5e-7 each), and the smallest of thousands of
    # gaps is the one rounding shortened most - MEASURED 0.02% short at
    # 1 V/div, half a code off by the top of a 2000-code record. The median
    # of the gaps near the smallest is the step with that averaged out.
    g0 = float(np.median(gaps[gaps < 1.5 * gaps.min()]))
    # Each value weighted by how precisely it was printed. Unweighted, the
    # thousands of values near 5 V (each good to 5e-7) outvote the few near
    # 0 V (good to 5e-10) and the fit misses those by 11x their rounding -
    # MEASURED on a trigger channel at 1 V/div.
    w = 1.0 / np.maximum(half_ulp(u, digits), 1e-6 * g0) ** 2
    for m in range(1, MAX_DIVISOR + 1):
        s = g0 / m
        # Refine on the short gaps first - each is a few steps, so its count
        # of steps is certain even with s slightly off - then count the long
        # ones (a trigger edge can jump 2000 codes) with the refined step.
        short = gaps < 20 * g0
        n = np.rint(gaps[short] / s)
        if np.any(n < 1) or np.any(np.abs(gaps[short] / s - n) > 0.25):
            continue
        s = float(gaps[short].sum() / n.sum())
        k = None
        for _ in range(3):
            k_new = (np.concatenate([[0.0], np.cumsum(np.rint(gaps / s))])
                     if k is None else np.rint((u - a) / b))
            if k is not None and np.array_equal(k_new, k):
                break
            k = k_new
            if k[-1] >= 2 ** 32:
                return None
            a, b = _wfit(k, u, w)
        if not np.all(np.abs(u - (a + b * k)) <= allowed(u, digits, b)):
            mm = _minimax(k, u, allowed(u, digits, b), a, b)
            if mm is not None:
                a, b = mm
        if np.all(np.abs(u - (a + b * k)) <= allowed(u, digits, b)):
            codes = np.rint((v - a) / b).astype(np.int64)
            dtype = np.uint16 if codes.max() < 2 ** 16 else np.uint32
            return codes.astype(dtype), (b, 0.0, a)
    return None


def fit_time(t, digits=TIME_DIGITS):
    """(x_inc, x_orig, 0.0) if t is a clean grid to its printed precision."""
    i = np.arange(len(t), dtype=np.float64)
    if len(t) < 2:
        return None
    # Weighted for the same reason as fit_lattice: a time printed near the
    # trigger (-5.6e-09) is good to 1e-18 s, one at 10 ms only to 1e-12, and
    # an unweighted line MEASURED 9x outside the near-zero ones.
    dt = float(np.median(np.diff(t)))
    w = 1.0 / np.maximum(half_ulp(t, digits), 1e-6 * abs(dt)) ** 2
    a, b = _wfit(i, t, w)
    tol = allowed(t, digits, b)
    if not np.all(np.abs(t - (i * b + a)) <= tol):
        mm = _minimax(i, t, tol, a, b)
        if mm is not None:
            a, b = mm
    if np.all(np.abs(t - (i * b + a)) <= allowed(t, digits, b)):
        return (b, a, 0.0)
    return None


def is_capture_header(cols):
    return (len(cols) >= 2 and cols[0] == "time_s"
            and all(c.startswith("CH") and c.endswith("_V") for c in cols[1:]))


def read_csv(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        cols = fh.readline().strip().split(",")
    if not is_capture_header(cols):
        return cols, None
    try:
        import pandas as pd
        data = pd.read_csv(path, float_precision="round_trip").to_numpy(np.float64)
    except ImportError:
        data = np.loadtxt(path, delimiter=",", skiprows=1, ndmin=2)
    return cols, data


def convert_one(path, write, delete):
    """Returns a dict describing what happened. Never raises."""
    rec = {"path": path, "status": "", "csv_bytes": os.path.getsize(path)}
    try:
        sg = sys.modules.get("scope_grab") or _sg()
        cols, data = read_csv(path)
        if data is None:
            rec["status"] = "not a capture"
            return rec
        if data.shape[1] != len(cols):
            rec["status"] = f"FAIL {data.shape[1]} columns under {len(cols)} headers"
            return rec
        t = data[:, 0]
        x = fit_time(t)
        ys, how = [], []
        for j in range(1, len(cols)):
            fit = fit_lattice(data[:, j])
            if fit is None:
                ys.append(data[:, j])
                how.append("f64")
            else:
                codes, y = fit
                ys.append(sg.scope_profiles.Record((1.0, 0.0, 0.0), codes=codes, y=y))
                how.append(f"u{codes.dtype.itemsize * 8}")
        rec["how"] = ("t:grid" if x else "t:f64") + " " + " ".join(how)
        npz = path[:-4] + ".npz"
        rec["npz"] = npz
        if not write:
            rec["status"] = "dry run"
            return rec
        if os.path.exists(npz):
            # Only ours to replace if a previous run of this tool wrote it from
            # this very CSV; anything else is a capture in its own right.
            try:
                prev_cols, prev = sg.load_capture(npz)
            except Exception:
                prev = None
            if prev is None or prev.shape != data.shape:
                rec["status"] = "FAIL an .npz of that name exists and is not this CSV"
                return rec
        sg.write_npz(npz, cols, x if x else t, ys)
        rec["npz_bytes"] = os.path.getsize(npz)

        # the check
        got_cols, got = sg.load_capture(npz)
        if got_cols != cols or got.shape != data.shape:
            rec["status"] = "FAIL read back with different columns or shape"
            return rec
        dt = abs(x[0]) if x else float(np.median(np.abs(np.diff(t))))
        worst = float(np.max(np.abs(got[:, 0] - t) / allowed(t, TIME_DIGITS, dt)))
        for j in range(1, len(cols)):
            v = data[:, j]
            u = np.unique(v)
            step = float(np.diff(u).min()) if len(u) > 1 else 1.0
            worst = max(worst, float(np.max(np.abs(got[:, j] - v)
                                            / allowed(v, VOLT_DIGITS, step))))
        rec["worst"] = worst
        if not worst <= 1.0:
            rec["status"] = f"FAIL read-back off by {worst:.3g}x the CSV's rounding"
            return rec
        if delete:
            os.remove(path)
            rec["status"] = "converted, CSV removed"
        else:
            rec["status"] = "converted"
        return rec
    except Exception as exc:                      # report, never stop the batch
        rec["status"] = f"FAIL {type(exc).__name__}: {exc}"
        return rec


def repack_one(path, write, delete=False):
    """An NPZ this tool wrote with its times stored outright (`t`, because an
    earlier version's time fit was unweighted and missed): fit the grid now and
    store x, n instead. The stored times are the CSV's values exactly as parsed,
    so the check is the same one the CSV got; every other array is carried
    over untouched and must read back bit-identical."""
    rec = {"path": path, "status": "", "csv_bytes": os.path.getsize(path)}
    try:
        sg = sys.modules.get("scope_grab") or _sg()
        with np.load(path, allow_pickle=False) as z:
            if "format" not in z.files or "t" not in z.files:
                rec["status"] = "not a capture" if "format" not in z.files else "already a grid"
                return rec
            arrs = {k: z[k] for k in z.files}
        t = arrs["t"]
        x = fit_time(t)
        if x is None:
            rec["status"] = "time still not a grid - left as it is"
            return rec
        cols_before, before = sg.load_capture(path)
        rec["how"] = "t:grid (repacked)"
        if not write:
            rec["status"] = "dry run"
            return rec
        del arrs["t"]
        arrs["x"] = np.array(x, dtype=np.float64)
        arrs["n"] = np.array(len(t), dtype=np.int64)
        tmp = path + ".repack"
        with open(tmp, "wb") as fh:
            np.savez_compressed(fh, **arrs)
        cols, got = sg.read_npz(tmp)      # by name, not extension: tmp ends .repack
        worst = float(np.max(np.abs(got[:, 0] - t) / allowed(t, TIME_DIGITS, x[0])))
        rec["worst"] = worst
        if (cols != cols_before or got.shape != before.shape
                or not np.array_equal(got[:, 1:], before[:, 1:]) or not worst <= 1.0):
            os.remove(tmp)
            rec["status"] = f"FAIL repack check (time worst {worst:.3g})"
            return rec
        os.replace(tmp, path)
        rec["npz_bytes"] = os.path.getsize(path)
        rec["status"] = "repacked"
        return rec
    except Exception as exc:
        if os.path.exists(path + ".repack"):
            os.remove(path + ".repack")
        rec["status"] = f"FAIL {type(exc).__name__}: {exc}"
        return rec


def find_npzs(roots, exclude):
    out = []
    for root in roots:
        for d, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if x not in exclude]
            out += [os.path.join(d, f) for f in files if f.lower().endswith(".npz")]
    return sorted(out)


def find_csvs(roots, exclude, min_age_s):
    now = time.time()
    out, young = [], []
    for root in roots:
        for d, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if x not in exclude]
            for f in files:
                if f.lower().endswith(".csv"):
                    p = os.path.join(d, f)
                    (young if now - os.path.getmtime(p) < min_age_s else out).append(p)
    return sorted(out), sorted(young)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("folders", nargs="+")
    ap.add_argument("--write", action="store_true", help="write the .npz files")
    ap.add_argument("--delete", action="store_true",
                    help="remove each CSV whose .npz passed the check (needs --write)")
    ap.add_argument("--exclude", action="append", default=[],
                    help="subfolder name to skip (repeatable)")
    ap.add_argument("--min-age", type=float, default=30,
                    help="skip CSVs modified in the last this-many minutes")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--repack", action="store_true",
                    help="instead of converting CSVs, store the time grid of "
                         "NPZs an earlier version left with explicit times")
    a = ap.parse_args()
    if a.delete and not a.write:
        ap.error("--delete needs --write")

    if a.repack:
        files, young, job = find_npzs(a.folders, set(a.exclude)), [], repack_one
    else:
        files, young = find_csvs(a.folders, set(a.exclude), a.min_age * 60)
        job = convert_one
    print(f"{len(files)} {'NPZs' if a.repack else 'CSVs'} to look at"
          + (f"; {len(young)} skipped as younger than {a.min_age:g} min" if young else ""))
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(a.folders[0], f"csv_to_npz_{'repack_' if a.repack else ''}{stamp}.log")
    t0 = time.time()
    results = []
    with open(log_path, "w", encoding="utf-8") as log, \
            ProcessPoolExecutor(max_workers=a.jobs) as pool:
        log.write(f"csv_to_npz {stamp}  write={a.write} delete={a.delete} "
                  f"repack={a.repack}\n")
        for p in young:
            log.write(f"SKIPPED (modified < {a.min_age:g} min ago)\t{p}\n")
        futs = [pool.submit(job, p, a.write, a.delete) for p in files]
        for i, f in enumerate(futs, 1):
            r = f.result()
            results.append(r)
            line = (f"{r['status']}\t{r['csv_bytes'] / 1e6:.2f} MB"
                    + (f" -> {r['npz_bytes'] / 1e6:.2f} MB" if "npz_bytes" in r else "")
                    + (f"\t{r['how']}" if "how" in r else "")
                    + (f"\tworst {r['worst']:.3f}" if "worst" in r else "")
                    + f"\t{os.path.relpath(r['path'], a.folders[0])}")
            log.write(line + "\n")
            log.flush()
            if r["status"].startswith("FAIL") or i % 25 == 0 or i == len(futs):
                el = time.time() - t0
                print(f"[{i}/{len(futs)} {el / 60:.1f} min] {line}", flush=True)

    caps = [r for r in results if r["status"] not in ("not a capture", "already a grid")]
    ok = [r for r in caps if not r["status"].startswith("FAIL")]
    bad = [r for r in caps if r["status"].startswith("FAIL")]
    before = sum(r["csv_bytes"] for r in caps)
    after = sum(r.get("npz_bytes", 0) for r in ok)
    lattice = sum(1 for r in ok if "f64" not in r.get("how", ""))
    print(f"\n{len(caps)} captures, {len(ok)} ok ({lattice} fully on the code "
          f"lattice), {len(bad)} failed, "
          f"{len(results) - len(caps)} other CSVs left alone")
    if a.write:
        print(f"{before / 1e9:.2f} GB {'before' if a.repack else 'of CSV'} -> "
              f"{after / 1e9:.2f} GB of NPZ for the ones that passed")
    for r in bad:
        print(f"  KEPT  {r['path']}\n        {r['status']}")
    print(f"log: {log_path}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
