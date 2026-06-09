#!/usr/bin/env python3
"""
Discover Django packages that ship static CSS/JS files.

Scans all packages on djangopackages.org, then checks their PyPI wheels
for static files using HTTP range requests — no full wheel download needed.

Requirements:
    pip install aiohttp certifi

Output:
    discovery_results.json       — all packages with full metadata
    packages_with_static.csv     — only packages that have static CSS/JS
"""

import asyncio
import csv
import json
import re
import ssl
import struct
import sys
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Optional

import aiohttp
import certifi

# ── ZIP constants ────────────────────────────────────────────────────────────
EOCD_SIG = b"PK\x05\x06"
CD_ENTRY_SIG = b"PK\x01\x02"
EOCD_FIXED_SIZE = 22
MAX_ZIP_COMMENT = 65535
# Fetch this many bytes from the end of the file to locate EOCD + central dir.
# 128 KB covers the EOCD + most central directories in one shot.
TAIL_FETCH_SIZE = 131072

# ── API endpoints ────────────────────────────────────────────────────────────
DJANGOPKG_API = "https://djangopackages.org/api/v3/packages/"
PYPI_JSON_API = "https://pypi.org/pypi/{name}/json"

STATIC_EXTENSIONS = {".css", ".js"}


@dataclass
class PackageResult:
    slug: str
    pypi_name: str
    repo_watchers: int
    usage_count: int
    repo_url: str = ""
    has_static: bool = False
    static_file_count: int = 0
    static_extensions: list = field(default_factory=list)
    error: Optional[str] = None


# ── djangopackages.org ───────────────────────────────────────────────────────


