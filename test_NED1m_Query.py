#!/usr/bin/env python3
# -----------------------------------------------------------------------------
# Test Suite for NED1m_Query.py
# -----------------------------------------------------------------------------
#
# Purpose:
# Exercises NED1m_Query.py across all output formats (GTIFF, HFA, AAIGRID)
# and both data sources (USGS S3 and OSN mirror), using geographically diverse
# AOIs across the continental US. Also includes large-area jobs to stress-test
# big outputs and verify that HFA correctly creates a .ige sidecar file when
# output exceeds 2GB.
#
# After each test, inspect the output in QGIS by extracting the tarball:
#   tar -xzf rasters_USGS1m.tar.gz
#
# Usage:
#   python test_NED1m_Query.py              # run all tests sequentially
#   python test_NED1m_Query.py 1 3 7        # run specific tests by number
#   python test_NED1m_Query.py --list       # list all tests without running
#
# All outputs are written to:
#   /datastaging/matt/testing/STAC1m/Redesign/<test_name>/
#
# Notes on expected output sizes:
#   GTIFF  (DEFLATE-compressed): smallest on disk, good for most AOI sizes.
#   HFA    (uncompressed .img) : grows fast; a 0.3 x 0.3 deg box at 47N is
#                                ~3GB and will trigger a .ige sidecar file.
#   AAIGRID (ASCII .asc)       : very large — ASCII float per pixel, roughly
#                                10-12 bytes each. Keep AOIs small for this
#                                format or expect multi-GB .asc files.
#
# If a test returns no URLs, the AOI likely falls in a gap in the catalog
# for that region. Adjust minlon/maxlon/etc. to a nearby area with coverage.
#
# Prerequisites: same Python environment used to run NED1m_Query.py
# -----------------------------------------------------------------------------

import subprocess
import sys
import tarfile
import time
from pathlib import Path
from datetime import datetime

BASE_DIR    = Path("/datastaging/matt/testing/STAC1m/Redesign")
QUERY_SCRIPT = Path("/home/beckley/NED/NED1m_STAC/NED1m_Query.py")
PYTHON      = sys.executable

# -----------------------------------------------------------------------------
# Test definitions
# Each entry specifies an AOI, output format, and data source override.
# source_flag: "--force_local_stac", "--force_osn", or None (default health check)
# -----------------------------------------------------------------------------
TEST_CASES = [

    # ---- GeoTIFF tests -------------------------------------------------------
    {
        "name":        "01_WA_Cascades_GTIFF_USGS",
        "description": "WA Eastern Cascades — GeoTIFF, USGS S3 (forced)",
        "minlon": -120.50, "minlat": 47.50, "maxlon": -120.30, "maxlat": 47.65,
        "format": "GTIFF",
        "source_flag": "--force_local_stac",
    },
    {
        "name":        "02_MT_Billings_GTIFF_OSN",
        "description": "MT Billings area — GeoTIFF, OSN mirror (forced)",
        "minlon": -108.80, "minlat": 45.70, "maxlon": -108.60, "maxlat": 45.85,
        "format": "GTIFF",
        "source_flag": "--force_osn",
    },
    {
        "name":        "03_NY_Adirondacks_GTIFF_default",
        "description": "NY Adirondacks — GeoTIFF, default health check (tests auto source selection)",
        "minlon": -74.00, "minlat": 44.71, "maxlon": -73.80, "maxlat": 44.78,
        "format": "GTIFF",
        "source_flag": None,
    },

    # ---- HFA (Erdas Imagine .img) tests --------------------------------------
    {
        "name":        "04_NE_Lincoln_HFA_USGS",
        "description": "NE Lincoln area — HFA, USGS S3 (forced)",
        "minlon": -96.80, "minlat": 40.70, "maxlon": -96.60, "maxlat": 40.85,
        "format": "HFA",
        "source_flag": "--force_local_stac",
    },
    {
        "name":        "05_WY_Laramie_HFA_OSN",
        "description": "WY Laramie area — HFA, OSN mirror (forced)",
        "minlon": -105.60, "minlat": 41.30, "maxlon": -105.40, "maxlat": 41.45,
        "format": "HFA",
        "source_flag": "--force_osn",
    },

    # ---- AAIGrid (Arc ASCII Grid .asc) tests ---------------------------------
    # Smaller AOIs used here because AAIGrid is uncompressed ASCII (~11 bytes/pixel).
    {
        "name":        "06_CO_Rockies_AAIGRID_USGS",
        "description": "CO Rocky Mountains — AAIGrid, USGS S3 (forced); small AOI (~100MB .asc)",
        "minlon": -105.50, "minlat": 39.60, "maxlon": -105.40, "maxlat": 39.68,
        "format": "AAIGRID",
        "source_flag": "--force_local_stac",
    },
    {
        "name":        "07_MN_TwinCities_AAIGRID_OSN",
        "description": "MN Twin Cities area — AAIGrid, OSN mirror (forced); small AOI (~100MB .asc)",
        "minlon": -93.30, "minlat": 44.90, "maxlon": -93.20, "maxlat": 44.98,
        "format": "AAIGRID",
        "source_flag": "--force_osn",
    },

    # ---- Large job tests -----------------------------------------------------
    {
        "name":        "08_MT_Large_HFA_USGS_ige_test",
        "description": (
            "Large MT area — HFA, USGS S3 — ~3GB uncompressed output. "
            "Tests that the HFA .ige sidecar file is created and archived correctly."
        ),
        # 0.3 x 0.3 deg at ~47N: ~33400 x 23300 px = ~3.1GB uncompressed (float32)
        "minlon": -109.00, "minlat": 45.50, "maxlon": -108.70, "maxlat": 45.80,
        "format": "HFA",
        "source_flag": "--force_local_stac",
    },
    {
        "name":        "09_NE_Large_GTIFF_USGS",
        "description": (
            "Large NE statewide area — GeoTIFF, USGS S3 — large output stress test. "
            "Nebraska is flat so compression is good; expect several hundred MB."
        ),
        # 0.3 x 0.3 deg at ~41N: ~33400 x 19000 px; compressed GTIFF ~300-600MB
        "minlon": -98.00, "minlat": 40.50, "maxlon": -97.70, "maxlat": 40.80,
        "format": "GTIFF",
        "source_flag": "--force_local_stac",
    },
    {
        "name":        "10_MT_Large_HFA_OSN_ige_test",
        "description": (
            "Large MT area — HFA, OSN mirror — same AOI as test 08 but via OSN. "
            "Verifies .ige handling and OSN throughput on a large job."
        ),
        "minlon": -109.00, "minlat": 45.50, "maxlon": -108.70, "maxlat": 45.80,
        "format": "HFA",
        "source_flag": "--force_osn",
    },
]


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def format_bytes(n):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if n < 1024 or unit == 'TB':
            return f"{n:.1f} {unit}"
        n /= 1024


