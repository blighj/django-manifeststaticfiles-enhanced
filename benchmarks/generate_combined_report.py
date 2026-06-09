#!/usr/bin/env python3
"""
Generate a combined HTML compatibility report from all three data sources:
  - results.json          (Django packages from PyPI)
  - js_libs_compat.json   (popular JS / CSS libraries)
  - django_sites_compat.json (static files from live Django deployments)

Output: scripts/combined_compat_report.html
"""

import argparse
import concurrent.futures
import csv
import html
import json
import ssl
import statistics
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import certifi

    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

parser = argparse.ArgumentParser()
parser.add_argument(
    "--refresh",
    action="store_true",
    help="Re-fetch lib popularity even if cache exists",
)
parser.add_argument(
    "--output-dir",
    metavar="DIR",
    help="Directory containing audit JSONs and for HTML output (default: outputs/)",
)
args = parser.parse_args()

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPTS_DIR.parent
OUTPUTS_DIR = Path(args.output_dir) if args.output_dir else SCRIPTS_DIR / "outputs"

RESULTS_PKG = OUTPUTS_DIR / "results.json"
RESULTS_LIBS = OUTPUTS_DIR / "js_libs_compat.json"
RESULTS_SITES = OUTPUTS_DIR / "django_sites_compat.json"
CSV_FILE = SCRIPTS_DIR / "packages_with_static.csv"
OUTPUT = OUTPUTS_DIR / "combined_compat_report.html"
LIB_POPULARITY_CACHE = OUTPUTS_DIR / ".lib_popularity_cache.json"

VARIANTS = ["django_42", "django_60", "django_61", "pkg_regex", "pkg_lexer"]
LABELS = {
    "django_42": "Django 4.2",
    "django_60": "Django 6.0",
    "django_61": "Django 6.1",
    "pkg_regex": "pkg regex",
    "pkg_lexer": "pkg lexer",
}

_LIMITATION_REASONS = {
    "regex matched inside minified js",
    "regex matched commented code",
    "extensionless import",
    "extensionless relative import",
    "bare module specifier",
    "circular references",
    "import in comment",
    "import in string",
}

TODOMVC_PREFIX = "todomvc-"
TODOMVC_REPO = "tastejs/todomvc"

# Maps local lib name → actual npm package name where they differ
NPM_PACKAGE_NAMES = {
    "fontawesome": "@fortawesome/fontawesome-free",
    "htmx": "htmx.org",
    "hyperscript": "_hyperscript",
    "isotope": "isotope-layout",
    "masonry": "masonry-layout",
    "popperjs": "@popperjs/core",
    "shoelace": "@shoelace-style/shoelace",
    "stimulus": "@hotwired/stimulus",
    "turbo": "@hotwired/turbo",
    "video-js": "video.js",
}

_UA = "django-manifeststaticfiles-enhanced/report"


