#!/usr/bin/env python3
"""
pypi_audit.py

Tests Django's collectstatic command against third-party package static files
for 5 different Django/storage configurations.

Variants
--------
  django_42    Django 4.2  + ManifestStaticFilesStorage
                (support_js_module_import_aggregation=True)
  django_60    Django 6.0  + ManifestStaticFilesStorage
                (support_js_module_import_aggregation=True)
  django_61    Django 6.1  + ManifestStaticFilesStorage
                (support_js_module_import_aggregation=True)
  pkg_regex    EnhancedManifestStaticFilesStorage (use_lexer=False)
  pkg_lexer    EnhancedManifestStaticFilesStorage (use_lexer=True)

Setup (creates per-version venvs — run once):
    python pypi_audit.py --setup

Evaluate packages:
    python pypi_audit.py ../packages_with_static.csv [--limit N] [--output results.json]
    python pypi_audit.py --packages djangorestframework wagtail

Re-render a saved JSON results file without re-running:
    python pypi_audit.py results.json --report-only
"""

import argparse
import csv
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import zipfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPTS_DIR.parent
OUTPUTS_DIR = SCRIPTS_DIR / "outputs"
VENVS_DIR = SCRIPTS_DIR / "venvs"
GROUND_TRUTH_JS = SCRIPTS_DIR / "ground_truth.js"

# Packages that must be downloaded alongside a primary package so their static
# files are available during collectstatic (e.g. a required companion app that
# provides files the primary package imports).
COMPANION_PACKAGES = {
    "django-jinja-knockout": ["djk-bootstrap5"],
}

# Variant → (venv subdirectory name, pip specifier, allow pre-releases)
# pkg_regex/pkg_lexer use the project venv, so no entry here.
DJANGO_VERSION_VENVS = {
    "django_42": ("django42", "django>=4.2,<4.3", False),
    "django_60": ("django60", "django>=6.0,<6.1", False),
    "django_61": ("django61", "django>=6.1a0,<6.2", True),
}

VARIANTS = ["django_42", "django_60", "django_61", "pkg_regex", "pkg_lexer"]
VARIANT_LABELS = {
    "django_42": "Django 4.2",
    "django_60": "Django 6.0",
    "django_61": "Django 6.1",
    "pkg_regex": "pkg regex",
    "pkg_lexer": "pkg lexer",
}

# ── Venv management ───────────────────────────────────────────────────────────


def _venv_python(variant):
    """Return the python executable path for a variant."""
    if variant in DJANGO_VERSION_VENVS:
        venv_name, *_ = DJANGO_VERSION_VENVS[variant]
        return VENVS_DIR / venv_name / "bin" / "python"
    # pkg_regex / pkg_lexer use the project venv
    return PROJECT_DIR / "venv" / "bin" / "python"


def setup_venvs():
    """Create (or update) the per-Django-version venvs."""
    VENVS_DIR.mkdir(exist_ok=True)
    for variant, (venv_name, pip_spec, allow_pre) in DJANGO_VERSION_VENVS.items():
        venv_path = VENVS_DIR / venv_name
        python = venv_path / "bin" / "python"

        if not python.exists():
            print(f"Creating venv for {VARIANT_LABELS[variant]}…")
            subprocess.run([sys.executable, "-m", "venv", str(venv_path)], check=True)
        else:
            print(f"Venv for {VARIANT_LABELS[variant]} already exists.")

        pip_cmd = [str(python), "-m", "pip", "install", "--quiet"]
        if allow_pre:
            pip_cmd.append("--pre")
        pip_cmd.append(pip_spec)
        print(f"  pip install '{pip_spec}'" + (" (--pre)" if allow_pre else ""))
        subprocess.run(pip_cmd, check=True)

        result = subprocess.run(
            [str(python), "-c", "import django; print(django.__version__)"],
            capture_output=True,
            text=True,
        )
        print(f"  Django {result.stdout.strip()} installed.")

    print("\nSetup complete.")
    print("Verify with: python pypi_audit.py --setup-check")


