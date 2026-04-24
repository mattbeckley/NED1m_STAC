import argparse
import configparser
import json
import logging
import os
import pickle
import re
import subprocess
import sys
import tarfile
from datetime import datetime
from pathlib import Path
import requests
import pystac
from rtree import index

# GDAL/OGR operations are done via subprocess calls to GDAL binaries.

# -----------------------------------------------------------------------------
# NED 1m Elevation Query Script
# -----------------------------------------------------------------------------
#
# Purpose:
# Retrieves USGS 1-meter Digital Elevation Model (DEM) data for a specified
# Area of Interest (AOI) from a local STAC catalog, then uses GDAL to produce
# a subsetted output raster.
#
# Background — Why the USGS Product API was removed:
# Earlier versions of this script used the USGS National Map Product API as the
# primary data source, with the local STAC catalog as a fallback. The USGS API
# proved too unreliable for production use (frequent timeouts, outages, and
# inconsistent responses). The script now always uses the local STAC catalog,
# which contains both USGS S3 URLs and OSN mirror URLs for every tile. A local
# mirror of the USGS data on OSN (Open Storage Network) serves as the failover
# when USGS S3 is unavailable.
#
# Each STAC item contains two assets:
#   - elevation-geotiff     : public USGS S3 URL (https://prd-tnm.s3.amazonaws.com/...)
#   - elevation-geotiff-osn : OSN mirror URL     (https://usgs.osn.mghpcc.org/...)
#
# Data Source Selection:
# By default the script checks USGS S3 first and only fails over to OSN if
# USGS is unreachable. Two override flags exist for testing and emergencies.
#
#   (default)             Perform a lightweight USGS health check (HEAD requests
#                         on 2 sample URLs with a 5s timeout). If USGS responds
#                         OK, use USGS S3 VSI paths. If the check fails or times
#                         out, automatically fail over to OSN and log a warning.
#   --force_local_stac    Skip health check; use USGS S3 VSI paths directly.
#                         Use when USGS is known to be healthy and you want to
#                         skip the small overhead of the health check.
#   --force_osn           Skip health check; use OSN VSI paths directly. For
#                         any tile missing an OSN asset, falls back to the USGS
#                         VSI path and logs a warning.
#
# --force_local_stac and --force_osn are mutually exclusive.
#
# OSN Access:
# OSN is not publicly accessible. When OSN paths are used, GDAL subprocesses
# are given S3-compatible credentials via environment variables read from
# Config.OSN_CREDENTIALS_FILE (an .ini file, never committed to git).
#
# Outputs (bundled into rasters_USGS1m.tar.gz):
#   - output_USGS1m.tif/.asc/.img  : raster subsetted to the AOI
#   - Original_USGS1mTiles_URLs.txt : public USGS S3 URLs for all source tiles.
#     Always contains USGS URLs regardless of which internal source was used,
#     with a comment line noting the retrieval mode for audit purposes.
#
# Temporary files (deleted by default, kept with --keep_temp_files):
#   - AOI_tiff_VSI_URLs.txt : GDAL VSI paths passed to gdalbuildvrt
#   - tmp.vrt               : virtual raster built from the VSI path list
#
# Command-Line Arguments:
#   --minlon FLOAT         Minimum longitude of the AOI (EPSG:4326).
#                          Default: -74.00
#   --minlat FLOAT         Minimum latitude of the AOI (EPSG:4326).
#                          Default: 44.705843
#   --maxlon FLOAT         Maximum longitude of the AOI (EPSG:4326).
#                          Default: -73.8
#   --maxlat FLOAT         Maximum latitude of the AOI (EPSG:4326).
#                          Default: 44.782501
#   --output_dir PATH      Directory for all outputs. Must already exist.
#                          Default: /data/matt/testing/STAC1m/test1
#   --output_format STR    Output raster format: GTIFF, AAIGRID, or HFA.
#                          Default: GTIFF
#   --keep_temp_files      If set, do not delete AOI_tiff_VSI_URLs.txt and
#                          tmp.vrt after the run. Default: False (deleted).
#   --stac_catalog_path PATH
#                          Override the local STAC catalog path. Useful for
#                          testing against a non-production catalog.
#                          Default: /data/matt/NED1m_STAC/catalog.json
#   --force_local_stac     Skip health check; use USGS S3 VSI paths from the
#                          local STAC. Mutually exclusive with --force_osn.
#                          Default: False
#   --force_osn            Skip health check; use OSN VSI paths from the local
#                          STAC. Falls back to USGS per tile if OSN asset is
#                          missing. Mutually exclusive with --force_local_stac.
#                          Default: False
#
# How it Works:
# 1. Parse arguments and configure logging to OUTPUT_DIR and stdout.
# 2. Query the local STAC R-tree index for all tiles intersecting the AOI.
#    Each result includes both USGS and OSN VSI paths.
# 3. Select data source: default health check, or forced via flag (see above).
# 4. Build the GDAL VSI path list and write USGS public URLs to the URL file.
# 5. Run gdalbuildvrt to assemble a virtual mosaic from the VSI paths.
# 6. Run gdal_translate to subset the mosaic to the AOI and write the output
#    raster. OSN runs pass S3 credentials via subprocess environment variables.
# 7. Bundle outputs into rasters_USGS1m.tar.gz and remove originals.
# 8. Optionally clean up temporary files.
#
# Prerequisites: pystac, rtree, requests, gdal (via subprocess)
# Last Substantial Modification: 2026-04-24
#
# -----------------------------------------------------------------------------


