#!/usr/bin/env python3
"""
Download static files from live Django sites for use in the django-sites
compatibility report.

For each site the script fetches /static/staticfiles.json (Django's manifest
file), then downloads every non-admin, non-hashed file listed in the manifest's
"paths" key.  The result mirrors the folder structure produced by the original
data-collection session, so you can pass the output directory straight to
js_libs_compat.py (or compat_report.py's django-sites variant).

Usage:
    python download_django_sites.py --output /path/to/django_sites_dir
    python download_django_sites.py --output /path/to/django_sites_dir \
        --site adamghill_com_static
    python download_django_sites.py --output /path/to/django_sites_dir --limit 10

The list of sites was collected by finding public Django deployments that expose
/static/staticfiles.json.  Some sites may have gone offline or changed their
static URL since the corpus was assembled — those will be skipped with a warning.

Requirements: Python 3.9+, no extra packages needed.
"""

import argparse
import json
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

# Use certifi for SSL verification if available (helps on macOS); otherwise
# fall back to the default context which works on most systems.
try:
    import certifi

    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl.create_default_context()

# ── known sites ───────────────────────────────────────────────────────────────
#
# (dir_name, base_static_url)
#
# dir_name  — folder created under the output directory
# base_url  — base URL for the site's static directory (without trailing file)
#
# Note: sites that have gone offline or changed their static URL since corpus
# collection will produce a 404/connection error and be skipped gracefully.
#
SITES = [
    ("adamghill_com_static", "https://adamghill.com/static/"),
    ("askhndigests_com_static", "https://askhndigests.com/static/"),
    ("assets_mofoprod_net_static", "https://assets.mofoprod.net/static/"),
    ("bakeup_org_static", "https://bakeup.org/static/"),
    ("beesocial_dev_static", "https://beesocial.dev/static/"),
    ("bloodcancer_org_uk_static", "https://bloodcancer.org.uk/static/"),
    ("builtwithdjango_com_static", "https://builtwithdjango.com/static/"),
    ("calorietracker_io_static", "https://calorietracker.io/static/"),
    ("cdn_cronitor_io_static", "https://cdn.cronitor.io/static/"),
    ("crowd_loc_gov_static", "https://crowd.loc.gov/static/"),
    ("demo_baby-buddy_net_static", "https://demo.baby-buddy.net/static/"),
    ("demo_django-bridge_org_static", "https://demo.django-bridge.org/static/"),
    ("devmarks_io_static", "https://devmarks.io/static/"),
    ("django_wtf_static", "https://django.wtf/static/"),
    ("dryorm_xterm_info_static", "https://dryorm.xterm.info/static/"),
    ("eachpod_com_static", "https://eachpod.com/static/"),
    ("empleovino_es_static", "https://empleovino.es/static/"),
    ("fediview_com_static", "https://fediview.com/static/"),
    ("filmcliq_com_static", "https://filmcliq.com/static/"),
    ("findwork_dev_static", "https://findwork.dev/static/"),
    ("fotoia_es_static", "https://fotoia.es/static/"),
    ("getdeploying_com_static", "https://getdeploying.com/static/"),
    ("gettjalerts_com_static", "https://gettjalerts.com/static/"),
    ("impresskit_net_static", "https://impresskit.net/static/"),
    ("jobs_django-news_com_static", "https://jobs.django-news.com/static/"),
    ("learningequality_org_static", "https://learningequality.org/static/"),
    ("lifelessons_de_static", "https://lifelessons.de/static/"),
    ("lifeweeks_app_static", "https://lifeweeks.app/static/"),
    ("marketingagents_net_static", "https://marketingagents.net/static/"),
    ("media_lincolnloop_com_static", "https://media.lincolnloop.com/static/"),
    ("motherflocker_app_static", "https://motherflocker.app/static/"),
    ("nononsense_recipes_static", "https://nononsense.recipes/static/"),
    ("octopus_energy_static", "https://octopus.energy/static/"),
    ("osig_app_static", "https://osig.app/static/"),
    ("ovarian_org_uk_static", "https://ovarian.org.uk/static/"),
    ("pontoon_mozilla_org_static", "https://pontoon.mozilla.org/static/"),
    ("pretalx_com_static", "https://pretalx.com/static/"),
    ("pycon_ie_static", "https://pycon.ie/static/"),
    ("python-podcast_de_static", "https://python-podcast.de/static/"),
    ("recodeqr_com_static", "https://recodeqr.com/static/"),
    ("shipwithdjango_com_static", "https://shipwithdjango.com/static/"),
    (
        "static-assets_clubhouseapi_com_static",
        "https://static-assets.clubhouseapi.com/static/",
    ),
    ("teensy_info_static", "https://teensy.info/static/"),
    ("testdriven_io_static", "https://testdriven.io/static/"),
    ("tinyfinch_chat_static", "https://tinyfinch.chat/static/"),
    ("torchbox_com_static", "https://torchbox.com/static/"),
    ("triviaroyale_io_static", "https://triviaroyale.io/static/"),
    ("uifcalculators_co_za_static", "https://uifcalculators.co.za/static/"),
    ("uk_silvercloudhealth_com_static", "https://uk.silvercloudhealth.com/static/"),
    (
        "unco-assets_s3_amazonaws_com_static",
        "https://unco-assets.s3.amazonaws.com/static/",
    ),
    ("usewebhook_com_static", "https://usewebhook.com/static/"),
    ("wagtail_org_static", "https://wagtail.org/static/"),
    ("www_alcottfarm_co_uk_static", "https://www.alcottfarm.co.uk/static/"),
    ("www_alternativas_io_static", "https://www.alternativas.io/static/"),
    (
        "www_bibliotecadigitaldebogota_gov_co_static",
        "https://www.bibliotecadigitaldebogota.gov.co/static/",
    ),
    ("www_buckinghamshire_gov_uk_static", "https://www.buckinghamshire.gov.uk/static/"),
    ("www_bugsink_com_static", "https://www.bugsink.com/static/"),
    ("www_cocktaillove_com_static", "https://www.cocktaillove.com/static/"),
    ("www_consumerfinance_gov_static", "https://www.consumerfinance.gov/static/"),
    ("www_django-unicorn_com_static", "https://www.django-unicorn.com/static/"),
    ("www_lintern_lu_static", "https://www.lintern.lu/static/"),
    ("www_lokerhq_com_static", "https://www.lokerhq.com/static/"),
    ("www_mozilla_org_static", "https://www.mozilla.org/static/"),
    ("www_nesta_org_uk_static", "https://www.nesta.org.uk/static/"),
    ("www_nickmoreton_co_uk_static", "https://www.nickmoreton.co.uk/static/"),
    ("www_outreachy_org_static", "https://www.outreachy.org/static/"),
    ("www_playkordle_com_static", "https://www.playkordle.com/static/"),
    ("www_python_org_static", "https://www.python.org/static/"),
    ("www_rca_ac_uk_static", "https://www.rca.ac.uk/static/"),
    ("www_rkh_co_uk_static", "https://www.rkh.co.uk/static/"),
    ("www_skyz_be_static", "https://www.skyz.be/static/"),
    ("www_theschooldesk_app_static", "https://www.theschooldesk.app/static/"),
    ("www_websitehunt_co_static", "https://www.websitehunt.co/static/"),
    ("zakuchess_com_static", "https://zakuchess.com/static/"),
]


