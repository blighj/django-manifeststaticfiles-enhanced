# Benchmarks

Compatibility testing for `collectstatic` across five variants: stock Django 4.2, 6.0, and 6.1 (with `support_js_module_import_aggregation=True`), plus this package's regex and lexer modes.

Results from three data sources are merged into a single HTML report.

---

## Data sources

| Source | Download script | Evaluation script | Output JSON |
|--------|----------------|-------------------|-------------|
| PyPI packages with static files | `discover_static_packages.py` → `packages_with_static.csv` | `pypi_audit.py` | `results.json` |
| Popular JS/CSS libraries (npm) | `download_js_libs.py` | `pre_downloaded_audit.py` | `js_libs_compat.json` |
| Live Django site static files | `download_django_sites.py` | `pre_downloaded_audit.py` | `django_sites_compat.json` |

---

## Full workflow

### 1. One-time venv setup

Both evaluation scripts share the same per-Django-version venvs (stored in `venvs/`):

```sh
python pypi_audit.py --setup
```

Verify with:

```sh
python pypi_audit.py --setup-check
```

---

### 2. Build the PyPI package corpus

`discover_static_packages.py` scrapes djangopackages.org and uses HTTP range requests to inspect PyPI wheels without downloading them in full. It writes `packages_with_static.csv`.

```sh
pip install aiohttp certifi
python discover_static_packages.py
```

This only needs to be re-run periodically to refresh the package list.

---

### 3. Download the JS library corpus

```sh
python download_js_libs.py --output /path/to/js_libs
```

Libraries already present are skipped. Use `--force` to re-download, or `--lib jquery vue` to fetch specific ones.

---

### 4. Download the Django sites corpus

```sh
python download_django_sites.py --output /path/to/django_sites
```

Fetches each site's `staticfiles.json` manifest then downloads the listed files. Sites that have gone offline are skipped with a warning.

---

### 5. Run the evaluations

Once all corpora are in place, `run_audit.py` runs all three audits in sequence and then generates the combined HTML report:

```sh
python run_audit.py --js-libs /path/to/js_libs --django-sites /path/to/django_sites
```

Omitting either corpus flag skips that audit. The combined report is only generated when all three output JSONs exist. Pass `--force` to re-evaluate everything, or `--limit N` to cap the PyPI package count.

Alternatively, run each script individually. Each actually runs `collectstatic` and measures import rewrite coverage using [Acorn](https://github.com/acornjs/acorn) as ground truth (via `ground_truth.js`). Node.js dependencies are installed automatically on first run.

**PyPI packages** (downloads each package on the fly):

```sh
python pypi_audit.py packages_with_static.csv --output outputs/results.json
```

Supports `--limit N` to evaluate a subset, and resumes automatically if `results.json` already exists.

**JS libraries** (against the pre-downloaded corpus):

```sh
python pre_downloaded_audit.py --local-dir /path/to/js_libs --output outputs/js_libs_compat.json
```

**Django sites** (same script, different corpus):

```sh
python pre_downloaded_audit.py --local-dir /path/to/django_sites --output outputs/django_sites_compat.json
```

---

### 6. Generate the combined HTML report

Reads `results.json`, `js_libs_compat.json`, and `django_sites_compat.json` from the project root, then fetches npm download counts and GitHub stars for popularity columns (cached in `.lib_popularity_cache.json`).

```sh
python generate_combined_report.py
# outputs: benchmarks/outputs/combined_compat_report.html
```

Pass `--refresh` to re-fetch popularity data instead of using the cache.

---

## Re-rendering without re-running

Both evaluation scripts support `--report-only` to re-render a saved JSON file as a terminal table without running `collectstatic` again:

```sh
python pypi_audit.py outputs/results.json --report-only
python pre_downloaded_audit.py outputs/js_libs_compat.json --report-only
```

---

## How ground truth works

`ground_truth.js` is a Node.js script that uses Acorn to parse each JS file and extract every `import`/`export`/`import()` statement. The evaluation scripts use this as the authoritative source of what imports exist, then check the `collectstatic` output to see which ones were actually rewritten. Coverage is reported as a percentage per variant.

Files that contain bare module specifiers (e.g. `import React from 'react'`) are treated as build artifacts and excluded from coverage scoring, since they are not intended to run directly in a browser.