# ----------------------------------------------------------------------
# Configuration & Constants
# ----------------------------------------------------------------------
class Config:
    # Paths to GDAL binaries
    GDAL_TRANSLATE_BIN = Path("/home/beckley/miniconda3/envs/workGDAL3ten/bin/gdal_translate")
    GDAL_BUILDVRT_BIN = Path("/home/beckley/miniconda3/envs/workGDAL3ten/bin/gdalbuildvrt")

    # Local STAC Catalog Settings
    LOCAL_STAC_CATALOG_PATH = Path("/data/matt/NED1m_STAC/catalog.json")
    LOCAL_STAC_INDEX_NAME = "stac_spatial_index"

    # OSN mirror settings
    OSN_ENDPOINT = "https://usgs.osn.mghpcc.org"
    OSN_BUCKET_NAME = "ot-usgs-osn"
    # OSN credentials file (must be created manually, never committed to git)
    # Format: ini file with [osn] section containing access_key and secret_key
    OSN_CREDENTIALS_FILE = Path("/data/matt/NED1m_STAC/osn_credentials.ini")

    # USGS health check: HEAD request on this many sample URLs before each run
    USGS_HEALTH_CHECK_TIMEOUT_SEC = 5
    USGS_HEALTH_CHECK_SAMPLE_COUNT = 2

    # Default Input AOI (EPSG:4326)
    DEFAULT_MIN_LON = -74.00
    DEFAULT_MIN_LAT = 44.705843
    DEFAULT_MAX_LON = -73.8
    DEFAULT_MAX_LAT = 44.782501

    # Default Output Directory and File Names
    DEFAULT_OUTPUT_BASE_DIR = Path("/data/matt/testing/STAC1m/test1")
    AOI_VSI_URL_LIST_FILE_NAME = 'AOI_tiff_VSI_URLs.txt'
    OUTPUT_S3_URL_LIST_FILE_NAME = 'Original_USGS1mTiles_URLs.txt'
    TEMP_VRT_FILE_NAME = 'tmp.vrt'
    OUTPUT_TARBALL_NAME = 'rasters_USGS1m.tar.gz'

    LOG_LEVEL = logging.INFO
    HTTP_403_WARNING_PATTERN = r"Warning 1: HTTP response code on .*?: 403"
    KEEP_TEMP_FILES = True
    FORCE_LOCAL_STAC = False  # skip health check, use USGS VSI paths
    FORCE_OSN = False         # skip health check, use OSN VSI paths


# Global logger instance, will be configured in main()
logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Logging Setup
# ----------------------------------------------------------------------
def setup_logging(log_file_path, log_level):
    """Configures the logging system."""
    log_file_path.parent.mkdir(parents=True, exist_ok=True)

    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()

    logger.setLevel(log_level)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    fh = logging.FileHandler(log_file_path, mode='a')
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    logger.addHandler(sh)

# ----------------------------------------------------------------------
# Helper Functions
# ----------------------------------------------------------------------

