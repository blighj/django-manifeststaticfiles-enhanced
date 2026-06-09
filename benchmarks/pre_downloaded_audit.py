#!/usr/bin/env python3
"""
pre_downloaded_audit.py

Tests Django's collectstatic against pre-downloaded real-world JS libraries,
using the same approach as pypi_audit.py: actually runs collectstatic for
each variant and measures coverage against Acorn ground truth.

Unlike evaluate_packages.py (which simulates regex/lexer behaviour in Python),
this script invokes real collectstatic, so results reflect what Django actually
does — including edge cases in URL substitution, the manifest, and post-processing.

Variants:
  django_42    Django 4.2 + ManifestStaticFilesStorage
                (support_js_module_import_aggregation=True)
  django_60    Django 6.0 + ManifestStaticFilesStorage
                (support_js_module_import_aggregation=True)
  django_61    Django 6.1 + ManifestStaticFilesStorage
                (support_js_module_import_aggregation=True)
  pkg_regex    EnhancedManifestStaticFilesStorage (use_lexer=False)
  pkg_lexer    EnhancedManifestStaticFilesStorage (use_lexer=True)

Setup (creates per-Django-version venvs — run once, same venvs as pypi_audit.py):
    python pre_downloaded_audit.py --setup

Run:
    python pre_downloaded_audit.py --local-dir /path/to/real_world_packages
    python pre_downloaded_audit.py --local-dir /path/to/real_world_packages \
        --lib vue lodash-es
    python pre_downloaded_audit.py --local-dir /path/to/real_world_packages \
        --output js_libs_compat.json
    python pre_downloaded_audit.py js_libs_compat.json --report-only

Note: .js and .mjs files are scored against Acorn ground truth for import coverage.
CSS files are included in the collectstatic run (url() rewrites are exercised) but
not scored — failures from missing url() targets will appear in the variant status.
"""

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
OUTPUTS_DIR = SCRIPTS_DIR / "outputs"
sys.path.insert(0, str(SCRIPTS_DIR))

from pypi_audit import (  # noqa: E402, F401
    VARIANT_LABELS,
    VARIANTS,
    _ensure_node_deps,
    _venv_python,
    check_venvs,
    print_report,
    run_ground_truth,
    run_variants,
    setup_venvs,
)

# ── Local lib helpers ─────────────────────────────────────────────────────────


def _list_js_files(lib_dir):
    """All .js and .mjs files under lib_dir."""
    lib_path = Path(lib_dir)
    return [
        str(p) for p in sorted(lib_path.rglob("*.js")) + sorted(lib_path.rglob("*.mjs"))
    ]


def _has_css_files(lib_dir):
    """Return True if any .css file exists under lib_dir."""
    return any(Path(lib_dir).rglob("*.css"))


_CSS_URL_RE = re.compile(r"""url\(\s*['"]?(\.\./[^'"\)]*?)['"]?\s*\)""")


def _css_references_outside_dir(lib_dir):
    """
    Return True if any CSS file under lib_dir contains a url(../...) reference
    that resolves to a path outside lib_dir.  Such packages are only partially
    downloaded (e.g. CSS without their companion font directory) and would cause
    SuspiciousFileOperation during collectstatic post-processing.
    """
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


