#!/usr/bin/env python3
"""
benchmark_branch.py

Benchmark a single Django branch or fork against the collectstatic test corpora
(JS libraries, Django sites, and optionally PyPI packages).  The branch is
installed into a dedicated venv in venvs/branch_<label>/ so it does not touch
the main project environment.

Accepted git specs
------------------
  https://github.com/blighj/django@ticket_36969    GitHub branch (most common)
  git+https://github.com/blighj/django@ticket_36969  Explicit git+ prefix
  https://gitlab.com/user/django@my-branch           Other git hosts
  /path/to/local/django                              Local checkout

Usage
-----
  python benchmark_branch.py https://github.com/blighj/django@ticket_36969

  # With explicit corpus paths (required if not using the defaults):
  python benchmark_branch.py https://github.com/blighj/django@ticket_36969 \\
      --js-libs files_cache/js_libs \\
      --django-sites files_cache/django_sites

  # Skip the slow PyPI audit (~45 min) for a quick result:
  python benchmark_branch.py https://github.com/blighj/django@ticket_36969 --no-pypi

  # Reinstall Django after updating the upstream branch:
  python benchmark_branch.py https://github.com/blighj/django@ticket_36969 --reinstall

  # Re-render saved results without re-running collectstatic:
  python benchmark_branch.py --report-only outputs/ticket_36969

The branch is always tested with support_js_module_import_aggregation=True via
a storages_shim subclass, matching how the other django_* variants are tested.
"""

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPTS_DIR.parent
VENVS_DIR = SCRIPTS_DIR / "venvs"
OUTPUTS_DIR = SCRIPTS_DIR / "outputs"
DEFAULT_JS_LIBS = SCRIPTS_DIR / "files_cache" / "js_libs"
DEFAULT_DJANGO_SITES = SCRIPTS_DIR / "files_cache" / "django_sites"
DEFAULT_PACKAGES_CSV = SCRIPTS_DIR / "packages_with_static.csv"

sys.path.insert(0, str(SCRIPTS_DIR))

from pypi_audit import (  # noqa: E402
    _SETTINGS_DJANGO,
    _STORAGES_SHIM,
    COMPANION_PACKAGES,
    _download_package,
    _ensure_node_deps,
    _extract_all,
    _extract_error_summary,
    calculate_coverage,
    classify_reason,
    find_static_dirs,
    has_css_files,
    list_js_files,
    run_ground_truth,
)

# ── Git spec normalisation ────────────────────────────────────────────────────


def _sanitize(s: str) -> str:
    """Make a string safe for use as a filesystem path component."""
    return re.sub(r"[^\w._-]", "_", s)[:64]


def _normalize_pip_spec(spec: str) -> str:
    """
    Convert a user-supplied git spec to something pip can install.

    - http(s):// URLs get a git+ prefix.
    - Local paths are passed through as-is (pip handles them directly).
    - Specs that already start with git+ are unchanged.
    """
    spec = spec.strip()
    if spec.startswith("git+"):
        return spec
    if spec.startswith("http://") or spec.startswith("https://"):
        return "git+" + spec
    # Local path — resolve to absolute so subprocesses find it regardless of cwd.
    p = Path(spec).expanduser().resolve()
    if p.exists():
        return str(p)
    return spec


def _derive_label(spec: str) -> str:
    """
    Derive a short, filesystem-safe label from the git spec.

    Priority:
      1. The @ref part if present (e.g. ticket_36969 from @ticket_36969).
      2. The last path component of the URL (e.g. the repo name).
    """
    s = spec.strip()
    if s.startswith("git+"):
        s = s[4:]
    if "@" in s:
        _, ref = s.rsplit("@", 1)
        return _sanitize(ref)
    path_part = s.split("?")[0].rstrip("/")
    return _sanitize(path_part.split("/")[-1])


# ── Venv management ───────────────────────────────────────────────────────────