def run_subprocess_command(command_args, suppress_403_warnings=False, log_prefix="SUBPROCESS", env=None):
    command_str = ' '.join(str(arg) for arg in command_args)
    logger.info(f"{log_prefix}: Running command: '{command_str}'")
    try:
        process = subprocess.run(
            command_str, shell=True, capture_output=True, text=True, check=False, env=env
        )
        if process.stdout:
            for line in process.stdout.splitlines():
                logger.info(f"{log_prefix} STDOUT: {line}")
        if process.stderr:
            for line in process.stderr.splitlines():
                if suppress_403_warnings and re.search(Config.HTTP_403_WARNING_PATTERN, line):
                    logger.debug(f"{log_prefix} STDERR (Suppressed 403): {line}")
                else:
                    if "error" in line.lower() or process.returncode != 0:
                        logger.error(f"{log_prefix} STDERR: {line}")
                    else:
                        logger.warning(f"{log_prefix} STDERR: {line}")
        if process.returncode != 0:
            logger.error(f"{log_prefix} Command failed with return code {process.returncode}")
            raise subprocess.CalledProcessError(process.returncode, command_str, output=process.stdout, stderr=process.stderr)
        logger.info(f"{log_prefix} Command completed successfully.")
        return True
    except FileNotFoundError:
        logger.error(f"{log_prefix} Error: Command not found. Ensure executable is in PATH or specify full path: '{command_args[0]}'", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"{log_prefix} An unexpected error occurred while running subprocess: {type(e).__name__}: {e}", exc_info=True)
        raise

def bboxes_intersect(bbox1_minlon_minlat_maxlon_maxlat, bbox2_minlon_minlat_maxlon_maxlat):
    minx1, miny1, maxx1, maxy1 = bbox1_minlon_minlat_maxlon_maxlat
    minx2, miny2, maxx2, maxy2 = bbox2_minlon_minlat_maxlon_maxlat
    if maxx1 < minx2 or minx1 > maxx2 or maxy1 < miny2 or miny1 > maxy2:
        return False
    return True

def _get_osn_gdal_env():
    """
    Reads OSN credentials and returns an env dict for GDAL subprocesses to
    access OSN via S3 (path-style addressing, Ceph-compatible).
    """
    cfg = configparser.ConfigParser()
    cfg.read(Config.OSN_CREDENTIALS_FILE)
    try:
        access_key = cfg['osn']['access_key']
        secret_key = cfg['osn']['secret_key']
    except KeyError as e:
        logger.error(f"OSN credentials file missing key {e}. Expected [osn] section with "
                     f"access_key and secret_key in {Config.OSN_CREDENTIALS_FILE}")
        raise
    return {
        **os.environ,
        'AWS_S3_ENDPOINT': 'usgs.osn.mghpcc.org',
        'AWS_ACCESS_KEY_ID': access_key,
        'AWS_SECRET_ACCESS_KEY': secret_key,
        'AWS_VIRTUAL_HOSTING': 'FALSE',
    }


def _check_usgs_accessible(sample_urls):
    """
    Does HEAD requests on up to USGS_HEALTH_CHECK_SAMPLE_COUNT USGS S3 URLs
    with a short timeout. Returns True only if all sampled URLs respond successfully.
    """
    urls = [u for u in sample_urls if u][:Config.USGS_HEALTH_CHECK_SAMPLE_COUNT]
    if not urls:
        logger.warning("USGS health check: no sample URLs available, assuming accessible.")
        return True
    try:
        for url in urls:
            r = requests.head(url, timeout=Config.USGS_HEALTH_CHECK_TIMEOUT_SEC)
            r.raise_for_status()
        logger.info(f"USGS health check: OK ({len(urls)} URL(s) tested).")
        return True
    except requests.exceptions.RequestException as e:
        logger.warning(f"USGS health check: FAILED ({e}).")
        return False


