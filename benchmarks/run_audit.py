#!/usr/bin/env python3
"""
run_audit.py

Runs all three collectstatic audits then generates the combined HTML report.
Assumes all corpora have already been downloaded.

Usage:
    python run_audit.py --js-libs /path/to/js_libs --django-sites /path/to/django_sites
    python run_audit.py --js-libs /path/to/js_libs \
        --django-sites /path/to/django_sites --force
    python run_audit.py --js-libs /path/to/js_libs   # skips Django sites audit

Omitting --js-libs or --django-sites skips that audit. The combined HTML report
is only generated when all three output JSON files exist.
"""

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable


def run(label, cmd):
    print(f"\n{'─' * 60}")
    print(f"  {label}")
    print(f"  {' '.join(str(c) for c in cmd)}")
    print(f"{'─' * 60}\n", flush=True)
    result = subprocess.run(cmd, cwd=SCRIPTS_DIR)
    if result.returncode != 0:
        print(f"\nFailed (exit {result.returncode}) — stopping.", file=sys.stderr)
        sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--js-libs", metavar="DIR", help="Directory of pre-downloaded JS libraries"
    )
    parser.add_argument(
        "--django-sites",
        metavar="DIR",
        help="Directory of pre-downloaded Django site static files",
    )
    parser.add_argument(
        "--packages-csv",
        metavar="FILE",
        default=str(SCRIPTS_DIR / "packages_with_static.csv"),
        help="CSV of PyPI packages (default: packages_with_static.csv)",
    )
    parser.add_argument(
        "--output-dir",
        metavar="DIR",
        default=str(SCRIPTS_DIR / "outputs"),
        help="Directory for all output files (default: outputs/)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-evaluate all entries even if already in the output file",
    )
    parser.add_argument(
        "--limit", type=int, metavar="N", help="Only evaluate the first N PyPI packages"
    )
    parser.add_argument(
        "--timing-runs",
        type=int,
        default=1,
        metavar="N",
        help="Collectstatic runs per variant for timing (default 1)",
    )
    args = parser.parse_args()

    # Resolve to absolute paths so they survive cwd=SCRIPTS_DIR in subprocesses.
    out = Path(args.output_dir).resolve()
    packages_csv = Path(args.packages_csv).resolve()
    js_libs = Path(args.js_libs).resolve() if args.js_libs else None
    django_sites = Path(args.django_sites).resolve() if args.django_sites else None

    timing = ["--timing-runs", str(args.timing_runs)] if args.timing_runs != 1 else []
    force = ["--force"] if args.force else []

    # ── 1. PyPI packages ──────────────────────────────────────────────────────
    cmd = (
        [
            PYTHON,
            "pypi_audit.py",
            str(packages_csv),
            "--output",
            str(out / "results.json"),
        ]
        + force
        + timing
    )
    if args.limit:
        cmd += ["--limit", str(args.limit)]
    run("[1/4] PyPI package audit", cmd)

    # ── 2. JS libraries ───────────────────────────────────────────────────────
    if js_libs:
        cmd = (
            [
                PYTHON,
                "pre_downloaded_audit.py",
                "--local-dir",
                str(js_libs),
                "--output",
                str(out / "js_libs_compat.json"),
            ]
            + force
            + timing
        )
        run("[2/4] JS library audit", cmd)
    else:
        print("\n[2/4] JS library audit — skipped (pass --js-libs DIR to include)")

    # ── 3. Django sites ───────────────────────────────────────────────────────
    if django_sites:
        cmd = (
            [
                PYTHON,
                "pre_downloaded_audit.py",
                "--local-dir",
                str(django_sites),
                "--output",
                str(out / "django_sites_compat.json"),
            ]
            + force
            + timing
        )
        run("[3/4] Django sites audit", cmd)
    else:
        print(
            "\n[3/4] Django sites audit — skipped (pass --django-sites DIR to include)"
        )

    # ── 4. Combined HTML report ───────────────────────────────────────────────
    required = [
        out / "results.json",
        out / "js_libs_compat.json",
        out / "django_sites_compat.json",
    ]
    missing = [p.name for p in required if not p.exists()]
    if missing:
        print(f"\n[4/4] Combined report — skipped (missing: {', '.join(missing)})")
    else:
        run(
            "[4/4] Combined HTML report",
            [PYTHON, "generate_combined_report.py", "--output-dir", str(out)],
        )

    print("\nDone.")


if __name__ == "__main__":
    main()