async def fetch_all_packages(session: aiohttp.ClientSession) -> list[dict]:
    packages: list[dict] = []
    params = {"format": "json", "limit": 100, "offset": 0}

    while True:
        async with session.get(DJANGOPKG_API, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()

        packages.extend(data["objects"])
        print(f"  fetched {len(packages)} packages...", end="\r", flush=True)

        if data["meta"]["next"] is None:
            break
        params["offset"] += params["limit"]
        await asyncio.sleep(1)

    print()
    return packages


def extract_pypi_name(package: dict) -> Optional[str]:
    """Derive the PyPI package name from the djangopackages entry."""
    pypi_url = package.get("pypi_url") or ""
    if pypi_url:
        # Handles both:
        #   https://pypi.org/project/django-debug-toolbar/
        #   https://pypi.python.org/pypi/django-debug-toolbar
        m = re.search(r"/(?:project|pypi)/([^/]+)/?$", pypi_url)
        if m:
            return m.group(1)
    # Fall back to slug — often identical to the PyPI name
    return package.get("slug") or None


# ── PyPI ─────────────────────────────────────────────────────────────────────


async def get_wheel_url(session: aiohttp.ClientSession, name: str) -> Optional[str]:
    url = PYPI_JSON_API.format(name=name)
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:
        return None

    for file_info in data.get("urls", []):
        if file_info["filename"].endswith(".whl"):
            return file_info["url"]
    return None


# ── ZIP range-request inspection ─────────────────────────────────────────────


def _find_eocd(tail: bytes) -> Optional[int]:
    """Search backwards through tail bytes for the EOCD signature."""
    # Start from the earliest possible EOCD position and work backward.
    search_start = max(0, len(tail) - EOCD_FIXED_SIZE - MAX_ZIP_COMMENT)
    pos = len(tail) - EOCD_FIXED_SIZE
    while pos >= search_start:
        if tail[pos : pos + 4] == EOCD_SIG:
            comment_len = struct.unpack_from("<H", tail, pos + 20)[0]
            if pos + EOCD_FIXED_SIZE + comment_len == len(tail):
                return pos
        pos -= 1
    return None


def _parse_eocd(tail: bytes, eocd_pos: int) -> tuple[int, int]:
    """Return (cd_offset, cd_size) from the EOCD record."""
    cd_size = struct.unpack_from("<I", tail, eocd_pos + 12)[0]
    cd_offset = struct.unpack_from("<I", tail, eocd_pos + 16)[0]
    return cd_offset, cd_size


def _parse_cd_filenames(cd_data: bytes) -> list[str]:
    """Extract all filenames from the ZIP central directory bytes."""
    filenames: list[str] = []
    pos = 0
    while pos + 46 <= len(cd_data):
        if cd_data[pos : pos + 4] != CD_ENTRY_SIG:
            break
        fname_len = struct.unpack_from("<H", cd_data, pos + 28)[0]
        extra_len = struct.unpack_from("<H", cd_data, pos + 30)[0]
        comment_len = struct.unpack_from("<H", cd_data, pos + 32)[0]
        fname_bytes = cd_data[pos + 46 : pos + 46 + fname_len]
        filenames.append(fname_bytes.decode("utf-8", errors="replace"))
        pos += 46 + fname_len + extra_len + comment_len
    return filenames


def _check_static(filenames: list[str]) -> tuple[bool, int, list[str]]:
    """Return (has_static, count, sorted_extensions) for static CSS/JS files."""
    exts: set[str] = set()
    count = 0
    for fname in filenames:
        path = PurePosixPath(fname)
        if "static" in path.parts:
            ext = path.suffix.lower()
            if ext in STATIC_EXTENSIONS:
                count += 1
                exts.add(ext)
    return bool(count), count, sorted(exts)


async def inspect_wheel(
    session: aiohttp.ClientSession, wheel_url: str
) -> tuple[bool, int, list[str], Optional[str]]:
    """
    Use HTTP range requests to inspect a wheel's zip central directory.
    Returns (has_static, count, extensions, error_or_None).
    """
    # HEAD to get file size
    try:
        async with session.head(wheel_url, allow_redirects=True) as resp:
            content_length = int(resp.headers.get("Content-Length", 0))
    except Exception as e:
        return False, 0, [], f"head_failed: {e}"

    if not content_length:
        return False, 0, [], "no_content_length"

    # Fetch the tail — large enough to cover EOCD + central directory
    fetch_size = min(TAIL_FETCH_SIZE, content_length)
    tail_start = content_length - fetch_size
    try:
        headers = {"Range": f"bytes={tail_start}-{content_length - 1}"}
        async with session.get(wheel_url, headers=headers) as resp:
            tail = await resp.read()
    except Exception as e:
        return False, 0, [], f"range_failed: {e}"

    eocd_pos = _find_eocd(tail)
    if eocd_pos is None:
        return False, 0, [], "eocd_not_found"

    cd_offset, cd_size = _parse_eocd(tail, eocd_pos)

    # If the central directory falls within our already-fetched tail, use it.
    if cd_offset >= tail_start:
        cd_in_tail = cd_offset - tail_start
        cd_data = tail[cd_in_tail : cd_in_tail + cd_size]
    else:
        # Need a second range request for the central directory.
        try:
            headers = {"Range": f"bytes={cd_offset}-{cd_offset + cd_size - 1}"}
            async with session.get(wheel_url, headers=headers) as resp:
                cd_data = await resp.read()
        except Exception as e:
            return False, 0, [], f"cd_fetch_failed: {e}"

    filenames = _parse_cd_filenames(cd_data)
    has_static, count, exts = _check_static(filenames)
    return has_static, count, exts, None


# ── Per-package orchestration ─────────────────────────────────────────────────


async def check_package(
    session: aiohttp.ClientSession,
    package: dict,
    semaphore: asyncio.Semaphore,
) -> PackageResult:
    slug = package["slug"]
    pypi_name = extract_pypi_name(package) or slug
    result = PackageResult(
        slug=slug,
        pypi_name=pypi_name,
        repo_watchers=package.get("repo_watchers") or 0,
        usage_count=package.get("usage_count") or 0,
        repo_url=package.get("repo_url") or "",
    )

    async with semaphore:
        await asyncio.sleep(0.5)
        wheel_url = await get_wheel_url(session, pypi_name)
        if not wheel_url:
            result.error = "no_wheel"
            return result

        has_static, count, exts, err = await inspect_wheel(session, wheel_url)
        if err:
            result.error = err
        else:
            result.has_static = has_static
            result.static_file_count = count
            result.static_extensions = exts

    return result


# ── Main ──────────────────────────────────────────────────────────────────────


async def main() -> None:
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(limit=50, ssl=ssl_ctx)
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        print("Fetching package list from djangopackages.org...")
        packages = await fetch_all_packages(session)
        total = len(packages)
        print(f"Found {total} packages. Inspecting wheels for static files...")

        semaphore = asyncio.Semaphore(3)
        tasks = [check_package(session, pkg, semaphore) for pkg in packages]

        results: list[PackageResult] = []
        completed = 0
        for coro in asyncio.as_completed(tasks):
            result = await coro
            results.append(result)
            completed += 1
            if completed % 50 == 0 or completed == total:
                with_static = sum(1 for r in results if r.has_static)
                print(
                    f"  {completed}/{total} checked"
                    f" — {with_static} with static files so far",
                    flush=True,
                )

    results.sort(key=lambda r: r.repo_watchers, reverse=True)

    # ── Write full results ────────────────────────────────────────────────────
    with open("discovery_results.json", "w") as f:
        json.dump([asdict(r) for r in results], f, indent=2)

    # ── Write filtered CSV ────────────────────────────────────────────────────
    with_static = [r for r in results if r.has_static]
    fieldnames = [
        "slug",
        "pypi_name",
        "repo_watchers",
        "usage_count",
        "static_file_count",
        "static_extensions",
        "repo_url",
    ]
    with open("packages_with_static.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in with_static:
            writer.writerow(
                {
                    "slug": r.slug,
                    "pypi_name": r.pypi_name,
                    "repo_watchers": r.repo_watchers,
                    "usage_count": r.usage_count,
                    "static_file_count": r.static_file_count,
                    "static_extensions": ",".join(r.static_extensions),
                    "repo_url": r.repo_url,
                }
            )

    # ── Summary ───────────────────────────────────────────────────────────────
    errors = [r for r in results if r.error and r.error != "no_wheel"]
    no_wheel = sum(1 for r in results if r.error == "no_wheel")

    print(f"\n{'─' * 72}")
    print(f"Total packages:          {total}")
    print(f"No wheel on PyPI:        {no_wheel}")
    print(f"Inspection errors:       {len(errors)}")
    print(f"With static CSS/JS:      {len(with_static)}")
    print(f"{'─' * 72}")
    print("Full results:            discovery_results.json")
    print("Packages with static:    packages_with_static.csv")

    if with_static:
        print("\nTop 20 by GitHub stars:")
        print(f"{'Package':<40} {'Stars':>6} {'Usage':>6} {'Files':>6}  Exts")
        print("─" * 72)
        for r in with_static[:20]:
            exts = ",".join(r.static_extensions)
            print(
                f"{r.slug:<40} {r.repo_watchers:>6}"
                f" {r.usage_count:>6} {r.static_file_count:>6}  {exts}"
            )


if __name__ == "__main__":
    if sys.version_info < (3, 9):
        sys.exit("Python 3.9+ required")
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        sys.exit("Run: pip install aiohttp certifi")
    asyncio.run(main())