def evaluate_local_lib(lib_name, lib_dir, variant_timeout=120, timing_runs=1):
    """
    Run collectstatic for each variant against files already on disk.

    STATICFILES_DIRS is pointed at lib_dir, so logical paths are relative to
    it (e.g. "cdn.js", "js/bootstrap.esm.js").  calculate_coverage reads the
    staticfiles.json manifest to see which imports were actually replaced.
    Coverage is measured for JS imports only (via Acorn ground truth); CSS files
    are included in the collectstatic run but not scored.

    Returns (variant_results_dict, skip_reason | None).
    """
    js_paths = _list_js_files(lib_dir)
    if not js_paths and not _has_css_files(lib_dir):
        return None, "no .js/.mjs/.css files found"
    if _css_references_outside_dir(lib_dir):
        return (
            None,
            "CSS references assets outside package directory (incomplete download)",
        )

    gt_map = run_ground_truth(js_paths)

    with tempfile.TemporaryDirectory(prefix="jslibs_run_") as run_dir:
        variant_results = run_variants(
            run_dir,
            [str(lib_dir)],
            gt_map,
            js_paths,
            Path(lib_dir),
            variant_timeout=variant_timeout,
            timing_runs=timing_runs,
        )

    return variant_results, None


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="JSON results file (used with --report-only)",
    )
    parser.add_argument(
        "--local-dir",
        metavar="DIR",
        help="Directory of pre-downloaded JS libraries (one subdirectory per library)",
    )
    parser.add_argument(
        "--lib",
        nargs="+",
        metavar="LIB",
        help="Only evaluate these libraries (by subdirectory name)",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="Write/update JSON results here (incremental — resumes if file exists)",
    )
    parser.add_argument(
        "--limit", type=int, metavar="N", help="Only evaluate the first N libraries"
    )
    parser.add_argument(
        "--setup", action="store_true", help="Create per-Django-version venvs and exit"
    )
    parser.add_argument(
        "--setup-check",
        action="store_true",
        help="Print Django version in each venv and exit",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Re-render an existing JSON results file without re-running",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-evaluate all entries even if already present in the output file",
    )
    parser.add_argument(
        "--timing-runs",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Number of collectstatic runs per variant"
            " for timing (median taken, default 1)"
        ),
    )
    parser.add_argument(
        "--variant-timeout",
        type=int,
        default=120,
        metavar="SEC",
        help="Seconds before a collectstatic run is killed (default 120)",
    )
    args = parser.parse_args()

    if args.setup:
        setup_venvs()
        return 0

    if args.setup_check:
        check_venvs()
        return 0

    if args.report_only:
        if not args.input:
            parser.error("--report-only requires an input JSON results file")
        data = json.loads(Path(args.input).read_text())
        print_report(data)
        return 0

    if not args.local_dir:
        parser.error("--local-dir is required (or use --setup / --report-only)")

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    _ensure_node_deps()

    local_root = Path(args.local_dir)
    if args.lib:
        lib_dirs = [(name, local_root / name) for name in args.lib]
    else:
        lib_dirs = sorted((d.name, d) for d in local_root.iterdir() if d.is_dir())
    if args.limit:
        lib_dirs = lib_dirs[: args.limit]

    # Load existing results for incremental runs
    all_results = {}
    if args.output and Path(args.output).exists():
        try:
            all_results = json.loads(Path(args.output).read_text())
            done = sum(
                1 for d in all_results.values() if "variants" in d or "skip" in d
            )
            print(
                f"Resuming: {done} libs already evaluated in {args.output}",
                file=sys.stderr,
            )
        except (json.JSONDecodeError, OSError):
            pass

    to_run = (
        list(lib_dirs)
        if args.force
        else [(name, d) for name, d in lib_dirs if name not in all_results]
    )
    if args.limit:
        to_run = to_run[: args.limit]
    total = len(to_run)
    new_count = 0

    for i, (lib_name, lib_dir) in enumerate(to_run, 1):
        print(f"  [{i}/{total}] {lib_name}…", file=sys.stderr, end="", flush=True)
        variants, skip_reason = evaluate_local_lib(
            lib_name,
            lib_dir,
            variant_timeout=args.variant_timeout,
            timing_runs=args.timing_runs,
        )
        if skip_reason:
            print(f" SKIP: {skip_reason}", file=sys.stderr)
            all_results[lib_name] = {"skip": skip_reason}
        else:
            summary = ", ".join(
                f"{VARIANT_LABELS[v]}:{variants[v]['status'].upper()}" for v in VARIANTS
            )
            print(f"  {summary}", file=sys.stderr)
            all_results[lib_name] = {"variants": variants}

        new_count += 1
        if args.output and new_count % 5 == 0:
            Path(args.output).write_text(json.dumps(all_results, indent=2))

    if args.output:
        Path(args.output).write_text(json.dumps(all_results, indent=2))
        print(f"\nResults saved to {args.output}", file=sys.stderr)

    print_report(all_results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
