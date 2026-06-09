#!/usr/bin/env python3
"""
Download the JS library test corpus into a local directory.

Each library is fetched from the npm registry (or GitHub for todomvc examples)
and the relevant browser-ready files are extracted.  Libraries already present
are skipped unless --force is given.

Usage:
    python download_js_libs.py --output /path/to/js_libs_dir
    python download_js_libs.py --output /path/to/js_libs_dir --force
    python download_js_libs.py --output /path/to/js_libs_dir --lib jquery bootstrap

Then run the compat test against the downloaded corpus:
    python js_libs_compat.py --local-dir /path/to/js_libs_dir \\
        --output js_libs_compat.json

Requirements: Python 3.9+, no extra packages needed.
"""

import argparse
import io
import ssl
import tarfile
import urllib.request
import zipfile
from pathlib import Path

# Use certifi for SSL verification if available (helps on macOS); otherwise
# fall back to the default context which works on most systems.
try:
    import certifi

    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl.create_default_context()

# ── npm packages ──────────────────────────────────────────────────────────────
#
# Each entry: (label, npm_package_name, version, extract)
#
# extract controls which files are placed into dest/<label>/:
#
#   None        – extract the entire package root (package/**), preserving
#                 the relative structure within the package.
#   "subdir"    – extract package/subdir/** and strip the "subdir/" prefix so
#                 files land at dest/<label>/ root level.
#   ["a","b"]   – same as "subdir" applied to each item independently; a
#                 trailing "/" means strip that dir's own name (copy contents),
#                 no trailing "/" means the dir itself is placed in dest.
#
# In practice this matches the behaviour of the original download_packages.sh
# script that populated the test corpus.
#
NPM_PACKAGES = [
    # ── Classic / global-script ──────────────────────────────────────────────
    ("alpinejs", "alpinejs", "3.14.9", "dist"),
    (
        "animate.css",
        "animate.css",
        "4.1.1",
        [("animate.css", ""), ("animate.min.css", "")],
    ),
    ("axios", "axios", "1.9.0", "dist"),
    ("backbone", "backbone", "1.6.0", [("backbone.js", "")]),
    ("bootstrap", "bootstrap", "5.3.6", "dist"),
    ("bulma", "bulma", "1.0.4", "css"),
    ("cash-dom", "cash-dom", "8.1.5", "dist"),
    ("chart.js", "chart.js", "4.4.9", "dist"),
    ("d3", "d3", "7.9.0", "dist"),
    # date-fns 4.x ships individual ES module files at the package root (no cdn/ dir)
    ("date-fns", "date-fns", "4.1.0", None),
    ("dayjs", "dayjs", "1.11.13", [("dayjs.min.js", "")]),
    ("flatpickr", "flatpickr", "4.6.13", "dist"),
    # fontawesome: copy css/ and webfonts/ (js/ omitted — large and rarely used)
    ("fontawesome", "@fortawesome/fontawesome-free", "6.7.2", ["css", "webfonts"]),
    ("gsap", "gsap", "3.12.7", "dist"),
    ("htm", "htm", "3.1.1", "dist"),
    ("htmx", "htmx.org", "2.0.4", "dist"),
    ("hyperscript", "hyperscript.org", "0.9.14", "dist"),
    ("immer", "immer", "10.1.1", "dist"),
    ("intro.js", "intro.js", "7.2.0", "minified"),
    ("isotope", "isotope-layout", "3.0.6", "dist"),
    ("jquery", "jquery", "4.0.0", "dist"),
    ("jquery-ui", "jquery-ui", "1.14.1", "dist"),
    ("lazysizes", "lazysizes", "5.3.2", [("lazysizes.js", "")]),
    # lit: entire package (ESM-only; index.js + submodule dirs at package root)
    ("lit", "lit", "3.3.2", None),
    ("lodash", "lodash", "4.17.21", [("lodash.js", "")]),
    ("lodash-es", "lodash-es", "4.17.21", [("lodash.js", "")]),
    ("masonry", "masonry-layout", "4.2.2", "dist"),
    ("mobx", "mobx", "6.13.7", "dist"),
    ("moment", "moment", "2.30.1", [("moment.js", "")]),
    # nanoid: browser-ready entry-point + non-secure and url-alphabet sub-packages
    (
        "nanoid",
        "nanoid",
        "5.1.5",
        [
            ("index.browser.js", ""),
            ("non-secure/", "non-secure"),
            ("url-alphabet/", "url-alphabet"),
        ],
    ),
    ("normalize.css", "normalize.css", "8.0.1", [("normalize.css", "")]),
    ("petite-vue", "petite-vue", "0.4.1", "dist"),
    ("popperjs", "@popperjs/core", "2.11.8", "dist"),
    ("preact", "preact", "10.25.4", "dist"),
    ("ramda", "ramda", "0.30.1", "dist"),
    ("redux", "redux", "5.0.1", "dist"),
    ("requirejs", "requirejs", "2.3.7", [("require.js", "")]),
    # rxjs: UMD bundles only (dist/bundles/** → dest root)
    ("rxjs", "rxjs", "7.8.2", "dist/bundles"),
    ("select2", "select2", "4.1.0-rc.0", "dist"),
    # shoelace: dist/** → dest root (components, assets, chunks …)
    ("shoelace", "@shoelace-style/shoelace", "2.20.0", "dist"),
    ("sortablejs", "sortablejs", "1.15.6", [("Sortable.js", "")]),
    ("stimulus", "@hotwired/stimulus", "3.2.2", "dist"),
    ("sweetalert2", "sweetalert2", "11.15.10", "dist"),
    ("swiper", "swiper", "11.2.6", [("swiper-bundle.min.js", "")]),
    ("systemjs", "systemjs", "6.15.1", "dist"),
    ("three", "three", "0.171.0", "build"),
    ("tippy.js", "tippy.js", "6.3.7", "dist"),
    ("toastr", "toastr", "2.1.4", "build"),
    ("turbo", "@hotwired/turbo", "8.0.12", "dist"),
    ("underscore", "underscore", "1.13.7", [("underscore.js", "")]),
    # uuid: esm-browser/ subdirectory preserved (not stripped)
    ("uuid", "uuid", "11.1.0", [("dist/esm-browser/", "esm-browser")]),
    # valtio: esm/** → dest root (index.mjs, react/, vanilla/ …)
    ("valtio", "valtio", "2.1.2", "esm"),
    ("video-js", "video.js", "8.21.1", "dist"),
    ("vue", "vue", "3.5.13", "dist"),
    ("workbox-sw", "workbox-sw", "7.3.0", "build"),
    ("xstate", "xstate", "5.19.2", "dist"),
]

