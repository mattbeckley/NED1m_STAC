# Implementation Plan: OSN Failover Redesign

## Background

The current code uses the USGS Product API as the primary source for GeoTIFF URLs,
with the local STAC catalog as a fallback. The USGS API is too unreliable for
production use. Additionally, a local mirror of the USGS data now exists on OSN
(Open Storage Network), which should serve as the failover when USGS resources
are unavailable.

---

## Decisions on Open Questions

1. **`--force_local_stac`**: Kept and repurposed as "use STAC + skip health
   check, trust USGS URLs." Allows bypassing the health check when USGS is known
   to be healthy and you want the fastest path.

2. **`--force_osn` with missing OSN assets**: Permissive — if an item has no OSN
   asset (e.g. a `rockyweb.usgs.gov` tile), fall back to the USGS URL for that
   tile and log a warning. The goal is to force OSN where possible, not to fail
   the whole job over a few unmappable tiles.

3. **General philosophy on force flags**: Both flags exist purely for testing and
   emergency overrides. In normal operation the code auto-detects which source to
   use via the health check. `--force_local_stac` and `--force_osn` are mutually
   exclusive.

---

## Answer to Open Question: Single STAC vs Two STACs

**Single STAC with dual assets is the right approach.** STAC natively supports
multiple assets per item with different keys. Each item already has an
`elevation-geotiff` asset pointing to the USGS URL. A second asset,
`elevation-geotiff-osn`, will be added pointing to the OSN equivalent.
One catalog, two URLs per tile, no duplication of structure.

The URL transformation drops the `StagedProducts/` prefix, since the rclone
sync maps `prd-tnm/StagedProducts/Elevation/1m/` → `ot-usgs-osn/Elevation/1m/`:
```
https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1m/Projects/...
  → https://usgs.osn.mghpcc.org/ot-usgs-osn/Elevation/1m/Projects/...
```

OSN uses an S3-compatible API (Ceph) with path-style addressing and requires
credentials. It is not publicly accessible over plain HTTPS.

---

## Testing Strategy

Long-running operations (especially the one-time OSN migration) carry a high risk
of "run for hours, fail, fix, repeat." The following safeguards are built into
the design to prevent that trap.

### Dry-run mode (`--dry-run`)
Add a `--dry-run` flag to `run_stac_pipeline.py`. In dry-run mode the script:
- Iterates items and logs what it *would* change
- Makes no writes to disk and does not rebuild the index
- Reports a summary: N items would be updated, M items skipped (no USGS URL to map)

Use this first to verify the URL transformation logic is correct before any
long run.

### Small-batch testing
The existing `PROJECTS_TO_PROCESS` config variable already allows targeting a
handful of known projects. Before the full migration:
1. Run `--dry-run` on 2-3 projects to verify URL mapping output looks correct.
2. Run for real on those same 2-3 projects and confirm the catalog saves and the
   R-tree index rebuilds cleanly.
3. Run a query against the updated items using `--force_osn` to confirm OSN URLs
   are accessible and GDAL can read them.
4. Only then kick off the full migration.

### OSN connectivity check before full migration
Before the full migration run, add a pre-flight step that:
- Picks one known-good USGS URL from the existing catalog
- Derives the OSN equivalent
- Does a HEAD request to confirm OSN is reachable and the URL resolves
- Aborts with a clear error if OSN is not accessible — no point running for hours
  if the destination is unreachable

### Progress checkpointing for the migration
The migration iterates potentially thousands of projects. Add checkpointing:
- After each project folder is processed, append its name to a
  `osn_migration_progress.txt` file
- On restart, read that file and skip already-processed projects
- This means a failure mid-run can be resumed rather than restarted from scratch

---

## Phase 1 — Add OSN Assets to STAC (`run_stac_pipeline.py`)

### 1a. Add OSN config constants to `Config`
- `OSN_ENDPOINT = "https://usgs.osn.mghpcc.org"`
- `OSN_BUCKET_NAME = "ot-usgs-osn"`

### 1b. Add URL transformation helper
Add `_usgs_url_to_osn_url(usgs_https_url)` that transforms the URL prefix.
Items whose URLs cannot be mapped (e.g. `rockyweb.usgs.gov` URLs) will log a
warning and return `None` — the OSN asset is simply omitted for those items.

### 1c. Modify `_create_stac_item`
Add the OSN asset (`elevation-geotiff-osn`) alongside the existing USGS asset
(`elevation-geotiff`) when creating new items going forward.