def tarball_contents(tar_path):
    """Return list of (name, size_bytes) for every member in the tarball."""
    try:
        with tarfile.open(tar_path, 'r:gz') as tar:
            return [(m.name, m.size) for m in tar.getmembers()]
    except Exception as e:
        return [(f"ERROR reading tarball: {e}", 0)]


def run_test(test, index, total):
    out_dir = BASE_DIR / test["name"]
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        PYTHON, str(QUERY_SCRIPT),
        "--minlon", str(test["minlon"]),
        "--minlat", str(test["minlat"]),
        "--maxlon", str(test["maxlon"]),
        "--maxlat", str(test["maxlat"]),
        "--output_dir", str(out_dir),
        "--output_format", test["format"],
    ]
    if test.get("source_flag"):
        cmd.append(test["source_flag"])

    print(f"\n{'='*72}")
    print(f"Test {index}/{total}: {test['name']}")
    print(f"{test['description']}")
    print(f"AOI    : ({test['minlon']}, {test['minlat']}) -> ({test['maxlon']}, {test['maxlat']})")
    print(f"Format : {test['format']}   Source: {test.get('source_flag') or 'default (health check)'}")
    print(f"Output : {out_dir}")
    print(f"{'='*72}")

    start = time.time()
    result = subprocess.run(cmd)
    elapsed = time.time() - start

    tarball = out_dir / "rasters_USGS1m.tar.gz"
    contents = []
    has_ige = False
    tarball_size = 0

    if tarball.exists():
        tarball_size = tarball.stat().st_size
        contents = tarball_contents(tarball)
        has_ige = any(name.endswith('.ige') for name, _ in contents)

    success = result.returncode == 0 and tarball.exists()

    print(f"\n--- {'PASS' if success else 'FAIL'} ({elapsed:.0f}s) ---")
    if tarball.exists():
        print(f"Tarball : {format_bytes(tarball_size)}")
        print("Contents:")
        for name, size in contents:
            note = "  <-- HFA sidecar (output exceeded 2GB)" if name.endswith('.ige') else ""
            print(f"  {name:<55} {format_bytes(size)}{note}")
    else:
        print("No tarball found — check the log in the output directory for errors.")

    return {
        "name":         test["name"],
        "description":  test["description"],
        "success":      success,
        "returncode":   result.returncode,
        "elapsed":      elapsed,
        "tarball_size": tarball_size,
        "has_ige":      has_ige,
    }


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main():
    args = sys.argv[1:]

    if "--list" in args:
        print(f"\n{'#':<5} {'Name':<45} Format   Source")
        print(f"{'-'*5} {'-'*45} {'-'*7}  {'-'*25}")
        for i, t in enumerate(TEST_CASES, 1):
            src = t.get("source_flag") or "default"
            print(f"{i:<5} {t['name']:<45} {t['format']:<8} {src}")
        return

    if args:
        try:
            indices = [int(a) - 1 for a in args]
            tests_to_run = [TEST_CASES[i] for i in indices]
        except (ValueError, IndexError):
            print(f"Usage: {sys.argv[0]} [test_number ...]  (1-{len(TEST_CASES)}), or --list")
            sys.exit(1)
    else:
        tests_to_run = TEST_CASES

    print(f"\nNED1m_Query.py Test Suite")
    print(f"Started : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Running : {len(tests_to_run)} of {len(TEST_CASES)} test(s)")
    print(f"Output  : {BASE_DIR}")

    BASE_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    for i, test in enumerate(tests_to_run, 1):
        r = run_test(test, i, len(tests_to_run))
        results.append(r)

    # ---- Summary table -------------------------------------------------------
    print(f"\n\n{'='*72}")
    print("SUMMARY")
    print(f"{'='*72}")
    print(f"{'#':<4} {'Test':<42} {'Status':<6} {'Time':>7} {'Tarball':>9} {'IGE':>5}")
    print(f"{'-'*4} {'-'*42} {'-'*6} {'-'*7} {'-'*9} {'-'*5}")
    for i, r in enumerate(results, 1):
        status   = "PASS" if r["success"] else "FAIL"
        ige      = "YES" if r["has_ige"] else "-"
        size_str = format_bytes(r["tarball_size"]) if r["tarball_size"] else "-"
        print(f"{i:<4} {r['name']:<42} {status:<6} {r['elapsed']:>6.0f}s {size_str:>9} {ige:>5}")

    passed = sum(1 for r in results if r["success"])
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} passed", end="")
    if failed:
        print(f"  —  {failed} FAILED: " + ", ".join(r['name'] for r in results if not r['success']))
    else:
        print("  —  all tests passed")
    print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