# ── todomvc examples (from GitHub) ────────────────────────────────────────────
#
# The tastejs/todomvc repo ships pre-built examples.  We download a zip of the
# main branch and extract the relevant example directories.
#
TODOMVC_REPO = "https://github.com/tastejs/todomvc/archive/refs/heads/master.zip"

TODOMVC_EXAMPLES = [
    # (label, examples/<subdir>)
    # Only the built dist/ output is extracted for each example (not source files).
    ("todomvc-angular", "angular"),
    ("todomvc-javascript-es5", "javascript-es5"),
    ("todomvc-javascript-es6", "javascript-es6"),
    ("todomvc-jquery", "jquery"),
    ("todomvc-preact", "preact"),
    ("todomvc-react", "react"),
    ("todomvc-svelte", "svelte"),
    ("todomvc-vue", "vue"),
    ("todomvc-web-components", "web-components"),
]

# Only extract this subdirectory from each todomvc example (strip the prefix).
# The built output lives in dist/ for all current examples.
TODOMVC_DIST_SUBDIR = "dist"


# ── helpers ───────────────────────────────────────────────────────────────────


def _user_agent():
    return "django-manifeststaticfiles-enhanced/download-corpus"


def _npm_tarball_url(pkg, version):
    if pkg.startswith("@"):
        scope, name = pkg.split("/", 1)
        return f"https://registry.npmjs.org/{pkg}/-/{name}-{version}.tgz"
    return f"https://registry.npmjs.org/{pkg}/-/{pkg}-{version}.tgz"


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent()})
    with urllib.request.urlopen(req, timeout=120, context=_SSL_CONTEXT) as resp:
        return resp.read()