# ── helpers ───────────────────────────────────────────────────────────────────

_USER_AGENT = "django-manifeststaticfiles-enhanced/download-corpus"


def _fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
        return resp.read()


def _safe_rel(path_str):
    """Return a safe relative Path, or None if it would escape the dest dir."""
    parts = Path(path_str).parts
    clean = [p for p in parts if p not in ("", ".", "..")]
    if not clean:
        return None
    p = Path(*clean)
    # Refuse absolute paths and traversal in case of unusual manifest entries.
    if p.is_absolute() or ".." in p.parts:
        return None
    return p


def download_site(
    dir_name, base_url, dest_root, force=False, skip_admin=True, delay=0.1
):
    """
    Download the static files for one site.

    Returns (downloaded, skipped, failed, skip_reason).
    skip_reason is non-None when the whole site should be skipped (e.g.
    manifest unavailable).
    """
    dest = dest_root / dir_name

    manifest_url = base_url.rstrip("/") + "/staticfiles.json"
    try:
        raw = _fetch(manifest_url)
    except urllib.error.HTTPError as exc:
        return 0, 0, 0, f"manifest HTTP {exc.code}"
    except Exception as exc:
        return 0, 0, 0, f"manifest fetch failed: {exc}"

    try:
        manifest = json.loads(raw)
        paths = manifest.get("paths", {})
    except Exception as exc:
        return 0, 0, 0, f"manifest parse failed: {exc}"

    if not paths:
        return 0, 0, 0, "manifest has no paths"

    dest.mkdir(parents=True, exist_ok=True)

    downloaded = skipped = failed = 0

    for rel_path in sorted(paths.keys()):
        if skip_admin and (
            rel_path.startswith("admin/") or rel_path.startswith("admin\\")
        ):
            skipped += 1
            continue

        safe = _safe_rel(rel_path)
        if safe is None:
            skipped += 1
            continue

        out_file = dest / safe
        if out_file.exists() and not force:
            skipped += 1
            continue

        file_url = base_url.rstrip("/") + "/" + rel_path
        try:
            data = _fetch(file_url)
        except Exception:
            failed += 1
            continue

        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_bytes(data)
        downloaded += 1

        if delay:
            time.sleep(delay)

    return downloaded, skipped, failed, None


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
        help="Directory to download site files into (created if missing)",
    )
    parser.add_argument(
        "--site",
        nargs="+",
        metavar="SITE",
        help="Only download these sites (by dir_name, e.g. wagtail_org_static)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Only download the first N sites",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download files that already exist",
    )
    parser.add_argument(
        "--include-admin",
        action="store_true",
        help="Include Django admin static files (excluded by default)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.1,
        metavar="SEC",
        help="Delay between file downloads in seconds (default 0.1; set 0 to disable)",
    )
    args = parser.parse_args()

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    sites = SITES
    if args.site:
        want = set(args.site)
        sites = [(d, u) for d, u in sites if d in want]
    if args.limit:
        sites = sites[: args.limit]

    total = len(sites)
    print(f"Downloading static files from {total} Django sites into {out}/")
    print("(Sites that have gone offline or changed their URL will be skipped)\n")

    ok_count = skip_count = fail_count = 0

    for i, (dir_name, base_url) in enumerate(sites, 1):
        print(f"  [{i:>2}/{total}] {dir_name}…", flush=True)
        downloaded, skipped, failed, skip_reason = download_site(
            dir_name,
            base_url,
            out,
            force=args.force,
            skip_admin=not args.include_admin,
            delay=args.delay,
        )
        if skip_reason:
            print(f"           SKIP: {skip_reason}")
            skip_count += 1
        else:
            print(
                f"           +{downloaded} downloaded,"
                f" {skipped} skipped, {failed} failed"
            )
            if failed:
                fail_count += 1
            else:
                ok_count += 1

    print(
        f"\nDone: {ok_count} sites complete, {skip_count} skipped (offline/changed), "
        f"{fail_count} with partial failures"
    )

    total_files = sum(1 for p in out.rglob("*") if p.is_file())
    total_bytes = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"Total files: {total_files}")
    print(f"Total size:  {total_bytes / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    main()
