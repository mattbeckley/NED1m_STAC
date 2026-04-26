# NED1m_STAC

Local STAC catalog and query tools for USGS 1-meter Digital Elevation Model (DEM) data.

## Purpose

The USGS distributes 1-meter DEM tiles as Cloud-Optimized GeoTIFFs (COGs) on a public
S3 bucket (`prd-tnm`). This repository builds and maintains a local STAC
(SpatioTemporal Asset Catalog) of those tiles, enabling fast spatial queries without
relying on the USGS Product API (which has a history of outages and unreliable
response times).

Each item in the catalog stores two asset URLs per tile:

- `elevation-geotiff` — public USGS S3 URL (`https://prd-tnm.s3.amazonaws.com/...`)
- `elevation-geotiff-osn` — OSN mirror URL (`https://usgs.osn.mghpcc.org/...`)

The OSN (Open Storage Network) mirror is a full copy of the USGS prd-tnm bucket
maintained via rclone copy. It serves as an automatic failover when USGS S3 is
unavailable. OSN is not publicly accessible.

---

## Files

### Core Scripts

**`run_stac_pipeline.py`**
Builds, updates, and indexes the local STAC catalog, and provides a one-time
migration to add OSN mirror URLs to existing catalog items.

- **Default (no flags):** Auto-detects create vs. update mode. Create mode processes
  all USGS project folders from S3 from scratch. Update mode adds only new project
  folders not yet in the catalog. Both modes write `elevation-geotiff` and
  `elevation-geotiff-osn` assets to every item. The R-tree spatial index is rebuilt
  only when the catalog actually changes. Run weekly via cron.
- **`--add-osn-assets`:** One-time migration that backfills the `elevation-geotiff-osn`
  asset on all existing items created before OSN support was added. Supports
  `--dry-run` to preview changes without writing, and checkpoints after each project
  folder so an interrupted run can be resumed.
- **`--stac_catalog_path PATH`:** Override the catalog directory for
  `--add-osn-assets` (useful for running the migration against a test catalog first).
- **`--dry-run`:** Preview changes without writing (only applies to `--add-osn-assets`).

**`NED1m_Query.py`**
Queries the local STAC catalog for 1m DEM tiles intersecting a given AOI, then uses
GDAL to produce a subsetted output raster. This is the primary production query script.

Data source selection (default checks USGS first, falls over to OSN automatically):
- **Default:** Lightweight USGS health check (HEAD on 2 sample URLs, 5s timeout).
  Uses USGS S3 if healthy; automatically fails over to OSN if not.
- **`--force_local_stac`:** Skip health check, use USGS S3 VSI paths directly.
- **`--force_osn`:** Skip health check, use OSN VSI paths directly.
- **`--stac_catalog_path PATH`:** Override the catalog path (useful for testing).

Output is bundled into `rasters_USGS1m.tar.gz` containing the raster and a
`Original_USGS1mTiles_URLs.txt` file listing the public USGS S3 URLs for all
source tiles (always USGS URLs regardless of which internal source was used).

**`run_weekly_stac_update.sh`**
Bash wrapper for the weekly cron job. Backs up the existing catalog to
`/data/matt/NED1m_STAC_BU/` via rsync, activates the `stac` conda environment,
and runs `run_stac_pipeline.py`. Configured to run via crontab.

**`test_NED1m_Query.py`**
Test suite for `NED1m_Query.py`. Exercises all three output formats (GeoTIFF, HFA,
AAIGrid) against both data sources (USGS S3 and OSN mirror) using geographically
diverse AOIs across the continental US. Includes large-area jobs to stress-test big
outputs and verify that the HFA `.ige` sidecar file is correctly created and archived
when output exceeds 2GB. All outputs go to `/datastaging/matt/testing/STAC1m/Redesign/`.

```bash
python test_NED1m_Query.py          # run all 10 tests
python test_NED1m_Query.py 1 3 5    # run specific tests by number
python test_NED1m_Query.py --list   # list all tests without running
```


### Documentation

**`ImplementationPlan.md`**
Design document for the OSN failover redesign. Covers architecture decisions
(single STAC with dual assets), the URL transformation between USGS S3 and OSN,
the phased implementation plan (Phase 1: catalog/pipeline, Phase 2: query script,
Phase 3: production service), and testing strategy.

**`redesign.md`**
Earlier design notes and open questions that fed into `ImplementationPlan.md`.

---

## Catalog Location

| Path | Contents |
|------|----------|
| `/data/matt/NED1m_STAC/` | Production catalog, R-tree index, logs, credentials |
| `/data/matt/NED1m_STAC_BU/` | Weekly rsync backup of the production catalog |
| `/data/matt/NED1m_STAC_test/` | Small test catalog (3 projects) used during development |

---

## Dependencies

| Package | Used by |
|---------|---------|
| `boto3` | `run_stac_pipeline.py` — S3 access |
| `pystac` | both pipeline and query scripts |
| `shapely` | `run_stac_pipeline.py` — geometry handling |
| `rtree` | both scripts — spatial indexing |
| `requests` | `NED1m_Query.py` — USGS health check |
| `gdal` (via subprocess) | `NED1m_Query.py` — raster processing |

Python environment: `stac` conda env (pipeline) / `workGDAL3ten` conda env (query/GDAL).