def _safe_path(name):
    """Strip leading slashes and refuse path traversal."""
    parts = Path(name).parts
    clean = []
    for p in parts:
        if p in ("", ".", ".."):
            continue
        clean.append(p)
    return Path(*clean) if clean else None


def _extract_npm_tarball(data, dest, extract):
    """
    Extract an npm tarball (*.tgz) into dest/ according to the extract spec.

    extract may be:
      None             – extract entire package root (package/**), preserve structure
      "subdir"         – extract package/subdir/**, strip subdir prefix
      [(src,dst), …]   – for each (src, dst):
                           src ending "/" → extract that dir, strip its name,
                                            place contents under dest/dst/
                           src as filename → copy single file to dest/dst/
    """
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            name = member.name
            # All npm tarballs have a leading "package/" component.
            if not name.startswith("package/"):
                continue
            rel = name[len("package/") :]  # path within the package

            dest_rel = _resolve_dest_rel(rel, extract)
            if dest_rel is None:
                continue

            safe = _safe_path(str(dest_rel))
            if safe is None:
                continue

            target = dest / safe
            target.parent.mkdir(parents=True, exist_ok=True)
            fobj = tf.extractfile(member)
            if fobj is None:
                continue
            target.write_bytes(fobj.read())


def _resolve_dest_rel(rel, extract):
    """
    Given a path relative to the package root and an extract spec, return the
    relative path within the destination directory, or None to skip.
    """
    if extract is None:
        # Extract everything as-is.
        return Path(rel)

    if isinstance(extract, str):
        # Single subdirectory: strip its prefix.
        prefix = extract.rstrip("/") + "/"
        if rel.startswith(prefix):
            return Path(rel[len(prefix) :])
        return None

    # List of (src, dst_subdir) pairs.
    for src, dst in extract:
        if src.endswith("/"):
            # Directory: match files under src/, strip src prefix, place in dst/
            if rel.startswith(src):
                tail = rel[len(src) :]
                if dst:
                    return Path(dst) / tail
                return Path(tail)
        else:
            # Single file: exact match, place in dst/ root
            if rel == src or rel.endswith("/" + src):
                filename = Path(src).name
                if dst:
                    return Path(dst) / filename
                return Path(filename)
    return None


def fetch_npm_package(label, pkg, version, extract, dest_root, force=False):
    dest = dest_root / label
    if dest.exists() and not force:
        return "cached"
    dest.mkdir(parents=True, exist_ok=True)
    url = _npm_tarball_url(pkg, version)
    try:
        data = _fetch(url)
    except Exception as exc:
        return f"FETCH FAILED: {exc}"
    try:
        _extract_npm_tarball(data, dest, extract)
    except Exception as exc:
        return f"EXTRACT FAILED: {exc}"
    return "ok"