### 1d. Add `--add-osn-assets` migration mode
A one-time migration (with dry-run and checkpointing support) that:
- Optionally runs `--dry-run` to preview changes without writing
- Loads the existing catalog
- Iterates every item, skipping projects already in the checkpoint file
- Adds the OSN asset where missing, logs a warning and continues for unmappable URLs
- Saves progress checkpoint after each project folder
- Saves the updated catalog and rebuilds the R-tree index only after all items
  are processed

---

## Phase 2 — Refactor Query Logic (`NED1m_Query.py`)

### 2a. Add OSN config constants
Add endpoint, bucket name, and GDAL VSI path prefix for OSN to `Config`.
OSN credentials (access key, secret key) must NOT be hardcoded. They will be
read from a config file (e.g. `~/.config/ned1m/osn_credentials.ini` or an
environment variable file) at runtime. The GDAL subprocess environment will be
augmented with:
- `AWS_S3_ENDPOINT=usgs.osn.mghpcc.org`
- `AWS_ACCESS_KEY_ID=<from config>`
- `AWS_SECRET_ACCESS_KEY=<from config>`
- `AWS_VIRTUAL_HOSTING=FALSE` (required for path-style addressing)

GDAL VSI path format for OSN: `/vsis3/ot-usgs-osn/Elevation/1m/Projects/...`

### 2b. Enrich `find_files_local_indexed` return value
Modify to extract both assets from each STAC item, returning:
```python
{'vsi': usgs_vsi, 's3_https': usgs_https, 'osn_vsi': osn_vsi, 'osn_https': osn_https}
```
`osn_vsi` and `osn_https` will be `None` for items that have no OSN asset.

### 2c. Add lightweight USGS health check
Add `_check_usgs_accessible(sample_urls, n=2)` that does HTTP HEAD requests on
2 sample URLs with a short timeout (5s). Returns `True` if both succeed.
The result and timing are logged.

### 2d. Rewrite retrieval flow in `main()`
```
Old flow:  USGS API  →  (fallback) LOCAL STAC

New flow:  LOCAL STAC (always)
              │
              ├─ --force_local_stac → use USGS VSI paths, skip health check
              ├─ --force_osn        → use OSN VSI paths (USGS fallback per-tile
              │                       if OSN asset missing)
              └─ (default)          → health check 2 USGS sample URLs
                                          OK?   → use USGS VSI paths
                                          Fail? → use OSN VSI paths
```

### 2e. Update CLI arguments
- `--force_local_stac`: repurposed — "use STAC + skip health check, trust USGS
  URLs." Mutually exclusive with `--force_osn`.
- `--force_osn`: new flag — skip health check, use OSN URLs directly.
  For tiles missing the OSN asset, fall back to USGS URL and log a warning.

### 2f. Update logging
Clearly log the data source on every run (USGS vs OSN, and whether forced or
auto-detected). Retain all existing timing logs. Add a timing entry for the
health check.

### 2g. `Original_USGS1mTiles_URLs.txt` — always write public USGS URLs
This file is user-facing: users read it to find the original source tiles and
access them directly from the USGS S3 bucket. It must always contain publicly
accessible URLs regardless of which internal source the code used.

Rules:
- **Normal mode (USGS)**: write USGS HTTPS URLs — no change from current behavior.
- **OSN failover mode**: derive the equivalent USGS URL from each OSN URL (reverse
  the OSN→USGS path transformation) and write that instead. The file content is
  identical to what it would have been had USGS been reachable.
- **OSN-only items** (no USGS URL derivable): write the bare filename only,
  stripping the OSN path prefix. Log a warning for each such case. In practice
  this should never happen since OSN is a direct mirror of the USGS bucket, but
  the fallback prevents writing private OSN URLs into a user-facing file.

A comment line at the top of the file will note the retrieval mode used
(e.g. `# Source: USGS S3 (direct)` or `# Source: OSN mirror (USGS URLs shown)`)
so there is an audit trail without exposing OSN internals.

---

## Phase 3 — Apply Same Changes to `RasterNED1mService.py`

This file appears to be a production variant of `NED1m_Query.py`. Once Phase 2
is complete and tested, apply the same changes here. Read the full file before
making any changes.

---

## Sequencing and Risks

| Step | Risk | Mitigation |
|---|---|---|
| Phase 1d migration | Could run for hours and fail | Dry-run first; checkpointing allows resume |
| OSN unreachable at migration time | Hours wasted | Pre-flight connectivity check before starting |
| OSN performance unknown | Could be slow vs USGS | Timing logs on every run; compare baseline |
| HEAD check adds latency | Small delay per query | n=2 with 5s timeout; log the time |
| `rockyweb.usgs.gov` URLs | Cannot derive OSN URL | Log warning, keep USGS-only for those tiles |
| `--force_osn` with missing assets | Some tiles get USGS URL silently | Warning logged per tile; summary count at end |