def setup_branch_venv(pip_spec: str, label: str, reinstall: bool = False) -> Path:
    """
    Create venvs/branch_<label>/ and install Django from pip_spec.
    Returns the path to the venv's Python executable.
    """
    venv_path = VENVS_DIR / f"branch_{label}"
    python = venv_path / "bin" / "python"

    if python.exists() and not reinstall:
        result = subprocess.run(
            [str(python), "-c", "import django; print(django.__version__)"],
            capture_output=True,
            text=True,
        )
        ver = result.stdout.strip() or "?"
        print(f"Reusing venv for '{label}' (Django {ver}). Pass --reinstall to update.")
        return python

    if venv_path.exists():
        shutil.rmtree(venv_path)

    print(f"Creating venv for '{label}'…")
    subprocess.run([sys.executable, "-m", "venv", str(venv_path)], check=True)

    print(f"Installing Django from {pip_spec}…")
    result = subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", "--pre", pip_spec],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stderr or result.stdout, file=sys.stderr)
        sys.exit(result.returncode)

    ver = subprocess.run(
        [str(python), "-c", "import django; print(django.__version__)"],
        capture_output=True,
        text=True,
    )
    print(f"  Django {ver.stdout.strip()} installed.")
    return python


# ── Settings ──────────────────────────────────────────────────────────────────


def _write_settings(run_dir: Path, variant_name: str, static_dirs, output_dir: Path):
    """Write settings_<variant_name>.py + storages_shim.py into run_dir."""
    shim = run_dir / "storages_shim.py"
    if not shim.exists():
        shim.write_text(_STORAGES_SHIM)

    content = _SETTINGS_DJANGO.format(
        static_root=str(output_dir),
        static_dirs=list(static_dirs),
    )
    (run_dir / f"settings_{variant_name}.py").write_text(content)


# ── collectstatic runner ──────────────────────────────────────────────────────


def _run_collectstatic(
    python: Path, variant_name: str, run_dir: Path, output_dir: Path, timeout: int = 120
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(python),
        "-m",
        "django",
        "collectstatic",
        "--no-input",
        "--verbosity=1",
        f"--settings=settings_{variant_name}",
    ]
    t0 = time.perf_counter()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={
                **os.environ,
                "PYTHONPATH": str(run_dir),
                "DJANGO_SETTINGS_MODULE": f"settings_{variant_name}",
            },
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "stdout": "",
            "stderr": f"collectstatic exceeded {timeout}s",
            "returncode": -1,
            "elapsed_seconds": round(time.perf_counter() - t0, 3),
            "error_summary": [f"TIMEOUT: did not finish within {timeout}s"],
            "reason": "NA",
        }

    elapsed = round(time.perf_counter() - t0, 3)
    combined = result.stderr + result.stdout
    if result.returncode == 0:
        status = "pass"
    elif (
        "SourcemapWarning" in combined
        and "ValueError" not in combined
        and "CommandError" not in combined
    ):
        status = "warn"
    else:
        status = "fail"

    r = {
        "status": status,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
        "elapsed_seconds": elapsed,
        "error_summary": _extract_error_summary(result.stderr, result.stdout),
        "reason": "NA",
    }
    r["reason"] = classify_reason(r)
    return r


# ── CSS helper ────────────────────────────────────────────────────────────────

_CSS_URL_RE = re.compile(r"""url\(\s*['"]?(\.\./[^'"\)]*?)['"]?\s*\)""")


def _css_references_outside_dir(lib_dir: Path) -> bool:
    lib_path = Path(lib_dir).resolve()
    for css_file in lib_path.rglob("*.css"):
        try:
            text = css_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in _CSS_URL_RE.finditer(text):
            resolved = (css_file.parent / m.group(1)).resolve()
            try:
                resolved.relative_to(lib_path)
            except ValueError:
                return True
    return False


# ── Single-variant evaluators ─────────────────────────────────────────────────


def _evaluate_one(
    static_dirs,
    abs_js_paths,
    extract_root,
    python: Path,
    variant_name: str,
    timeout: int = 120,
) -> dict:
    """
    Run collectstatic for a single variant and return a result dict with
    coverage metrics appended (if the run passes and ground-truth data exists).
    """
    gt_map = run_ground_truth(abs_js_paths) if abs_js_paths else {}

    with tempfile.TemporaryDirectory(prefix="branch_run_") as run_dir:
        run_dir = Path(run_dir)
        out_dir = run_dir / "out"
        _write_settings(run_dir, variant_name, static_dirs, out_dir)
        result = _run_collectstatic(
            python, variant_name, run_dir, out_dir, timeout=timeout
        )

        if result["status"] in ("pass", "warn") and gt_map and abs_js_paths:
            gt_total, gt_replaced = calculate_coverage(
                out_dir, gt_map, abs_js_paths, Path(extract_root)
            )
            result["gt_total"] = gt_total
            result["gt_replaced"] = gt_replaced
            result["coverage_pct"] = (
                round(gt_replaced / gt_total * 100) if gt_total > 0 else None
            )

    return result