def fetch_todomvc(examples, dest_root, force=False):
    """Download the todomvc main branch zip and extract the requested examples."""
    already_have = [
        (label, subdir)
        for label, subdir in examples
        if (dest_root / label).exists() and not force
    ]
    to_fetch = [
        (label, subdir)
        for label, subdir in examples
        if (label, subdir) not in [(lbl, sub) for lbl, sub in already_have]
    ]

    if not to_fetch:
        for label, _ in examples:
            print(f"      {label:<30} cached")
        return

    print("  Downloading tastejs/todomvc main branch…", flush=True)
    try:
        data = _fetch(TODOMVC_REPO)
    except Exception as exc:
        for label, _ in to_fetch:
            print(f"  [!!] {label:<30} FETCH FAILED: {exc}")
        return

    need = {subdir: label for label, subdir in to_fetch}

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            # The zip has "todomvc-main/" or "todomvc-{sha}/" as the root prefix.
            root_prefix = names[0].split("/")[0] + "/"

            dist_prefix = TODOMVC_DIST_SUBDIR + "/" if TODOMVC_DIST_SUBDIR else ""
            for name in names:
                if name.endswith("/"):
                    continue  # skip directory entries
                if not name.startswith(root_prefix):
                    continue
                rel = name[
                    len(root_prefix) :
                ]  # e.g. "examples/react/dist/app.bundle.js"
                if not rel.startswith("examples/"):
                    continue
                # rel = "examples/{subdir}/{optional_dist_prefix}{tail}"
                parts = rel.split("/", 3 if dist_prefix else 2)
                if dist_prefix:
                    if len(parts) < 4:
                        continue
                    _, subdir, dist_dir, tail = parts
                    if dist_dir + "/" != dist_prefix:
                        continue
                else:
                    if len(parts) < 3:
                        continue
                    _, subdir, tail = parts
                if subdir not in need:
                    continue
                if not tail:
                    continue
                label = need[subdir]
                safe = _safe_path(tail)
                if safe is None:
                    continue
                target = dest_root / label / safe
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as fobj:
                    target.write_bytes(fobj.read())
    except Exception as exc:
        print(f"  [!!] EXTRACT FAILED: {exc}")
        return

    for label, subdir in already_have:
        print(f"           {label:<30} cached")
    for label, subdir in to_fetch:
        dest = dest_root / label
        js_count = sum(
            1 for p in dest.rglob("*") if p.suffix in {".js", ".mjs"} and p.is_file()
        )
        print(f"  [NEW]      {label:<30} {js_count} JS files")


# ── main ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--output",
        "-o",
        required=True,
        metavar="DIR",
        help="Directory to download libraries into (created if missing)",
    )
    parser.add_argument(
        "--lib",
        nargs="+",
        metavar="LABEL",
        help="Only download these libraries (by label)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download libraries that already exist",
    )
    args = parser.parse_args()

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    want = set(args.lib) if args.lib else None

    # npm packages
    npm_pkgs = [
        (label, pkg, ver, extract)
        for label, pkg, ver, extract in NPM_PACKAGES
        if want is None or label in want
    ]

    todomvc = [
        (label, subdir)
        for label, subdir in TODOMVC_EXAMPLES
        if want is None or label in want
    ]

    total = len(npm_pkgs) + len(todomvc)
    print(f"Downloading {total} libraries into {out}/")

    failures = []
    for i, (label, pkg, version, extract) in enumerate(npm_pkgs, 1):
        status = fetch_npm_package(label, pkg, version, extract, out, force=args.force)
        marker = (
            "      " if status == "cached" else "[NEW] " if status == "ok" else "[!!]  "
        )
        print(f"  {marker} [{i}/{total}] {label:<28} {pkg}@{version}  {status}")
        if status not in ("cached", "ok"):
            failures.append((label, status))

    if todomvc:
        base = len(npm_pkgs) + 1
        print(f"\n  [todomvc] [{base}–{base + len(todomvc) - 1}/{total}]")
        fetch_todomvc(todomvc, out, force=args.force)

    print()
    if failures:
        print(f"Failures ({len(failures)}):")
        for label, status in failures:
            print(f"  {label}: {status}")
        print()

    total_js = sum(
        1 for p in out.rglob("*") if p.suffix in {".js", ".mjs"} and p.is_file()
    )
    total_bytes = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"Total .js/.mjs files: {total_js}")
    print(f"Total size:           {total_bytes / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    main()