def find_files_local_indexed(catalog_path: Path, search_bbox_4326, index_name: str):
    """
    Searches a local STAC catalog using an R-tree index.
    Returns a list of dicts:
      [{'vsi': usgs_vsi, 's3_https': usgs_https, 'osn_vsi': osn_vsi, 'osn_https': osn_https}, ...]
    osn_vsi and osn_https are None for items that have no OSN asset.
    """
    logger.info(f"Running local STAC search on {catalog_path} using index {index_name}...")
    results_list = []
    try:
        index_dir = catalog_path.parent
        idx_path = str(index_dir / index_name)
        pkl_path = index_dir / f"{index_name}.pkl"

        if not Path(idx_path + ".idx").exists() or not pkl_path.exists():
            logger.error(f"R-tree index files not found: {idx_path}.idx or {pkl_path}")
            return None

        idx = index.Index(idx_path, read_only=True)
        with open(pkl_path, 'rb') as f:
            item_id_to_relative_path = pickle.load(f)

        logger.debug(f"Search BBox for R-tree (EPSG:4326): {search_bbox_4326}")
        hits_count = 0

        for hit in idx.intersection(search_bbox_4326, objects=True):
            hits_count += 1
            item_id = hit.object
            relative_path_str = item_id_to_relative_path.get(item_id)
            if not relative_path_str:
                logger.warning(f"Item ID {item_id} found in R-tree but not in pickle mapping. Skipping.")
                continue

            item_path_abs = (catalog_path.parent / relative_path_str).resolve()

            try:
                item = pystac.read_file(str(item_path_abs))
                item_bbox = item.bbox

                if item_bbox and len(item_bbox) == 4:
                    if bboxes_intersect(item_bbox, search_bbox_4326):
                        asset = item.assets.get("elevation-geotiff")
                        if asset and asset.href and asset.href.lower().endswith(".tif"):
                            asset_href_raw = asset.href # This is what's stored in the STAC item
                            logger.debug(f"Item ID {item.id}, Original asset.href from STAC: '{asset_href_raw}'")

                            gdal_vsi_url = None
                            s3_https_url = None

                            if asset_href_raw.startswith('https://prd-tnm.s3.amazonaws.com'):
                                s3_https_url = asset_href_raw
                                gdal_vsi_url = s3_https_url.replace('https://prd-tnm.s3.amazonaws.com','/vsis3/prd-tnm')
                            elif asset_href_raw.startswith('s3://prd-tnm'):
                                s3_https_url = asset_href_raw.replace('s3://prd-tnm', 'https://prd-tnm.s3.amazonaws.com')
                                gdal_vsi_url = asset_href_raw.replace('s3://','/vsis3/')
                            elif asset_href_raw.startswith('https://rockyweb.usgs.gov/vdelivery/Datasets/Staged/'):
                                logger.debug(f"Handling rockyweb.usgs.gov URL for item {item.id}")
                                rocky_prefix = 'https://rockyweb.usgs.gov/vdelivery/Datasets/Staged/'
                                path_suffix = asset_href_raw[len(rocky_prefix):]
                                gdal_vsi_url = f"/vsis3/prd-tnm/StagedProducts/{path_suffix}"
                                s3_https_url = f"https://prd-tnm.s3.amazonaws.com/StagedProducts/{path_suffix}"
                            elif asset_href_raw.startswith('/vsis3/prd-tnm'):
                                gdal_vsi_url = asset_href_raw
                                s3_https_url = gdal_vsi_url.replace('/vsis3/prd-tnm', 'https://prd-tnm.s3.amazonaws.com')
                            elif asset_href_raw.startswith('/vsicurl/https://prd-tnm.s3.amazonaws.com'):
                                s3_https_url = asset_href_raw[len('/vsicurl/'):]
                                gdal_vsi_url = s3_https_url.replace('https://prd-tnm.s3.amazonaws.com','/vsis3/prd-tnm')
                            else: # Assume it might be a relative path or an absolute local file path
                                resolved_asset_path = Path(asset_href_raw)
                                if not resolved_asset_path.is_absolute():
                                    resolved_asset_path = (item_path_abs.parent / asset_href_raw).resolve()

                                if resolved_asset_path.is_file():
                                    gdal_vsi_url = f'/vsicurl/file://{resolved_asset_path}'
                                    s3_https_url = None # It's a local file, not S3
                                    logger.debug(f"Local file asset for {item.id}: '{resolved_asset_path}', VSI: '{gdal_vsi_url}'")
                                else:
                                    logger.warning(f"Asset href for item {item.id} ('{asset_href_raw}') is not a recognized S3 URL or existing local file ('{resolved_asset_path}'). Cannot form GDAL VSI path.")

                            if gdal_vsi_url:
                                if not any(d['vsi'] == gdal_vsi_url for d in results_list):
                                    osn_asset = item.assets.get("elevation-geotiff-osn")
                                    osn_https = osn_asset.href if osn_asset else None
                                    osn_vsi = osn_https.replace(f"{Config.OSN_ENDPOINT}/", "/vsis3/") if osn_https else None
                                    results_list.append({'vsi': gdal_vsi_url, 's3_https': s3_https_url,
                                                         'osn_vsi': osn_vsi, 'osn_https': osn_https})
            except Exception as e:
                logger.error(f"Error processing STAC item {item_id} from {item_path_abs}: {e}", exc_info=True)

        logger.info(f"Local STAC search: {hits_count} R-tree hits, {len(results_list)} unique URLs found.")
        return results_list
    except FileNotFoundError as e:
        logger.error(f"STAC index file not found: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Error during local STAC search: {e}", exc_info=True)
    return None