def evaluate_local_lib(
    lib_dir: Path, python: Path, variant_name: str, timeout: int = 120
):
    """Evaluate one pre-downloaded library.

    Returns (result, None) or (None, skip_reason).
    """
    lib_dir = Path(lib_dir)
    js_paths = [
        str(p) for p in sorted(lib_dir.rglob("*.js")) + sorted(lib_dir.rglob("*.mjs"))
    ]
    if not js_paths and not any(lib_dir.rglob("*.css")):
        return None, "no .js/.mjs/.css files found"
    if _css_references_outside_dir(lib_dir):
        return (
            None,
            "CSS references assets outside package directory (incomplete download)",
        )

    result = _evaluate_one(
        [str(lib_dir)],
        js_paths,
        lib_dir,
        python,
        variant_name,
        timeout=timeout,
    )
    return result, None


def evaluate_pypi_package(
    pkg_name: str, python: Path, variant_name: str, timeout: int = 120
):
    """Download + extract a PyPI package and evaluate.

    Returns (result, None) or (None, skip_reason).
    """
    with (
        tempfile.TemporaryDirectory(prefix="branch_dl_") as dl_dir,
        tempfile.TemporaryDirectory(prefix="branch_ex_") as ex_dir,
    ):

        pkg_file, err = _download_package(pkg_name, dl_dir)
        if pkg_file is None:
            return None, f"download failed: {err}"

        extract_root, err = _extract_all(pkg_file, ex_dir)
        if err:
            return None, err

        static_dirs = find_static_dirs(extract_root)

        for i, companion in enumerate(COMPANION_PACKAGES.get(pkg_name, [])):
            comp_dl = Path(dl_dir) / f"comp_{i}"
            comp_ex = Path(ex_dir) / f"comp_{i}"
            comp_dl.mkdir()
            comp_ex.mkdir()
            comp_file, _ = _download_package(companion, str(comp_dl))
            if comp_file:
                comp_root, _ = _extract_all(comp_file, str(comp_ex))
                if comp_root:
                    static_dirs += find_static_dirs(comp_root)

        abs_js_paths = list_js_files(find_static_dirs(extract_root))
        if not abs_js_paths and not has_css_files(static_dirs):
            return None, "no static files found"

        result = _evaluate_one(
            static_dirs,
            abs_js_paths,
            extract_root,
            python,
            variant_name,
            timeout=timeout,
        )
    return result, None


# ── Corpus runners ────────────────────────────────────────────────────────────


def run_local_corpus(
    corpus_dir: Path,
    python: Path,
    variant_name: str,
    label: str,
    output_file: Path,
    force: bool,
    limit: int | None,
    timeout: int = 120,
) -> dict:
    """Evaluate all subdirectories in a pre-downloaded corpus."""
    all_results = _load_existing(output_file, force)

    entries = sorted((d.name, d) for d in corpus_dir.iterdir() if d.is_dir())
    if not force:
        entries = [(n, d) for n, d in entries if n not in all_results]
    if limit:
        entries = entries[:limit]

    total = len(entries)
    for i, (name, lib_dir) in enumerate(entries, 1):
        print(f"  [{i}/{total}] {name}…", file=sys.stderr, end="", flush=True)
        result, skip = evaluate_local_lib(
            lib_dir, python, variant_name, timeout=timeout
        )
        if skip:
            print(f" SKIP: {skip}", file=sys.stderr)
            all_results[name] = {"skip": skip}
        else:
            print(f"  {_cell(result)}", file=sys.stderr)
            all_results[name] = {"variants": {label: result}}

        if i % 5 == 0:
            output_file.write_text(json.dumps(all_results, indent=2))

    output_file.write_text(json.dumps(all_results, indent=2))
    return all_results


def run_pypi_corpus(
    packages_csv: Path,
    python: Path,
    variant_name: str,
    label: str,
    output_file: Path,
    force: bool,
    limit: int | None,
    timeout: int = 120,
) -> dict:
    """Evaluate PyPI packages from a CSV list."""
    all_results = _load_existing(output_file, force)

    with open(packages_csv, newline="") as f:
        reader = csv.DictReader(f)
        all_packages = [row["pypi_name"] for row in reader if row.get("pypi_name")]

    packages = (
        all_packages if force else [p for p in all_packages if p not in all_results]
    )
    if limit:
        packages = packages[:limit]

    total = len(packages)
    for i, pkg in enumerate(packages, 1):
        print(f"  [{i}/{total}] {pkg}…", file=sys.stderr, end="", flush=True)
        result, skip = evaluate_pypi_package(pkg, python, variant_name, timeout=timeout)
        if skip:
            print(f" SKIP: {skip}", file=sys.stderr)
            all_results[pkg] = {"skip": skip}
        else:
            print(f"  {_cell(result)}", file=sys.stderr)
            all_results[pkg] = {"variants": {label: result}}

        if i % 5 == 0:
            output_file.write_text(json.dumps(all_results, indent=2))

    output_file.write_text(json.dumps(all_results, indent=2))
    return all_results