def check_venvs():
    """Print Django version installed in each venv."""
    for variant in VARIANTS:
        python = _venv_python(variant)
        if not python.exists():
            print(f"  {VARIANT_LABELS[variant]:<12}  MISSING — run --setup")
            continue
        result = subprocess.run(
            [str(python), "-c", "import django; print(django.__version__)"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(f"  {VARIANT_LABELS[variant]:<12}  Django {result.stdout.strip()}")
        else:
            print(
                f"  {VARIANT_LABELS[variant]:<12}  ERROR: {result.stderr.strip()[:60]}"
            )


# ── Package download + static file extraction ─────────────────────────────────


def _download_package(pkg_name, dest_dir):
    """pip download --no-deps into dest_dir. Returns (Path, error_str|None)."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--no-deps",
            "--quiet",
            "-d",
            str(dest_dir),
            pkg_name,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None, result.stderr.strip() or result.stdout.strip()
    files = [f for f in Path(dest_dir).iterdir() if f.is_file()]
    return (files[0], None) if files else (None, "no file downloaded")


def _extract_all(package_file, dest_dir):
    """Extract the wheel or sdist, returning the extraction root path."""
    dest = Path(dest_dir)
    if package_file.suffix in (".whl", ".zip"):
        with zipfile.ZipFile(package_file) as zf:
            zf.extractall(dest)
    elif ".tar" in package_file.name:
        with tarfile.open(package_file) as tf:
            tf.extractall(dest)
    else:
        return None, f"unknown archive format: {package_file.name}"
    return dest, None


def find_static_dirs(extract_root):
    """
    Find directories to add to STATICFILES_DIRS.

    For a standard Django app layout (app/static/...) we add each */static/
    directory so that files are accessible at their app-relative paths.
    Falls back to the extraction root if no static/ dirs exist.
    """
    root = Path(extract_root)
    static_dirs = []
    seen = set()
    for candidate in sorted(root.rglob("static")):
        if not candidate.is_dir():
            continue
        abs_str = str(candidate)
        # Skip nested static dirs (e.g., app/static/app/static/)
        if any(abs_str.startswith(s + "/") for s in seen):
            continue
        static_dirs.append(abs_str)
        seen.add(abs_str)

    return static_dirs if static_dirs else [str(root)]


def has_js_files(static_dirs):
    """Return True if any .js file exists under the given directories."""
    for d in static_dirs:
        if any(Path(d).rglob("*.js")):
            return True
    return False


def has_css_files(static_dirs):
    """Return True if any .css file exists under the given directories."""
    for d in static_dirs:
        if any(Path(d).rglob("*.css")):
            return True
    return False


def list_js_files(static_dirs):
    """Return absolute paths of all .js files under the given static directories."""
    paths = []
    for d in static_dirs:
        for p in Path(d).rglob("*.js"):
            if p.is_file():
                paths.append(str(p))
    return paths


def _logical_path(abs_path, base_dir):
    """
    Convert an absolute file path to the logical path Django's file finders expose.
    Strips everything up to and including the first 'static/' component so that
    e.g. rest_framework/static/rest_framework/js/foo.js → rest_framework/js/foo.js.
    """
    rel = Path(abs_path).relative_to(base_dir)
    parts = rel.parts
    for i, part in enumerate(parts):
        if part == "static":
            return "/".join(parts[i + 1 :])
    return str(rel).replace("\\", "/")


# ── Ground truth (Acorn) ──────────────────────────────────────────────────────


def _ensure_node_deps():
    if (SCRIPTS_DIR / "node_modules").exists():
        return
    print("Installing Node.js dependencies (acorn, acorn-walk)…", file=sys.stderr)
    subprocess.run(["npm", "install"], cwd=SCRIPTS_DIR, check=True)


def run_ground_truth(paths):
    """
    Run ground_truth.js on a list of absolute file paths.
    Returns {abs_path: {ok: bool, imports: [{kind, url, start, end}]}}.
    """
    if not paths:
        return {}
    proc = subprocess.Popen(
        ["node", str(GROUND_TRUTH_JS)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    proc.stdin.write("\n".join(paths) + "\n")
    proc.stdin.close()
    out = {}
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            out[data["path"]] = data
        except (json.JSONDecodeError, KeyError):
            continue
    proc.wait()
    return out


def _is_in_scope(imp):
    """True for imports with a JS extension that collectstatic should rewrite."""
    if imp.get("kind") == "dynamic_import_nonliteral":
        return False
    url = imp.get("url") or ""
    if not (url.startswith("./") or url.startswith("../") or url.startswith("/")):
        return False
    bare = url.split("?")[0].split("#")[0]
    return os.path.splitext(bare)[1].lower() in {".js", ".mjs"}


# ── Coverage calculation ──────────────────────────────────────────────────────


def _output_gt_urls(output_file_path):
    """
    Run ground_truth.js on an output file and return the set of import URLs
    it still contains.  Returns an empty set on any error.
    """
    result = run_ground_truth([str(output_file_path)])
    data = result.get(str(output_file_path), {})
    return {imp["url"] for imp in data.get("imports", []) if _is_in_scope(imp)}


def calculate_coverage(output_dir, gt_map, abs_js_paths, extract_root):
    """
    After a successful collectstatic run, measure what fraction of ground-truth
    imports were actually replaced in the output files.

    Reads staticfiles.json from output_dir to map each input logical path to its
    hashed output path, then runs ground_truth.js on the output file to get the
    set of URLs still present as actual import statements.  A URL that no longer
    appears as an import was replaced; one that still does was not.

    This avoids false negatives from URLs that appear as plain strings elsewhere
    in the file (e.g. Vite's __vite__mapDeps preload array).

    Returns (gt_total, gt_replaced).  Both are 0 if the manifest is unreadable.
    """
    manifest_path = Path(output_dir) / "staticfiles.json"
    if not manifest_path.exists():
        return 0, 0

    try:
        manifest = json.loads(manifest_path.read_text())
        paths_map = manifest.get("paths", {})
    except (json.JSONDecodeError, OSError):
        return 0, 0

    gt_total = 0
    gt_replaced = 0

    for abs_path in abs_js_paths:
        logical = _logical_path(abs_path, extract_root)
        gt_data = gt_map.get(abs_path)
        if not gt_data or not gt_data.get("ok"):
            continue

        in_scope = [g for g in gt_data.get("imports", []) if _is_in_scope(g)]
        if not in_scope:
            continue

        hashed_name = paths_map.get(logical)
        if not hashed_name:
            gt_total += len(in_scope)
            continue

        output_file = Path(output_dir) / hashed_name
        if not output_file.exists():
            gt_total += len(in_scope)
            continue

        # URLs still present as imports in the output file.
        still_imported = _output_gt_urls(output_file)

        for g in in_scope:
            url = g.get("url", "")
            if not url:
                continue
            gt_total += 1
            if url not in still_imported:
                gt_replaced += 1

    return gt_total, gt_replaced


# ── Settings file generation ──────────────────────────────────────────────────

# storages_shim.py is written once per run alongside the settings files.
# It defines a storage subclass with support_js_module_import_aggregation=True
# for use by the django_* variants.  The pkg_* variants use OPTIONS instead.
_STORAGES_SHIM = textwrap.dedent("""\
    from django.contrib.staticfiles.storage import ManifestStaticFilesStorage

    class AuditStorage(ManifestStaticFilesStorage):
        support_js_module_import_aggregation = True
""")

_SETTINGS_DJANGO = textwrap.dedent("""\
    INSTALLED_APPS = [
        "django.contrib.staticfiles",
        "django.contrib.admin",
        "django.contrib.auth",
        "django.contrib.contenttypes",
    ]
    STATIC_URL = "/static/"
    STATIC_ROOT = {static_root!r}
    STATICFILES_DIRS = {static_dirs!r}
    DATABASES = {{}}
    DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

    STORAGES = {{
        "default": {{"BACKEND": "django.core.files.storage.FileSystemStorage"}},
        "staticfiles": {{"BACKEND": "storages_shim.AuditStorage"}},
    }}
""")

_SETTINGS_PKG = textwrap.dedent("""\
    INSTALLED_APPS = [
        "django_manifeststaticfiles_enhanced",
        "django.contrib.staticfiles",
        "django.contrib.admin",
        "django.contrib.auth",
        "django.contrib.contenttypes",
    ]
    STATIC_URL = "/static/"
    STATIC_ROOT = {static_root!r}
    STATICFILES_DIRS = {static_dirs!r}
    DATABASES = {{}}
    DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

    STORAGES = {{
        "default": {{"BACKEND": "django.core.files.storage.FileSystemStorage"}},
        "staticfiles": {{
            "BACKEND": (
                "django_manifeststaticfiles_enhanced.storage"
                ".EnhancedManifestStaticFilesStorage"
            ),
            "OPTIONS": {{
                "support_js_module_import_aggregation": True,
                "use_lexer": {use_lexer!r},
                "sourcemap_strict": False,
            }},
        }},
    }}
""")

_SETTINGS_BASELINE = textwrap.dedent("""\
    INSTALLED_APPS = [
        "django.contrib.staticfiles",
        "django.contrib.admin",
        "django.contrib.auth",
        "django.contrib.contenttypes",
    ]
    STATIC_URL = "/static/"
    STATIC_ROOT = {static_root!r}
    STATICFILES_DIRS = {static_dirs!r}
    DATABASES = {{}}
    DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

    STORAGES = {{
        "default": {{"BACKEND": "django.core.files.storage.FileSystemStorage"}},
        "staticfiles": {{
            "BACKEND": (
                "django.contrib.staticfiles.storage"
                ".ManifestStaticFilesStorage"
            ),
        }},
    }}
""")


def write_settings(run_dir, variant, static_dirs, output_dir):
    """Write settings_{variant}.py + storages_shim.py to run_dir."""
    run_dir = Path(run_dir)
    shim_path = run_dir / "storages_shim.py"
    if not shim_path.exists():
        shim_path.write_text(_STORAGES_SHIM)

    static_root = str(output_dir)
    static_dirs_list = list(static_dirs)

    if variant in ("django_42", "django_60", "django_61"):
        content = _SETTINGS_DJANGO.format(
            static_root=static_root,
            static_dirs=static_dirs_list,
        )
    elif variant == "baseline":
        content = _SETTINGS_BASELINE.format(
            static_root=static_root,
            static_dirs=static_dirs_list,
        )
    else:
        content = _SETTINGS_PKG.format(
            static_root=static_root,
            static_dirs=static_dirs_list,
            use_lexer=(variant == "pkg_lexer"),
        )

    settings_path = run_dir / f"settings_{variant}.py"
    settings_path.write_text(content)
    return settings_path


# ── collectstatic runner ──────────────────────────────────────────────────────

_USEFUL_KEYWORDS = (
    "Post-processing",
    "CommandError:",
    "ValueError:",
    "SourcemapWarning:",
    "could not be found",
    "contains this reference",
    "on line",
)


def _extract_error_summary(stderr, stdout):
    """
    Filter collectstatic stderr/stdout down to the lines that explain what failed.
    Strips the traceback boilerplate so the key error is immediately visible.
    """
    summary = []
    for line in (stderr + "\n" + stdout).splitlines():
        stripped = line.strip()
        if stripped and any(kw in stripped for kw in _USEFUL_KEYWORDS):
            summary.append(stripped)
    return summary


_IMAGE_EXTS = {".png", ".gif", ".jpg", ".jpeg", ".webp", ".ico", ".bmp"}
_FONT_EXTS = {".eot", ".woff", ".woff2", ".ttf", ".otf"}
_JS_EXTS = {".js", ".mjs"}


def classify_reason(vdata):
    """
    Inspect a variant result dict and return a human-readable reason string.

    Reasons that contain "package problem" are treated as package-level issues
    by the HTML report (yellow rows); everything else is a library limitation.

    When a sourcemap warning was also emitted alongside a different primary
    failure (e.g. the package variant skipped the missing sourcemap but then
    hit a missing font), both reasons are included: "missing sourcemap, <reason>".
    """
    status = vdata.get("status", "")
    if status in ("pass", "warn", "skip", "timeout"):
        return "NA"

    errs = vdata.get("error_summary", [])
    # Django 4.2/6.0 emit the Python source of the format string before the
    # formatted message; skip that line and look at the rest.
    lines = [
        line
        for line in (errs if isinstance(errs, list) else [str(errs)])
        if "%s" not in line and "%r" not in line
    ]
    text = " | ".join(lines)

    # Circular / post-process loop
    if re.search(r"circular|All.*failed|Max.*post.process", text, re.I):
        reason = "circular references"

    # Build-artifact warning (our BuildArtifactWarning message)
    elif re.search(r"build artifact", text, re.I):
        reason = "build artifact import"

    # UTF-8 / binary file
    elif re.search(r"utf-?8.*codec|codec.*utf-?8|can't decode", text, re.I):
        reason = "binary/non-UTF-8 file"

    else:
        # Missing file — classify by extension.
        # Use .*? anchored to "could not be found" so embedded quotes in the
        # path (e.g. CSS url() delimiters that got captured) don't truncate it.
        m = re.search(r"The file '(.*?)' could not be found", text)
        if m:
            path = m.group(1)
            # Regex false positive: the captured "path" is actually minified JS/CSS.
            # Parentheses and newlines are reliable signals; quotes are not —
            # old Django (4.2/6.0) captures URL delimiters as part of the path.
            if (
                len(path) > 120
                or "\n" in path  # real newline
                or r"\n" in path  # literal backslash-n from minified code
                or "(" in path
            ):  # JS token
                reason = "regex matched inside minified JS"
            else:
                # Strip surrounding quotes that old Django may have captured.
                ext = os.path.splitext(path.strip("\"'"))[1].lower()
                if ext == ".map":
                    reason = "missing sourcemap"
                elif ext in _IMAGE_EXTS:
                    reason = "missing image file"
                elif ext in _FONT_EXTS:
                    reason = "missing font file"
                elif ext in _JS_EXTS:
                    reason = "missing JS file"
                elif ext == ".css":
                    reason = "missing CSS file"
                elif not ext:
                    # Use the "contains this reference" snippet (present in
                    # django_61 and pkg variants) to distinguish relative
                    # imports from bare module specifiers.
                    ref_m = re.search(r"contains this reference '(.*?)' on line", text)
                    if ref_m:
                        ref_snippet = ref_m.group(1)
                        if ref_snippet.startswith(("./", "../")) or re.search(
                            r"""[\"'](\./|\.\./)[^\"']*[\"']""", ref_snippet
                        ):
                            reason = "extensionless relative import"
                        else:
                            reason = "bare module specifier"
                    else:
                        reason = "NA"
                else:
                    reason = f"missing {ext} file"
        else:
            reason = "NA"

    # If a sourcemap warning was also emitted alongside a different primary
    # failure (e.g. the package variant skipped the missing sourcemap but then
    # hit a different error), surface both.  The pkg variants emit a "sourcemap
    # reference … could not be resolved, leaving as-is" line to stdout rather
    # than raising, so check for that phrase in the full output.
    combined = vdata.get("stderr", "") + vdata.get("stdout", "")
    if reason not in ("NA", "missing sourcemap") and "sourcemap reference" in combined:
        return f"missing sourcemap, {reason}"
    return reason


def run_collectstatic(variant, run_dir, output_dir, timeout=120):
    """
    Run collectstatic for one variant.

    Returns:
        {
            "status": "pass" | "fail" | "timeout" | "error",
            "stdout": str,
            "stderr": str,
            "returncode": int,
        }
    """
    python = _venv_python(variant)
    if not python.exists():
        return {
            "status": "error",
            "stdout": "",
            "stderr": f"Python not found: {python}  (run --setup first)",
            "returncode": -1,
            "error_summary": [f"Python not found: {python}  (run --setup first)"],
        }

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    cmd = [
        str(python),
        "-m",
        "django",
        "collectstatic",
        "--no-input",
        "--verbosity=1",
        f"--settings=settings_{variant}",
    ]
    if variant in ("pkg_regex", "pkg_lexer"):
        cmd.append("--parallel=1")

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
                "DJANGO_SETTINGS_MODULE": f"settings_{variant}",
            },
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "stdout": "",
            "stderr": f"collectstatic exceeded {timeout}s timeout",
            "returncode": -1,
            "elapsed_seconds": time.perf_counter() - t0,
            "error_summary": [
                f"TIMEOUT: collectstatic did not finish within {timeout}s"
            ],
        }
    elapsed = time.perf_counter() - t0

    if result.returncode == 0:
        status = "pass"
    else:
        combined = result.stderr + result.stdout
        # SourcemapWarning is a UserWarning subclass; Django's management command
        # treats any yielded exception as a failure and exits non-zero even though
        # the package intends it as a warning. If the only issue is sourcemap
        # references (no ValueError / CommandError), report it as "warn" not "fail".
        if (
            "SourcemapWarning" in combined
            and "ValueError" not in combined
            and "CommandError" not in combined
        ):
            status = "warn"
        else:
            status = "fail"

    result_dict = {
        "status": status,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
        "elapsed_seconds": round(elapsed, 3),
        "error_summary": _extract_error_summary(result.stderr, result.stdout),
        "reason": "NA",
    }
    result_dict["reason"] = classify_reason(result_dict)
    return result_dict


def _run_timed(variant, run_dir, output_dir, timeout, runs=1):
    """Run collectstatic up to `runs` times.

    Returns result with median elapsed_seconds.
    """
    times = []
    result = None
    output_path = Path(output_dir)
    for _ in range(max(1, runs)):
        if output_path.exists():
            shutil.rmtree(output_path)
        result = run_collectstatic(variant, run_dir, output_dir, timeout)
        times.append(result.get("elapsed_seconds", 0))
        if result["status"] in ("timeout", "error"):
            break
    result = dict(result)
    result["elapsed_seconds"] = round(statistics.median(times), 3)
    if len(times) > 1:
        result["elapsed_runs"] = [round(t, 3) for t in times]
    return result


# ── Variant runner ────────────────────────────────────────────────────────────


def run_variants(
    run_dir,
    static_dirs,
    gt_map,
    abs_js_paths,
    extract_root,
    variant_timeout=120,
    timing_runs=1,
):
    """Run baseline + all variants; return variant_results dict."""
    baseline_output_dir = Path(run_dir) / "out_baseline"
    write_settings(run_dir, "baseline", static_dirs, baseline_output_dir)
    baseline_result = _run_timed(
        "baseline",
        run_dir,
        baseline_output_dir,
        timeout=variant_timeout,
        runs=timing_runs,
    )
    baseline_elapsed = baseline_result.get("elapsed_seconds", 0)

    variant_results = {}
    for variant in VARIANTS:
        output_dir = Path(run_dir) / f"out_{variant}"
        write_settings(run_dir, variant, static_dirs, output_dir)
        result = _run_timed(
            variant, run_dir, output_dir, timeout=variant_timeout, runs=timing_runs
        )

        if result["status"] in ("pass", "warn") and gt_map:
            gt_total, gt_replaced = calculate_coverage(
                output_dir, gt_map, abs_js_paths, extract_root
            )
            result["gt_total"] = gt_total
            result["gt_replaced"] = gt_replaced
            result["coverage_pct"] = (
                round(gt_replaced / gt_total * 100) if gt_total > 0 else None
            )

        if baseline_elapsed:
            result["overhead_pct"] = round(
                (result["elapsed_seconds"] - baseline_elapsed) / baseline_elapsed * 100
            )

        variant_results[variant] = result

    return variant_results


# ── Per-package evaluation ────────────────────────────────────────────────────


def evaluate_package(pkg_name, variant_timeout=120, timing_runs=1):
    """
    Download the package, find its static files, run collectstatic for each
    variant, and return a results dict.

    Returns (result_dict, skip_reason | None).
    result_dict keys: variant → {status, stdout, stderr, returncode, error_summary}
    """
    with (
        tempfile.TemporaryDirectory(prefix="compat_dl_") as dl_dir,
        tempfile.TemporaryDirectory(prefix="compat_ex_") as ex_dir,
        tempfile.TemporaryDirectory(prefix="compat_run_") as run_dir,
    ):

        # 1. Download
        pkg_file, err = _download_package(pkg_name, dl_dir)
        if pkg_file is None:
            return None, f"download failed: {err}"

        # 2. Extract
        extract_root, err = _extract_all(pkg_file, ex_dir)
        if err:
            return None, err

        # 3. Find static dirs and JS files
        static_dirs = find_static_dirs(extract_root)

        # Download and extract any companion packages, adding their static dirs
        # to the collectstatic search path (but not to ground truth scoring).
        for i, companion in enumerate(COMPANION_PACKAGES.get(pkg_name, [])):
            comp_dl_dir = Path(dl_dir) / f"comp_{i}"
            comp_ex_dir = Path(ex_dir) / f"comp_{i}"
            comp_dl_dir.mkdir()
            comp_ex_dir.mkdir()
            comp_file, _ = _download_package(companion, str(comp_dl_dir))
            if comp_file:
                comp_root, _ = _extract_all(comp_file, str(comp_ex_dir))
                if comp_root:
                    static_dirs = static_dirs + find_static_dirs(comp_root)

        abs_js_paths = list_js_files(find_static_dirs(extract_root))
        if not abs_js_paths:
            if not has_css_files(static_dirs):
                return None, "no static files found"
            # CSS-only package: run collectstatic but skip JS ground-truth analysis

        # 4. Ground truth: run acorn once on all JS files
        gt_map = run_ground_truth(abs_js_paths)

        # 5 & 6. Run baseline + all variants
        return (
            run_variants(
                run_dir,
                static_dirs,
                gt_map,
                abs_js_paths,
                extract_root,
                variant_timeout=variant_timeout,
                timing_runs=timing_runs,
            ),
            None,
        )


# ── Output formatting ─────────────────────────────────────────────────────────

_COL_W = 13


def _status_cell(vr):
    s = vr["status"]
    cov = vr.get("coverage_pct")
    cov_str = f" {cov:3d}%" if cov is not None else ""
    if s == "pass":
        return f"PASS{cov_str}"
    if s == "fail":
        return "FAIL"
    if s == "warn":
        return f"WARN{cov_str}"
    if s == "timeout":
        return "TIMEOUT"
    return "ERROR"


def print_report(all_results, max_lines=15):
    evaluated = sum(1 for d in all_results.values() if "variants" in d)
    skipped = sum(1 for d in all_results.values() if "skip" in d)
    print(
        f"\nCompatibility Report  —  "
        f"{evaluated} packages evaluated, {skipped} skipped\n"
    )

    pkg_col = 30
    hdrs = "  ".join(f"{VARIANT_LABELS[v]:^{_COL_W}}" for v in VARIANTS)
    sep = "─" * (pkg_col + 2 + (_COL_W + 2) * len(VARIANTS))
    print(f"{'Package':<{pkg_col}}  {hdrs}")
    print(sep)

    totals = {
        v: {"pass": 0, "warn": 0, "fail": 0, "timeout": 0, "error": 0, "skip": 0}
        for v in VARIANTS
    }

    for pkg, pkg_data in all_results.items():
        if "skip" in pkg_data:
            name = (pkg[: pkg_col - 1] + "…") if len(pkg) > pkg_col else pkg
            print(f"{name:<{pkg_col}}  {'SKIP':^{_COL_W}}")
            for v in VARIANTS:
                totals[v]["skip"] += 1
            continue

        variants = pkg_data.get("variants", {})
        cells = "  ".join(f"{_status_cell(variants[v]):^{_COL_W}}" for v in VARIANTS)
        name = (pkg[: pkg_col - 1] + "…") if len(pkg) > pkg_col else pkg
        print(f"{name:<{pkg_col}}  {cells}")

        for v in VARIANTS:
            totals[v][variants[v]["status"]] += 1

    print(sep)
    # Totals row
    cells = "  ".join(
        "{:^{}}".format(
            f"P{totals[v]['pass']}/W{totals[v]['warn']}/F{totals[v]['fail']}",
            _COL_W,
        )
        for v in VARIANTS
    )
    print(f"{'TOTALS (P/W/F)':<{pkg_col}}  {cells}")

    # ── Failures detail ───────────────────────────────────────────────────────
    any_fail = False
    for pkg, pkg_data in all_results.items():
        if "variants" not in pkg_data:
            continue
        variants = pkg_data["variants"]
        failed = [
            v
            for v in VARIANTS
            if variants[v]["status"] in ("fail", "warn", "timeout", "error")
        ]
        if not failed:
            continue

        if not any_fail:
            print("\n" + "═" * 70)
            print("FAILURE / WARNING DETAILS")
            print("═" * 70)
            any_fail = True

        print(f"\n  {pkg}")
        for v in failed:
            vr = variants[v]
            print(f"\n    ── {VARIANT_LABELS[v]} [{vr['status'].upper()}] ──")
            summary = vr.get("error_summary") or []
            if summary:
                for line in summary:
                    print(f"      {line}")
            else:
                # Fall back to raw stderr (older results without error_summary)
                output = (vr["stderr"] or vr["stdout"]).strip()
                lines = output.splitlines()
                for line in lines[:max_lines]:
                    print(f"      {line}")
                if len(lines) > max_lines:
                    print(f"      … ({len(lines) - max_lines} more lines)")

    if not any_fail:
        print("\nNo failures.")


# ── Input parsing ─────────────────────────────────────────────────────────────


def _load_packages(args):
    if args.packages:
        return args.packages
    path = Path(args.input)
    text = path.read_text()
    lines = text.splitlines()
    if path.suffix == ".csv" or (lines and "," in lines[0]):
        reader = csv.DictReader(lines)
        return [row[args.col] for row in reader if row.get(args.col)]
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "input",
        nargs="?",
        help=(
            "CSV/text file of package names, or a JSON"
            " results file with --report-only"
        ),
    )
    parser.add_argument("--packages", nargs="+", metavar="PKG")
    parser.add_argument(
        "--col",
        default="pypi_name",
        help="CSV column for package names (default: pypi_name)",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        default=str(OUTPUTS_DIR / "results.json"),
        help="Write/update JSON results here (default: outputs/results.json)",
    )
    parser.add_argument(
        "--limit", type=int, metavar="N", help="Only evaluate the first N packages"
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
        help="Re-render an existing JSON results file",
    )
    parser.add_argument(
        "--reclassify",
        action="store_true",
        help=(
            "Re-run classify_reason() on every entry in an"
            " existing JSON results file and write it back"
        ),
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=15,
        metavar="N",
        help="Max stderr lines shown per failure (default 15)",
    )
    parser.add_argument(
        "--variant-timeout",
        type=int,
        default=120,
        metavar="SEC",
        help="Seconds before a collectstatic run is killed as TIMEOUT (default 120)",
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
        "--force",
        action="store_true",
        help="Re-evaluate all packages even if already present in the output file",
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
        print_report(data, max_lines=args.max_lines)
        return 0

    if args.reclassify:
        if not args.input:
            parser.error("--reclassify requires an input JSON results file")
        data = json.loads(Path(args.input).read_text())
        updated = 0
        for pkg_data in data.values():
            for vdata in pkg_data.get("variants", {}).values():
                new_reason = classify_reason(vdata)
                if vdata.get("reason") != new_reason:
                    vdata["reason"] = new_reason
                    updated += 1
        out_path = Path(args.output or args.input)
        out_path.write_text(json.dumps(data, indent=2))
        print(f"Reclassified {updated} entries → {out_path}", file=sys.stderr)
        return 0

    if not args.input and not args.packages:
        parser.error("provide an input file or --packages (or --setup / --report-only)")

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    _ensure_node_deps()
    all_packages = _load_packages(args)

    # Load existing results for incremental runs
    all_results = {}
    if args.output and Path(args.output).exists():
        try:
            all_results = json.loads(Path(args.output).read_text())
            done = sum(
                1 for d in all_results.values() if "variants" in d or "skip" in d
            )
            print(
                f"Resuming: {done} packages already done in {args.output}",
                file=sys.stderr,
            )
        except (json.JSONDecodeError, OSError):
            pass

    # --limit applies to packages not yet evaluated, so re-runs advance through the list
    packages = (
        all_packages
        if args.force
        else [p for p in all_packages if p not in all_results]
    )
    if args.limit:
        packages = packages[: args.limit]

    total = len(packages)
    new_count = 0
    for i, pkg in enumerate(packages, 1):

        print(f"  [{i}/{total}] {pkg}…", file=sys.stderr, end="", flush=True)
        variants, skip_reason = evaluate_package(
            pkg, variant_timeout=args.variant_timeout, timing_runs=args.timing_runs
        )
        if skip_reason:
            print(f" SKIP: {skip_reason}", file=sys.stderr)
            all_results[pkg] = {"skip": skip_reason}
        else:
            summary = ", ".join(
                f"{VARIANT_LABELS[v]}:{variants[v]['status'].upper()}" for v in VARIANTS
            )
            print(f"  {summary}", file=sys.stderr)
            all_results[pkg] = {"variants": variants}

        new_count += 1
        if args.output and new_count % 5 == 0:
            Path(args.output).write_text(json.dumps(all_results, indent=2))

    if args.output:
        Path(args.output).write_text(json.dumps(all_results, indent=2))
        print(f"\nResults saved to {args.output}", file=sys.stderr)

    print_report(all_results, max_lines=args.max_lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