# ----------------------------------------------------------------------
# Main Script Logic
# ----------------------------------------------------------------------
def main(cli_args):
    FORMAT_MAP = {
        'GTIFF': {'driver': 'GTiff', 'ext': '.tif'},
        'AAIGRID': {'driver': 'AAIGrid', 'ext': '.asc'},
        'HFA': {'driver': 'HFA', 'ext': '.img'}
    }

    Config.MIN_LON = cli_args.minlon
    Config.MIN_LAT = cli_args.minlat
    Config.MAX_LON = cli_args.maxlon
    Config.MAX_LAT = cli_args.maxlat
    Config.BBOX_4326 = [Config.MIN_LON, Config.MIN_LAT, Config.MAX_LON, Config.MAX_LAT]
    Config.OUTPUT_BASE_DIR = Path(cli_args.output_dir)
    Config.KEEP_TEMP_FILES = cli_args.keep_temp_files
    Config.FORCE_LOCAL_STAC = cli_args.force_local_stac
    Config.FORCE_OSN = cli_args.force_osn
    if cli_args.stac_catalog_path:
        Config.LOCAL_STAC_CATALOG_PATH = Path(cli_args.stac_catalog_path)
    selected_format_key = cli_args.output_format.upper()

    log_file_path = Config.OUTPUT_BASE_DIR / f"processing_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    setup_logging(log_file_path, Config.LOG_LEVEL)

    logger.info("--- Script Started ---")
    logger.info(f"AOI: minlon={Config.MIN_LON}, minlat={Config.MIN_LAT}, maxlon={Config.MAX_LON}, maxlat={Config.MAX_LAT}")
    logger.info(f"Output directory: {Config.OUTPUT_BASE_DIR}")
    logger.info(f"Output format: {selected_format_key}")
    logger.info(f"Keep temp files: {Config.KEEP_TEMP_FILES}")
    logger.info(f"--force_local_stac: {Config.FORCE_LOCAL_STAC}  --force_osn: {Config.FORCE_OSN}")

    if not Config.OUTPUT_BASE_DIR.exists() or not Config.OUTPUT_BASE_DIR.is_dir():
        logger.error(f"Output directory does not exist or is not a directory: {Config.OUTPUT_BASE_DIR}. Exiting.")
        sys.exit(1)

    projwin_ulx, projwin_uly = Config.MIN_LON, Config.MAX_LAT
    projwin_lrx, projwin_lry = Config.MAX_LON, Config.MIN_LAT

    # --- Step 1: Query local STAC (always) ---
    logger.info("\n--- Querying Local STAC Catalog ---")
    query_start_time = datetime.now()
    url_data_list = find_files_local_indexed(
        Config.LOCAL_STAC_CATALOG_PATH, Config.BBOX_4326, Config.LOCAL_STAC_INDEX_NAME
    )
    query_time = datetime.now() - query_start_time

    if not url_data_list:
        logger.critical("Local STAC search returned no URLs. Exiting.")
        sys.exit(1)
    logger.info(f"Local STAC found {len(url_data_list)} URL(s). Query time: {query_time}")

    # --- Step 2: Determine data source ---
    logger.info("\n--- Determining Data Source ---")
    use_osn = False
    health_check_time = None
    source_label = ""

    if Config.FORCE_LOCAL_STAC:
        source_label = "USGS S3 (--force_local_stac, health check skipped)"
    elif Config.FORCE_OSN:
        use_osn = True
        source_label = "OSN mirror (--force_osn, health check skipped)"
    else:
        sample_urls = [e['s3_https'] for e in url_data_list if e.get('s3_https')][:Config.USGS_HEALTH_CHECK_SAMPLE_COUNT]
        health_check_start = datetime.now()
        usgs_ok = _check_usgs_accessible(sample_urls)
        health_check_time = datetime.now() - health_check_start
        if usgs_ok:
            source_label = "USGS S3 (health check passed)"
        else:
            use_osn = True
            source_label = "OSN mirror (USGS health check failed, using OSN failover)"

    logger.info(f"Data source: {source_label}")

    gdal_env = None
    if use_osn:
        try:
            gdal_env = _get_osn_gdal_env()
        except Exception as e:
            logger.critical(f"Failed to load OSN credentials: {e}. Exiting.")
            sys.exit(1)

    # --- Step 3: Build VSI and USGS URL lists ---
    vsi_urls_for_vrt = []
    s3_https_urls_for_log = []
    osn_fallback_count = 0

    for entry in url_data_list:
        if use_osn:
            if entry.get('osn_vsi'):
                vsi_urls_for_vrt.append(entry['osn_vsi'])
            elif entry.get('vsi'):
                vsi_urls_for_vrt.append(entry['vsi'])
                osn_fallback_count += 1
                logger.warning(f"No OSN asset for tile, falling back to USGS: {entry.get('s3_https', 'unknown')}")
        else:
            if entry.get('vsi'):
                vsi_urls_for_vrt.append(entry['vsi'])

        s3_url = entry.get('s3_https')
        if s3_url:
            s3_https_urls_for_log.append(s3_url)
        elif entry.get('vsi'):
            s3_https_urls_for_log.append(Path(entry['vsi']).name)
            logger.warning(f"No USGS S3 URL for tile, writing bare filename to URL list.")

    if use_osn and osn_fallback_count > 0:
        logger.warning(f"{osn_fallback_count} tile(s) had no OSN asset and fell back to USGS VSI paths.")

    if not vsi_urls_for_vrt:
        logger.critical("No valid VSI URLs to process for VRT. Exiting.")
        sys.exit(1)

    # --- Step 4: GDAL Processing ---
    logger.info("\n--- Processing GDAL Operations ---")

    format_info = FORMAT_MAP[selected_format_key]
    gdal_driver_name = format_info['driver']
    output_basename = "output_USGS1m"
    primary_output_raster_file = Config.OUTPUT_BASE_DIR / (output_basename + format_info['ext'])
    out_vsi_url_list_file = Config.OUTPUT_BASE_DIR / Config.AOI_VSI_URL_LIST_FILE_NAME
    out_s3_https_url_list_file = Config.OUTPUT_BASE_DIR / Config.OUTPUT_S3_URL_LIST_FILE_NAME
    vrt_file = Config.OUTPUT_BASE_DIR / Config.TEMP_VRT_FILE_NAME
    vrt_build_time = None
    translate_time = None

    try:
        with open(out_vsi_url_list_file, 'w') as f_out:
            for url in vsi_urls_for_vrt:
                f_out.write(url + '\n')
        logger.info(f"Wrote {len(vsi_urls_for_vrt)} VSI URLs to {out_vsi_url_list_file}")

        source_comment = "# Source: OSN mirror (USGS URLs shown)\n" if use_osn else "# Source: USGS S3 (direct)\n"
        with open(out_s3_https_url_list_file, 'w') as f_out_s3:
            f_out_s3.write(source_comment)
            for url in s3_https_urls_for_log:
                f_out_s3.write(url + '\n')
        logger.info(f"Wrote {len(s3_https_urls_for_log)} USGS S3 URLs to {out_s3_https_url_list_file}")

        vrt_start_time = datetime.now()
        run_subprocess_command(
            [str(Config.GDAL_BUILDVRT_BIN), str(vrt_file), "-input_file_list", str(out_vsi_url_list_file)],
            suppress_403_warnings=True, log_prefix="GDALBUILDVRT", env=gdal_env
        )
        vrt_build_time = datetime.now() - vrt_start_time

        translate_start_time = datetime.now()
        projwin_args = [str(v) for v in [projwin_ulx, projwin_uly, projwin_lrx, projwin_lry]]
        cmd_translate = [
            str(Config.GDAL_TRANSLATE_BIN), "-of", gdal_driver_name,
            "-projwin"] + projwin_args + ["-projwin_srs", "EPSG:4326", str(vrt_file)
        ]
        if selected_format_key == 'GTIFF':
            cmd_translate.extend(["-co", "COMPRESS=deflate", "-co", "TILED=YES",
                                   "-co", "blockxsize=512", "-co", "blockysize=512"])
        cmd_translate.append(str(primary_output_raster_file))
        run_subprocess_command(cmd_translate, suppress_403_warnings=False, log_prefix="GDAL_TRANSLATE", env=gdal_env)
        translate_time = datetime.now() - translate_start_time
        logger.info(f"Successfully created: {primary_output_raster_file}")

        logger.info("\n--- Archiving results and cleaning up ---")
        gdal_output_files = list(Config.OUTPUT_BASE_DIR.glob(f"{output_basename}.*"))
        if not gdal_output_files:
            logger.error("No 'output.*' files found after gdal_translate. Halting before archiving.")
            sys.exit(1)
        files_to_archive = gdal_output_files.copy()
        if out_s3_https_url_list_file.exists():
            files_to_archive.append(out_s3_https_url_list_file)

        logger.info(f"Archiving {len(files_to_archive)} file(s): {[f.name for f in files_to_archive]}")
        tarball_path = Config.OUTPUT_BASE_DIR / Config.OUTPUT_TARBALL_NAME
        try:
            with tarfile.open(tarball_path, "w:gz") as tar:
                for fp in files_to_archive:
                    tar.add(fp, arcname=fp.name)
            logger.info(f"Created tarball: {tarball_path}")
            for fp in files_to_archive:
                try:
                    if fp.exists():
                        fp.unlink()
                except OSError as e:
                    logger.warning(f"Failed to remove '{fp.name}': {e}")
        except (tarfile.TarError, Exception) as e:
            logger.error(f"Archiving failed, original files kept: {e}", exc_info=True)

    except subprocess.CalledProcessError:
        logger.critical("A GDAL command failed. Check logs. Exiting.")
        sys.exit(1)
    except IOError as e:
        logger.critical(f"File I/O error: {e}. Exiting.", exc_info=True)
    except Exception as e:
        logger.critical(f"Unexpected error: {type(e).__name__}: {e}. Exiting.", exc_info=True)
        sys.exit(1)
    finally:
        if not Config.KEEP_TEMP_FILES:
            try:
                for tmp in [out_vsi_url_list_file, vrt_file]:
                    if tmp.exists():
                        tmp.unlink()
                        logger.info(f"Removed temp file: {tmp}")
            except OSError as e:
                logger.warning(f"Failed to remove temp files: {e}", exc_info=True)
        else:
            logger.info(f"Temp files kept: {out_vsi_url_list_file}, {vrt_file}")

    logger.info("\n--- Final Execution Times ---")
    logger.info(f"STAC query:          {query_time}")
    if health_check_time:
        logger.info(f"USGS health check:   {health_check_time}")
    if vrt_build_time:
        logger.info(f"VRT build:           {vrt_build_time}")
    if translate_time:
        logger.info(f"GDAL translate:      {translate_time}")
    logger.info(f"Data source:         {source_label}")
    logger.info("--- Script Finished Successfully ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Retrieve USGS 1m DEM data for a given AOI using local STAC with USGS/OSN source selection.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("--minlon", type=float, default=Config.DEFAULT_MIN_LON, help="Min longitude (EPSG:4326).")
    parser.add_argument("--minlat", type=float, default=Config.DEFAULT_MIN_LAT, help="Min latitude (EPSG:4326).")
    parser.add_argument("--maxlon", type=float, default=Config.DEFAULT_MAX_LON, help="Max longitude (EPSG:4326).")
    parser.add_argument("--maxlat", type=float, default=Config.DEFAULT_MAX_LAT, help="Max latitude (EPSG:4326).")
    parser.add_argument("--output_dir", type=str, default=str(Config.DEFAULT_OUTPUT_BASE_DIR),
                        help="Output directory (must exist).")
    parser.add_argument("--output_format", type=str.upper, default='GTIFF', choices=['GTIFF', 'AAIGRID', 'HFA'])
    parser.add_argument("--keep_temp_files", action='store_true',
                        help="Keep temporary VSI URL list and VRT files.")
    parser.add_argument("--stac_catalog_path", type=str, default=None,
                        help="Override the local STAC catalog path.")

    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument("--force_local_stac", action='store_true',
                              help="Skip health check and use USGS VSI paths from local STAC.")
    source_group.add_argument("--force_osn", action='store_true',
                              help="Skip health check and use OSN VSI paths from local STAC.")

    cli_args = parser.parse_args()
    main(cli_args)