def _load_existing(output_file: Path, force: bool) -> dict:
    if not force and output_file.exists():
        try:
            data = json.loads(output_file.read_text())
            done = sum(1 for d in data.values() if "variants" in d or "skip" in d)
            print(f"Resuming: {done} already done in {output_file}", file=sys.stderr)
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


# ── Reporting ─────────────────────────────────────────────────────────────────


def _cell(r: dict) -> str:
    s = r["status"]
    cov = r.get("coverage_pct")
    cov_str = f" {cov:3d}%" if cov is not None else ""
    return {
        "pass": f"PASS{cov_str}",
        "warn": f"WARN{cov_str}",
        "fail": "FAIL",
        "timeout": "TIMEOUT",
        "error": "ERROR",
    }.get(s, s.upper())


def print_branch_report(all_results: dict, label: str, title: str = ""):
    evaluated = sum(1 for d in all_results.values() if "variants" in d)
    skipped = sum(1 for d in all_results.values() if "skip" in d)
    heading = f"Branch report: {label}"
    if title:
        heading += f"  ({title})"
    print(f"\n{heading}  —  {evaluated} evaluated, {skipped} skipped\n")

    name_w, col_w = 38, 16
    sep = "─" * (name_w + 2 + col_w)
    print(f"{'Entry':<{name_w}}  {label:^{col_w}}")
    print(sep)

    totals = {"pass": 0, "warn": 0, "fail": 0, "timeout": 0, "error": 0}

    for name, entry in all_results.items():
        disp = (name[: name_w - 1] + "…") if len(name) > name_w else name
        if "skip" in entry:
            print(f"{disp:<{name_w}}  {'SKIP':^{col_w}}")
            continue
        r = entry["variants"][label]
        print(f"{disp:<{name_w}}  {_cell(r):^{col_w}}")
        totals[r["status"]] = totals.get(r["status"], 0) + 1

    print(sep)
    summary = f"P{totals['pass']}/W{totals['warn']}/F{totals['fail']}"
    if totals.get("timeout"):
        summary += f"/T{totals['timeout']}"
    print(f"{'TOTALS (P/W/F)':<{name_w}}  {summary:^{col_w}}")

    # Failure details
    any_fail = False
    for name, entry in all_results.items():
        if "variants" not in entry:
            continue
        r = entry["variants"][label]
        if r["status"] not in ("fail", "warn", "timeout", "error"):
            continue
        if not any_fail:
            print("\n" + "═" * 60)
            print("FAILURE / WARNING DETAILS")
            print("═" * 60)
            any_fail = True
        print(f"\n  {name}  [{r['status'].upper()}]  reason: {r.get('reason', 'NA')}")
        for line in r.get("error_summary") or []:
            print(f"    {line}")

    if not any_fail:
        print("\nNo failures.")


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "spec",
        help=(
            "Git URL (with optional @branch) or local path"
            " of the Django fork to test,"
            " or an output directory when used with --report-only"
        ),
    )
    parser.add_argument(
        "--label",
        metavar="NAME",
        help="Human-readable label for this variant (default: derived from spec)",
    )
    parser.add_argument(
        "--js-libs",
        metavar="DIR",
        default=str(DEFAULT_JS_LIBS),
        help=f"Pre-downloaded JS libraries (default: {DEFAULT_JS_LIBS})",
    )
    parser.add_argument(
        "--django-sites",
        metavar="DIR",
        default=str(DEFAULT_DJANGO_SITES),
        help=f"Pre-downloaded Django sites (default: {DEFAULT_DJANGO_SITES})",
    )
    parser.add_argument(
        "--packages-csv",
        metavar="FILE",
        default=str(DEFAULT_PACKAGES_CSV),
        help=f"PyPI packages CSV (default: {DEFAULT_PACKAGES_CSV})",
    )
    parser.add_argument(
        "--output-dir",
        metavar="DIR",
        help="Where to write results (default: outputs/<label>)",
    )
    parser.add_argument(
        "--no-pypi",
        action="store_true",
        help=(
            "Skip the PyPI packages audit"
            " (saves ~45 min; JS libs and Django sites still run)"
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help=(
            "Evaluate only the first N entries per corpus"
            " (useful for quick smoke tests)"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-evaluate entries already present in the output files",
    )
    parser.add_argument(
        "--reinstall",
        action="store_true",
        help="Reinstall Django in the venv even if it already exists",
    )
    parser.add_argument(
        "--setup-only",
        action="store_true",
        help="Create the venv and exit without running benchmarks",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Re-render saved JSON results from an output directory without re-running",
    )
    parser.add_argument(
        "--variant-timeout",
        type=int,
        default=120,
        metavar="SEC",
        help="Seconds before a collectstatic run is killed (default: 120)",
    )
    args = parser.parse_args()

    # ── Report-only mode ──────────────────────────────────────────────────────
    if args.report_only:
        out_dir = Path(args.spec)
        label = args.label or out_dir.name
        for fname, title in [
            ("js_libs.json", "JS libraries"),
            ("django_sites.json", "Django sites"),
            ("pypi.json", "PyPI packages"),
        ]:
            p = out_dir / fname
            if p.exists():
                data = json.loads(p.read_text())
                print_branch_report(data, label, title=title)
            else:
                print(f"\n[{title}] — {p} not found, skipped.")
        return 0

    # ── Normal mode ───────────────────────────────────────────────────────────
    pip_spec = _normalize_pip_spec(args.spec)
    label = args.label or _derive_label(args.spec)
    # Variant name must be a valid Python identifier (used as a settings module name).
    variant_name = re.sub(r"\W", "_", label)

    print(f"Spec:    {pip_spec}")
    print(f"Label:   {label}")

    python = setup_branch_venv(pip_spec, label, reinstall=args.reinstall)

    if args.setup_only:
        print("Venv ready. Exiting (--setup-only).")
        return 0

    _ensure_node_deps()

    out_dir = Path(args.output_dir) if args.output_dir else OUTPUTS_DIR / label
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output:  {out_dir}\n")

    js_libs_dir = Path(args.js_libs)
    django_sites_dir = Path(args.django_sites)
    packages_csv = Path(args.packages_csv)

    # ── JS libraries ──────────────────────────────────────────────────────────
    if js_libs_dir.is_dir():
        print(f"\n{'─' * 60}")
        print("  [1] JS library audit")
        print(f"{'─' * 60}\n")
        js_results = run_local_corpus(
            js_libs_dir,
            python,
            variant_name,
            label,
            out_dir / "js_libs.json",
            force=args.force,
            limit=args.limit,
            timeout=args.variant_timeout,
        )
        print_branch_report(js_results, label, title="JS libraries")
    else:
        print(f"\n[1] JS library corpus not found at {js_libs_dir} — skipped.")

    # ── Django sites ──────────────────────────────────────────────────────────
    if django_sites_dir.is_dir():
        print(f"\n{'─' * 60}")
        print("  [2] Django sites audit")
        print(f"{'─' * 60}\n")
        site_results = run_local_corpus(
            django_sites_dir,
            python,
            variant_name,
            label,
            out_dir / "django_sites.json",
            force=args.force,
            limit=args.limit,
            timeout=args.variant_timeout,
        )
        print_branch_report(site_results, label, title="Django sites")
    else:
        print(f"\n[2] Django sites corpus not found at {django_sites_dir} — skipped.")

    # ── PyPI packages ─────────────────────────────────────────────────────────
    if args.no_pypi:
        print("\n[3] PyPI audit skipped (--no-pypi).")
    elif packages_csv.exists():
        print(f"\n{'─' * 60}")
        print("  [3] PyPI packages audit  (~45 min for full corpus)")
        print(f"{'─' * 60}\n")
        pypi_results = run_pypi_corpus(
            packages_csv,
            python,
            variant_name,
            label,
            out_dir / "pypi.json",
            force=args.force,
            limit=args.limit,
            timeout=args.variant_timeout,
        )
        print_branch_report(pypi_results, label, title="PyPI packages")
    else:
        print(f"\n[3] Packages CSV not found at {packages_csv} — PyPI audit skipped.")

    print(f"\nResults saved to {out_dir}/")
    print("Re-render at any time with:")
    print(f"  python benchmark_branch.py --report-only {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