def _fetch_json(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
        return json.loads(resp.read())


def _fetch_npm_downloads(lib_name):
    pkg = NPM_PACKAGE_NAMES.get(lib_name, lib_name)
    url = "https://api.npmjs.org/downloads/point/last-week/" + urllib.parse.quote(
        pkg, safe=""
    )
    return _fetch_json(url).get("downloads", 0)


def _fetch_gh_stars(repo):
    data = _fetch_json(f"https://api.github.com/repos/{repo}")
    return data.get("stargazers_count", 0)


def fetch_lib_popularity(lib_names):
    """Return {lib_name: int} — weekly downloads for npm libs, stars for todomvc."""
    if not args.refresh and LIB_POPULARITY_CACHE.exists():
        try:
            cached = json.loads(LIB_POPULARITY_CACHE.read_text())
            if all(n in cached for n in lib_names):
                print("  lib popularity: using cache (pass --refresh to refetch)")
                return {n: cached[n] for n in lib_names}
        except Exception:
            pass

    npm_libs = [n for n in lib_names if not n.startswith(TODOMVC_PREFIX)]
    todomvc_libs = [n for n in lib_names if n.startswith(TODOMVC_PREFIX)]
    result = {}

    if todomvc_libs:
        print(f"  GitHub stars for {TODOMVC_REPO}…", end="", flush=True)
        try:
            stars = _fetch_gh_stars(TODOMVC_REPO)
            for n in todomvc_libs:
                result[n] = stars
            print(f" {stars:,}")
        except Exception as exc:
            print(f" FAILED ({exc})")
            for n in todomvc_libs:
                result[n] = 0

    if npm_libs:
        print(f"  npm weekly downloads for {len(npm_libs)} packages…", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            futures = {pool.submit(_fetch_npm_downloads, n): n for n in npm_libs}
            done = 0
            for fut in concurrent.futures.as_completed(futures):
                name = futures[fut]
                done += 1
                try:
                    result[name] = fut.result()
                except Exception:
                    result[name] = 0
                print(f"\r  {done}/{len(npm_libs)} fetched…", end="", flush=True)
        print()

    try:
        LIB_POPULARITY_CACHE.write_text(json.dumps(result, indent=2, sort_keys=True))
    except Exception:
        pass

    return result


def format_downloads(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M/wk"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k/wk"
    return f"{n}/wk"


# ── helpers ───────────────────────────────────────────────────────────────────


def worst_status(variants):
    statuses = [v.get("status") for v in variants.values() if isinstance(v, dict)]
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    if "pass" in statuses:
        return "pass"
    return None


def best_reason(variants):
    simple = ""
    for v in VARIANTS:
        r = variants.get(v, {}).get("reason", "NA")
        if r and r != "NA":
            if "," in r:
                return r
            if not simple:
                simple = r
    return simple


def categorize(reason, worst):
    if worst not in ("fail", "warn"):
        return ""
    r = (reason or "").lower()
    if not r or r == "na":
        return "limitation"
    if any(lim in r for lim in _LIMITATION_REASONS):
        return "limitation"
    return "missing"


def status_badge(status, vdata=None):
    cls = {"pass": "pass", "fail": "fail", "warn": "pass"}.get(status, "skip")
    label = {"pass": "PASS", "fail": "FAIL", "warn": "PASS"}.get(
        status, "SKIP" if status is None else status.upper()
    )
    cov = overhead = ""
    if status in ("pass", "warn") and vdata:
        pct = vdata.get("coverage_pct")
        total = vdata.get("gt_total") or 0
        if pct is not None and total > 0:
            replaced = vdata.get("gt_replaced", 0)
            cov_color = (
                "inherit" if pct == 100 else ("#cf222e" if pct == 0 else "#9a6700")
            )
            cov = (
                f' <span style="font-weight:400;color:{cov_color}" '
                f'title="{replaced}/{total} imports replaced">({pct}%)</span>'
            )
        op = vdata.get("overhead_pct")
        if op is not None:
            mult = (100 + op) / 100
            color = "#cf222e" if mult > 4.0 else "#9a6700" if mult > 2.5 else "#656d76"
            overhead = f'<div class="overhead" style="color:{color}">{mult:.1f}x</div>'
    return f'<span class="badge {cls}">{label}{cov}</span>{overhead}'


def error_tooltip(vdata):
    errs = vdata.get("error_summary", [])
    if not errs:
        return ""
    return f' title="{html.escape(chr(10).join(errs[:4]))}"'


def category_badge(cat):
    if cat == "missing":
        return '<span class="badge cat-pkg">Missing files</span>'
    if cat == "limitation":
        return '<span class="badge cat-lim">Limitation</span>'
    return ""


def type_badge(typ):
    colors = {
        "package": ("type-pkg", "Package"),
        "lib": ("type-lib", "JS Lib"),
        "site": ("type-site", "Site"),
    }
    cls, label = colors.get(typ, ("", typ))
    return f'<span class="badge {cls}">{label}</span>'


# ── link builders ─────────────────────────────────────────────────────────────


def pkg_link(pypi_name, slug, gh_url=""):
    url = f"https://pypi.org/project/{html.escape(pypi_name)}/"
    name = html.escape(slug or pypi_name)
    link = f'<a href="{url}" target="_blank">{name}</a>'
    if gh_url:
        link += f' <a href="{html.escape(gh_url)}" target="_blank" title="GitHub">★</a>'
    return link


def lib_link(name):
    if name.startswith(TODOMVC_PREFIX):
        example = name[len(TODOMVC_PREFIX) :]
        url = (
            "https://github.com/tastejs/todomvc/tree/master/examples/"
            f"{html.escape(example)}"
        )
    else:
        pkg = NPM_PACKAGE_NAMES.get(name, name)
        url = f"https://www.npmjs.com/package/{html.escape(pkg)}"
    return f'<a href="{url}" target="_blank">{html.escape(name)}</a>'


def site_link(dir_name):
    # e.g. "adamghill_com_static" → "https://adamghill.com/"
    domain = dir_name.removesuffix("_static").replace("_", ".")
    url = f"https://{domain}/"
    return f'<a href="{html.escape(url)}" target="_blank">{html.escape(domain)}</a>'


def popularity_cell(typ, name, pop_value, max_pkg_stars, max_npm_dl):
    """Returns (inner_html, sort_val) for the Popularity column."""
    if typ == "site":
        return "", 0
    if typ == "package":
        s = pop_value or 0
        if not s:
            return "", 0
        pct = min(s / max_pkg_stars * 100, 100) if max_pkg_stars else 0
        html_str = (
            f'<div class="bar-wrap">'
            f'<div class="bar" style="width:{pct:.2f}%"></div></div>'
            f'<span class="stars-num">{s:,}</span>'
        )
        return html_str, s
    # lib
    v = pop_value or 0
    if not v:
        return "", 0
    if name.startswith(TODOMVC_PREFIX):
        return f'<span class="stars-num">★ {v:,}</span>', v
    pct = min(v / max_npm_dl * 100, 100) if max_npm_dl else 0
    html_str = (
        f'<div class="bar-wrap">'
        f'<div class="bar npm-bar" style="width:{pct:.2f}%"></div></div>'
        f'<span class="stars-num">{format_downloads(v)}</span>'
    )
    return html_str, v


# ── load data ─────────────────────────────────────────────────────────────────

pkg_meta = {}
with open(CSV_FILE) as f:
    for row in csv.DictReader(f):
        pkg_meta[row["pypi_name"]] = row

packages_raw = json.loads(RESULTS_PKG.read_text())
libs_raw = json.loads(RESULTS_LIBS.read_text())
sites_raw = json.loads(RESULTS_SITES.read_text())

# ── lib popularity ────────────────────────────────────────────────────────────

tested_lib_names = [n for n, d in libs_raw.items() if "skip" not in d]
lib_popularity = fetch_lib_popularity(tested_lib_names)

npm_downloads = [
    v for n, v in lib_popularity.items() if not n.startswith(TODOMVC_PREFIX) and v
]
max_npm_dl = max(npm_downloads) if npm_downloads else 1

# ── build unified rows ────────────────────────────────────────────────────────

rows = []  # (type, name, link_html, pop_value, file_count, variants)
skipped = {"package": 0, "lib": 0, "site": 0}

pkg_stars_list = []
for pkg_name, data in packages_raw.items():
    if "skip" in data:
        skipped["package"] += 1
        continue
    m = pkg_meta.get(pkg_name, {})
    link = pkg_link(pkg_name, m.get("slug", pkg_name), m.get("repo_url", ""))
    try:
        stars = int(m.get("repo_watchers", 0) or 0)
    except (ValueError, TypeError):
        stars = 0
    try:
        file_count = int(m.get("static_file_count", 0) or 0)
    except (ValueError, TypeError):
        file_count = 0
    pkg_stars_list.append(stars)
    rows.append(
        ("package", pkg_name, link, stars, file_count, data.get("variants", {}))
    )

max_pkg_stars = max(pkg_stars_list) if pkg_stars_list else 1

for lib_name, data in libs_raw.items():
    if "skip" in data:
        skipped["lib"] += 1
        continue
    rows.append(
        (
            "lib",
            lib_name,
            lib_link(lib_name),
            lib_popularity.get(lib_name, 0),
            data.get("static_file_count", 0),
            data.get("variants", {}),
        )
    )

for site_name, data in sites_raw.items():
    if "skip" in data:
        skipped["site"] += 1
        continue
    rows.append(
        (
            "site",
            site_name,
            site_link(site_name),
            None,
            data.get("static_file_count", 0),
            data.get("variants", {}),
        )
    )

# ── sort ──────────────────────────────────────────────────────────────────────

TYPE_ORDER = {"package": 0, "lib": 1, "site": 2}


def row_sort_key(r):
    typ, name, link, pop_value, file_count, variants = r
    w = worst_status(variants)
    cat = categorize(best_reason(variants), w)
    cat_order = {"limitation": 0, "missing": 1, "": 2}
    pop = -(pop_value or 0) if typ in ("package", "lib") else 0
    return (cat_order.get(cat, 2), TYPE_ORDER[typ], pop, name)


rows.sort(key=row_sort_key)

# ── per-variant counts (across all types) ─────────────────────────────────────

counts = {v: {"passing": 0, "missing": 0, "limitation": 0} for v in VARIANTS}
for typ, name, link, pop_value, file_count, variants in rows:
    w = worst_status(variants)
    cat = categorize(best_reason(variants), w)
    for v in VARIANTS:
        s = variants.get(v, {}).get("status")
        if s in ("pass", "warn"):
            counts[v]["passing"] += 1
        elif cat == "missing":
            counts[v]["missing"] += 1
        else:
            counts[v]["limitation"] += 1

total_tested = len(rows)
total_skipped = sum(skipped.values())

# Overhead median and >4x outlier count across all sources
_median_overhead = {}
_overhead_gt4x = {}
for _v in VARIANTS:
    _vals = [
        (100 + d["variants"][_v]["overhead_pct"]) / 100
        for _raw in (packages_raw, libs_raw, sites_raw)
        for d in _raw.values()
        if "variants" in d
        and d["variants"].get(_v, {}).get("status") in ("pass", "warn")
        and d["variants"].get(_v, {}).get("overhead_pct") is not None
    ]
    _median_overhead[_v] = round(statistics.median(_vals), 1) if _vals else None
    _overhead_gt4x[_v] = sum(1 for x in _vals if x > 4.0)

# Aggregate import coverage across all sources
_cov_total = {v: 0 for v in VARIANTS}
_cov_replaced = {v: 0 for v in VARIANTS}
for _raw in (packages_raw, libs_raw, sites_raw):
    for d in _raw.values():
        if "variants" not in d:
            continue
        for _v in VARIANTS:
            _vd = d["variants"].get(_v, {})
            if _vd.get("status") in ("pass", "warn"):
                _cov_total[_v] += _vd.get("gt_total") or 0
                _cov_replaced[_v] += _vd.get("gt_replaced") or 0

# ── summary cards ─────────────────────────────────────────────────────────────

summary_cards = ""
for v in VARIANTS:
    c = counts[v]
    total = c["passing"] + c["missing"] + c["limitation"]
    pass_pct = round(c["passing"] / total * 100) if total else 0
    pkg_pct = round((c["passing"] + c["missing"]) / total * 100) if total else 0
    med = _median_overhead.get(v)
    gt4x = _overhead_gt4x.get(v, 0)
    gt4x_note = (
        f' <span style="color:#cf222e;font-weight:600">' f"· {gt4x} &gt;4×</span>"
        if gt4x
        else ""
    )
    overhead_note = (
        f'<div class="card-overhead">{med:.1f}x overhead (median){gt4x_note}</div>'
        if med
        else ""
    )

    gt_total = _cov_total[v]
    gt_replaced = _cov_replaced[v]
    if gt_total > 0:
        cov_pct = round(gt_replaced / gt_total * 100)
        cov_color = (
            "inherit" if cov_pct == 100 else ("#cf222e" if cov_pct < 80 else "#9a6700")
        )
        cov_note = (
            f'<div class="card-overhead" style="color:{cov_color}">'
            f"{cov_pct}% import coverage"
            f' <span style="font-weight:400;color:#888">'
            f"({gt_replaced:,}&thinsp;/&thinsp;{gt_total:,})</span>"
            f"</div>"
        )
    else:
        cov_note = ""

    summary_cards += f"""
    <div class="card">
      <div class="card-label">{LABELS[v]}</div>
      <div class="card-stats">
        <span class="s-pass">{c['passing']} passing</span>
        <span class="s-pkg">{c['missing']} missing files</span>
        <span class="s-lim">{c['limitation']} limitation</span>
      </div>
      <div class="card-bar-wrap">
        <div class="card-bar-pkg" style="width:{pkg_pct}%"></div>
        <div class="card-bar"     style="width:{pass_pct}%"></div>
      </div>
      <div class="card-pct">{pass_pct}% passing</div>
      {cov_note}
      {overhead_note}
    </div>"""

# ── table rows ────────────────────────────────────────────────────────────────

table_rows = []
for typ, name, link_html, pop_value, file_count, variants in rows:
    w = worst_status(variants)
    reason = best_reason(variants)
    cat = categorize(reason, w)
    row_cls = {"missing": "row-pkg-fail", "limitation": "row-fail"}.get(cat, "")

    cells = ""
    for v in VARIANTS:
        vdata = variants.get(v, {})
        status = vdata.get("status")
        tip = error_tooltip(vdata)
        cells += f'<td class="cov"{tip}>{status_badge(status, vdata)}</td>'

    pop_html, pop_sort = popularity_cell(
        typ, name, pop_value, max_pkg_stars, max_npm_dl
    )
    file_count_html = (
        f'<span class="stars-num">{file_count}</span>' if file_count else ""
    )

    table_rows.append(f"""
    <tr class="{row_cls}" data-worst="{w or 'skip'}" data-cat="{cat}" data-type="{typ}">
      <td>{link_html}</td>
      <td class="pop" data-val="{pop_sort}">{pop_html}</td>
      <td class="num" data-val="{file_count or 0}">{file_count_html}</td>
      {cells}
      <td class="reason">{html.escape(reason)}</td>
      <td>{category_badge(cat)}</td>
      <td>{type_badge(typ)}</td>
    </tr>""")

table_html = "\n".join(table_rows)

# ── per-type counts for the subtitle ─────────────────────────────────────────

type_counts = {"package": 0, "lib": 0, "site": 0}
for typ, *_ in rows:
    type_counts[typ] += 1

# todomvc star count for subtitle (all share same value)
_todomvc_stars = next(
    (v for n, v in lib_popularity.items() if n.startswith(TODOMVC_PREFIX) and v), None
)

# ── full HTML ─────────────────────────────────────────────────────────────────

page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>collectstatic combined compat report</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 13px;
      background: #f6f8fa;
      color: #1f2328;
      padding: 24px;
    }}
    h1 {{ font-size: 1.35rem; margin-bottom: 4px; }}
    .subtitle {{ color: #656d76; margin-bottom: 20px; font-size: 0.85rem; }}

    /* cards */
    .cards {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }}
    .card {{
      background: #fff; border: 1px solid #d0d7de; border-radius: 8px;
      padding: 14px 18px; min-width: 160px; flex: 1;
    }}
    .card-label {{ font-weight: 600; font-size: 0.9rem;
                   margin-bottom: 6px; color: #444; }}
    .card-stats {{ font-size: 0.8rem; display: flex; gap: 8px;
                   margin-bottom: 6px; flex-wrap: wrap; }}
    .s-pass {{ color: #1a7f37; font-weight: 600; }}
    .s-pkg  {{ color: #92400e; font-weight: 600; }}
    .s-lim  {{ color: #1e40af; font-weight: 600; }}
    .card-bar-wrap {{ position: relative; height: 6px; background: #eaeef2;
                      border-radius: 3px; margin-bottom: 4px; }}
    .card-bar-pkg {{ position: absolute; height: 100%;
                     background: #fcd34d; border-radius: 3px; }}
    .card-bar {{ position: absolute; height: 100%;
                 background: #1a7f37; border-radius: 3px; }}
    .card-pct      {{ font-size: 0.75rem; color: #656d76; }}
    .card-overhead {{ font-size: 0.72rem; color: #656d76; margin-top: 2px; }}

    /* toolbar */
    .toolbar {{ display: flex; gap: 8px; align-items: center;
                margin-bottom: 12px; flex-wrap: wrap; }}
    #search {{
      padding: 6px 11px; border: 1px solid #d0d7de; border-radius: 6px;
      font-size: 13px; width: 220px; background: #fff;
    }}
    .filter-group {{ display: flex; gap: 5px; align-items: center; }}
    .filter-sep {{
      width: 1px; height: 22px; background: #d0d7de; margin: 0 4px;
    }}
    .filter-btn {{
      padding: 4px 11px; border: 1px solid #d0d7de; border-radius: 6px;
      background: #fff; cursor: pointer; font-size: 12px; font-weight: 500;
    }}
    .filter-btn.active {{ background: #1f2328; color: #fff; border-color: #1f2328; }}
    .filter-btn.f-pass.active  {{ background: #1a7f37; border-color: #1a7f37; }}
    .filter-btn.f-pkg.active   {{ background: #92400e; border-color: #92400e; }}
    .filter-btn.f-lim.active   {{ background: #1e40af; border-color: #1e40af; }}
    .filter-btn.f-tpkg.active  {{ background: #6f42c1; border-color: #6f42c1; }}
    .filter-btn.f-tlib.active  {{ background: #0969da; border-color: #0969da; }}
    .filter-btn.f-tsite.active {{ background: #1a7f37; border-color: #1a7f37; }}
    .tally {{ font-size: 0.8rem; color: #656d76; margin-left: auto; }}

    /* table */
    .table-wrap {{ overflow-x: auto; border-radius: 8px;
                   box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; }}
    thead th {{
      background: #1f2328; color: #e6edf3; text-align: left;
      padding: 9px 10px; font-weight: 600; cursor: pointer;
      user-select: none; white-space: nowrap; font-size: 12px;
    }}
    thead th:hover {{ background: #32383f; }}
    tbody tr {{ border-top: 1px solid #f0f0f0; }}
    tbody tr:hover {{ background: #f6f8fa; }}
    td {{ padding: 7px 10px; vertical-align: middle; }}
    td.cov {{ text-align: center; white-space: nowrap; min-width: 68px; }}
    td.pop {{ white-space: nowrap; }}
    td.num, th.num {{ text-align: right; white-space: nowrap; }}
    td.reason {{ color: #656d76; font-size: 11px; max-width: 200px; }}
    .overhead {{ font-size: 10px; margin-top: 2px; font-weight: 500; }}

    .row-fail     {{ background: #fff8f8; }}
    .row-warn     {{ background: #fffdf0; }}
    .row-pkg-fail {{ background: #fffbeb; }}

    /* badges */
    .badge {{
      display: inline-block; padding: 2px 7px; border-radius: 10px;
      font-size: 11px; font-weight: 700; letter-spacing: .02em;
    }}
    .badge.pass    {{ background: #dafbe1; color: #1a7f37; }}
    .badge.fail    {{ background: #ffebe9; color: #cf222e; }}
    .badge.skip    {{ background: #f0f0f0; color: #888; }}
    .badge.cat-pkg {{ background: #fef3c7; color: #92400e; }}
    .badge.cat-lim {{ background: #dbeafe; color: #1e40af; }}
    .badge.type-pkg  {{ background: #f3e8ff; color: #6f42c1; }}
    .badge.type-lib  {{ background: #dbeafe; color: #0969da; }}
    .badge.type-site {{ background: #dcfce7; color: #166534; }}

    /* popularity bar */
    .bar-wrap {{
      display: inline-block; width: 56px; height: 4px;
      background: #eaeef2; border-radius: 2px;
      vertical-align: middle; margin-right: 4px;
    }}
    .bar     {{ height: 100%; background: #1a7f37; border-radius: 2px; }}
    .npm-bar {{ background: #0969da; }}
    .stars-num {{ font-size: 11px; color: #656d76; vertical-align: middle; }}

    a {{ color: #0969da; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .hidden {{ display: none !important; }}
  </style>
</head>
<body>
  <h1>collectstatic combined compatibility report</h1>
  <p class="subtitle">
    {total_tested} entries tested &nbsp;·&nbsp;
    {type_counts['package']} packages &nbsp;·&nbsp;
    {type_counts['lib']} JS libs &nbsp;·&nbsp;
    {type_counts['site']} sites &nbsp;·&nbsp;
    5 variants: stock Django 4.2, 6.0, 6.1 and this package's
    regex &amp; lexer modes &nbsp;·&nbsp;
    overhead shown where measured
  </p>

  <div class="cards">{summary_cards}</div>

  <div class="toolbar">
    <input id="search" type="search" placeholder="Filter…" oninput="applyFilters()">

    <div class="filter-sep"></div>

    <div class="filter-group">
      <button class="filter-btn active" data-status="all"
              onclick="setStatus('all',this)">All</button>
      <button class="filter-btn f-pass" data-status="pass"
              onclick="setStatus('pass',this)">Passing</button>
      <button class="filter-btn f-pkg" data-status="cat:missing"
              onclick="setStatus('cat:missing',this)">Missing files</button>
      <button class="filter-btn f-lim" data-status="cat:limitation"
              onclick="setStatus('cat:limitation',this)">Limitations</button>
    </div>

    <div class="filter-sep"></div>

    <div class="filter-group">
      <button class="filter-btn active" data-type="all"
              onclick="setType('all',this)">All types</button>
      <button class="filter-btn f-tpkg" data-type="package"
              onclick="setType('package',this)">Packages</button>
      <button class="filter-btn f-tlib" data-type="lib"
              onclick="setType('lib',this)">JS Libs</button>
      <button class="filter-btn f-tsite" data-type="site"
              onclick="setType('site',this)">Sites</button>
    </div>

    <span class="tally" id="tally"></span>
  </div>

  <div class="table-wrap">
    <table id="main-table">
      <thead>
        <tr>
          <th onclick="sortTable(0)">Name</th>
          <th onclick="sortTable(1)">Popularity</th>
          <th class="num" onclick="sortTable(2)">Files</th>
          <th class="cov" onclick="sortTable(3)">Django 4.2</th>
          <th class="cov" onclick="sortTable(4)">Django 6.0</th>
          <th class="cov" onclick="sortTable(5)">Django 6.1</th>
          <th class="cov" onclick="sortTable(6)">pkg regex</th>
          <th class="cov" onclick="sortTable(7)">pkg lexer</th>
          <th onclick="sortTable(8)">Failure reason</th>
          <th onclick="sortTable(9)">Category</th>
          <th onclick="sortTable(10)">Type</th>
        </tr>
      </thead>
      <tbody>
        {table_html}
      </tbody>
    </table>
  </div>

  <script>
    let activeStatus = 'all';
    let activeType   = 'all';
    let sortCol = 1, sortAsc = false;
    const statusOrder = {{fail:0, warn:1, pass:2, skip:3}};
    const statusCols = new Set([3,4,5,6,7]);

    function setStatus(s, btn) {{
      activeStatus = s;
      document.querySelectorAll('[data-status]')
        .forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      applyFilters();
    }}

    function setType(t, btn) {{
      activeType = t;
      document.querySelectorAll('[data-type]')
        .forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      applyFilters();
    }}

    function applyFilters() {{
      const q = document.getElementById('search').value.toLowerCase();
      let visible = 0;
      document.querySelectorAll('#main-table tbody tr').forEach(row => {{
        const worst = row.dataset.worst;
        const cat   = row.dataset.cat || '';
        const typ   = row.dataset.type || '';

        let matchStatus;
        if      (activeStatus === 'all')           matchStatus = true;
        else if (activeStatus === 'pass')
          matchStatus = worst === 'pass' || worst === 'skip';
        else if (activeStatus === 'cat:missing')    matchStatus = cat === 'missing';
        else if (activeStatus === 'cat:limitation') matchStatus = cat === 'limitation';
        else matchStatus = false;

        const matchType   = activeType === 'all' || typ === activeType;
        const matchSearch = !q || row.innerText.toLowerCase().includes(q);

        row.classList.toggle('hidden', !matchStatus || !matchType || !matchSearch);
        if (matchStatus && matchType && matchSearch) visible++;
      }});
      document.getElementById('tally').textContent = visible + ' shown';
    }}

    function cellVal(row, col) {{
      const cell = row.cells[col];
      if (!cell) return '';
      return cell.getAttribute('data-val') ?? cell.innerText.replace(/,/g,'').trim();
    }}

    function sortTable(col) {{
      if (sortCol === col) {{ sortAsc = !sortAsc; }}
      else {{ sortAsc = false; sortCol = col; }}
      const tbody = document.querySelector('#main-table tbody');
      const rows  = Array.from(tbody.rows);
      rows.sort((a, b) => {{
        let av = cellVal(a, col), bv = cellVal(b, col);
        if (statusCols.has(col)) {{
          av = statusOrder[av.toLowerCase()] ?? 9;
          bv = statusOrder[bv.toLowerCase()] ?? 9;
          return sortAsc ? av - bv : bv - av;
        }}
        const diff = isNaN(av) || isNaN(bv)
          ? av.localeCompare(bv) : Number(av) - Number(bv);
        return sortAsc ? diff : -diff;
      }});
      rows.forEach(r => tbody.appendChild(r));
    }}

    applyFilters();
  </script>
</body>
</html>"""

OUTPUTS_DIR.mkdir(exist_ok=True)
OUTPUT.write_text(page)
print(f"Written {OUTPUT}  ({OUTPUT.stat().st_size // 1024} KB, {total_tested} entries)")
print(
    f"  {type_counts['package']} packages,"
    f" {type_counts['lib']} JS libs,"
    f" {type_counts['site']} sites"
)
