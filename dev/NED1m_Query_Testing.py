import logging
import os
import subprocess
import json
import re # For regex in warning suppression
from datetime import datetime, timedelta # Added timedelta
from pathlib import Path
import requests
from urllib.parse import urlencode, quote # For custom encoding
import pystac # For pystac.read_file
from rtree import index # For R-tree spatial index
import pickle # For loading the .pkl file for the R-tree index
import sys # For sys.exit and stream handler logging
from osgeo import ogr, osr # Re-added for creating AOI shapefile


# -----------------------------------------------------------------------------
# STAC Processing Workflow Test Script
# -----------------------------------------------------------------------------
#
# Purpose:
# This script is designed to test two primary workflows for retrieving and
# processing USGS 1-meter Digital Elevation Model (DEM) data based on a
# given Area of Interest (AOI):
#   1. Using the official USGS National Map Product API.
#   2. Using a locally built STAC (SpatioTemporal Asset Catalog) as a fallback
#      complete with an R-tree spatial index for fast queries.
#
# The script allows for isolated testing of these workflows, comparison of their
# execution times, and ensures that the fallback mechanism (local STAC) is
# triggered correctly if the primary method (USGS API) is simulated to fail.
#
# How it Works:
# 1. Configuration: Uses a `TestConfig` class to manage all settings, including
#    paths to GDAL binaries, API endpoints, local STAC catalog details, AOI
#    bounding box, and test control parameters.
# 2. Unique Test Runs: Each execution of the script creates a unique timestamped
#    subdirectory within `TestConfig.TEST_OUTPUT_PARENT_DIR`. All outputs for
#    that run (logs, temporary files if kept, generated GeoTIFFs, AOI shapefile)
#    are stored in this unique directory.
# 3. Logging: Comprehensive logging is implemented, writing to both the console
#    and a log file within the unique run directory.
# 4. AOI Shapefile: Generates an ESRI Shapefile representing the input AOI
#    bounding box for visual verification or use in GIS software.
# 5. Test Workflows:
#    a. `run_test_usgs_api_workflow()`:
#       - Calls `_get_usgs_api_urls_for_test()` to fetch GeoTIFF URLs from the
#         USGS Product API based on the configured AOI.
#    b. `run_test_local_stac_fallback_workflow()`:
#       - Simulates a failure of the USGS API call.
#       - Then calls `_find_files_local_indexed_for_test()` to fetch GeoTIFF
#         URLs from the local STAC catalog and its R-tree index.
# 6. GDAL Processing (`_process_urls_to_geotiff`):
#    - Takes a list of GeoTIFF URLs (prefixed with `/vsis3/` or `/vsicurl/`).
#    - Writes these URLs to a temporary text file.
#    - Calls `gdalbuildvrt` (via subprocess) to create a VRT (Virtual Raster)
#      from the list of remote/virtual GeoTIFFs.
#    - Calls `gdal_translate` (via subprocess) to subset the VRT based on the
#      AOI's lat/lon bounding box (`-projwin` and `-projwin_srs EPSG:4326`)
#      and creates a final GeoTIFF output.
#    - Optionally keeps or deletes temporary files based on `TestConfig.KEEP_TEMP_FILES`.
# 7. Subprocess Handling: Uses a helper `run_subprocess_command` to execute
#    GDAL commands, capture their output, log stdout/stderr, and optionally
#    suppress specific HTTP 403 warnings from `gdalbuildvrt`.
# 8. Test Control: `TestConfig.RUN_TESTS` allows specifying whether to run
#    "both" tests, "usgs_api_only", or "local_stac_only".
# 9. Timing: Records and logs execution times for major stages of each workflow
#    (URL query, VRT creation, gdal_translate).
#
# Key Features & Things to Be Aware Of:
# - Test Isolation: Each test run has its own output directory.
# - Configuration Driven: Behavior is controlled via the `TestConfig` class.
#   Important settings include GDAL paths, local STAC path, AOI,
#   `KEEP_TEMP_FILES`, and `RUN_TESTS`.
# - GDAL Dependencies: Relies on external GDAL command-line tools being
#   correctly installed and accessible via the paths in `TestConfig`.
# - AWS/Network Access: The USGS API test requires internet access. The local
#   STAC test requires the catalog and index files to be accessible.
#   GDAL operations on `/vsis3/` or `/vsicurl/` paths also require network
#   access to the S3 objects.
# - Error Handling: Includes try-except blocks for robustness and logs errors.
# - Performance Comparison: Useful for comparing the speed of direct API calls
#   versus querying a pre-built local STAC index.
#
# Prerequisites/Libraries:
# - Python 3.x
# - requests
# - pystac
# - rtree
# - pickle (standard library)
# - osgeo (GDAL Python bindings, specifically for ogr/osr to create the AOI shapefile)
# - GDAL command-line utilities installed and paths configured.
#
# Last Substantial Modification: [Gemini - 2025-06-04]
#
# MAB Notes:
# - This code is useful for testing because it can test each method
# individually, or both.  It will also be useful for comparing time of
# execution, and/or if both methods are producing the same output.
#
# -----------------------------------------------------------------------------


# ----------------------------------------------------------------------
# Configuration & Constants for Testing
# ----------------------------------------------------------------------
class TestConfig:
    # Paths to GDAL binaries (ensure these are correct for your system)
    GDAL_TRANSLATE_BIN = Path("/home/beckley/miniconda3/envs/workGDAL3ten/bin/gdal_translate")
    GDAL_BUILDVRT_BIN = Path("/home/beckley/miniconda3/envs/workGDAL3ten/bin/gdalbuildvrt")

    # USGS Product API Settings
    USGS_API_URL_BASE = 'https://tnmaccess.nationalmap.gov/api/v1/products'
    USGS_DATASET_NAME = 'Digital Elevation Model (DEM) 1 meter'
    USGS_API_TIMEOUT_SEC = 60
    USGS_API_MAX_RESULTS = 1000

    # Local STAC Catalog Settings
    LOCAL_STAC_CATALOG_PATH = Path("/data/matt/NED1m_STAC_Jun05/catalog.json") 
    LOCAL_STAC_INDEX_NAME = "stac_spatial_index"

    # Input AOI (EPSG:4326) - Same as main script for comparable tests
    MIN_LON = -112.07972734
    MIN_LAT = 36.08450160
    MAX_LON = -112.04118761
    MAX_LAT = 36.10919384

    BBOX_4326 = [MIN_LON, MIN_LAT, MAX_LON, MAX_LAT]

    # Test Output Directory and File Names
    TEST_OUTPUT_PARENT_DIR = Path("/data/matt/testing/STAC1m") 
    
    # Logging Settings
    # LOG_FILE will be set dynamically within the unique test run directory
    LOG_LEVEL = logging.INFO # Can be set to logging.DEBUG for more detail

    HTTP_403_WARNING_PATTERN = r"Warning 1: HTTP response code on .*?: 403"
    
    KEEP_TEMP_FILES = True # Set to True to keep temp URL list and VRT files

    # Test Execution Control
    # Options: "both", "usgs_api_only", "local_stac_only"
    RUN_TESTS = "both"

# ----------------------------------------------------------------------
# Logging Setup
# ----------------------------------------------------------------------
logger = logging.getLogger(__name__) # Get logger instance

def setup_dynamic_logging(log_file_path, log_level):
    """Configures the logging system to use a dynamic log file path."""
    # Remove existing handlers to avoid duplicate logging if called multiple times in same Python session
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close() # Important to close file handlers before removing
    
    log_file_path.parent.mkdir(parents=True, exist_ok=True)
    
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    fh = logging.FileHandler(log_file_path, mode='a') 
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    logger.addHandler(sh)
    
    logger.setLevel(log_level)


# ----------------------------------------------------------------------
# Helper Functions (Adapted from the main script)
# ----------------------------------------------------------------------
def run_subprocess_command(command_args, suppress_403_warnings=False, log_prefix="SUBPROCESS"):
    command_str = ' '.join(str(arg) for arg in command_args)
    logger.info(f"{log_prefix}: Running command: '{command_str}'")
    try:
        process = subprocess.run(
            command_str, shell=True, capture_output=True, text=True, check=False
        )
        if process.stdout:
            for line in process.stdout.splitlines():
                logger.info(f"{log_prefix} STDOUT: {line}")
        if process.stderr:
            for line in process.stderr.splitlines():
                if suppress_403_warnings and re.search(TestConfig.HTTP_403_WARNING_PATTERN, line):
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

def create_aoi_shapefile(bbox_4326, output_dir_path, filename="aoi_bbox.shp"):
    """Creates an ESRI Shapefile from a bounding box."""
    minlon, minlat, maxlon, maxlat = bbox_4326
    output_shapefile = output_dir_path / filename
    logger.info(f"Creating AOI shapefile: {output_shapefile}")

    try:
        ring = ogr.Geometry(ogr.wkbLinearRing)
        ring.AddPoint(minlon, minlat)
        ring.AddPoint(minlon, maxlat)
        ring.AddPoint(maxlon, maxlat)
        ring.AddPoint(maxlon, minlat)
        ring.AddPoint(minlon, minlat) 
        polygon = ogr.Geometry(ogr.wkbPolygon)
        polygon.AddGeometry(ring)
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        if int(osr.GetPROJVersionMajor()) >= 6:
             srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        driver = ogr.GetDriverByName("ESRI Shapefile")
        if driver is None:
            logger.error("ESRI Shapefile driver not available.")
            return False
        if output_shapefile.exists():
            try:
                driver.DeleteDataSource(str(output_shapefile))
                logger.debug(f"Removed existing shapefile: {output_shapefile}")
            except Exception as e_delete: 
                logger.warning(f"Could not delete existing shapefile {output_shapefile}: {e_delete}")
        dataSource = driver.CreateDataSource(str(output_shapefile))
        if dataSource is None:
            logger.error(f"Could not create shapefile: {output_shapefile}")
            return False
        layer = dataSource.CreateLayer("aoi_boundary", srs=srs, geom_type=ogr.wkbPolygon)
        if layer is None:
            logger.error("Could not create layer in shapefile.")
            return False
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetGeometry(polygon)
        layer.CreateFeature(feature)
        feature = None
        dataSource = None 
        logger.info(f"Successfully created AOI shapefile: {output_shapefile}")
        return True
    except Exception as e:
        logger.error(f"Error creating AOI shapefile: {e}", exc_info=True)
        return False


def _get_usgs_api_urls_for_test(minlon, minlat, maxlon, maxlat, simulate_failure=False):
    """Internal function for USGS API call, can simulate failure."""
    if simulate_failure:
        logger.info("USGS_API_TEST: Simulating API failure.")
        return None 
    logger.info("USGS_API_TEST: Attempting URL retrieval via USGS Product API...")
    try:
        safe_params_to_encode = {
            'datasets': TestConfig.USGS_DATASET_NAME,
            'prodFormats': 'GeoTIFF',
            'outputFormat': 'JSON',
            'max': TestConfig.USGS_API_MAX_RESULTS
        }
        encoded_params = urlencode(safe_params_to_encode, quote_via=quote)
        bbox_val_str = f"{minlon},{minlat},{maxlon},{maxlat}"
        final_url = f"{TestConfig.USGS_API_URL_BASE}?{encoded_params}&bbox={bbox_val_str}"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        logger.info(f"USGS_API_TEST: Request URL: {final_url}")
        r = requests.get(final_url, headers=headers, timeout=TestConfig.USGS_API_TIMEOUT_SEC)
        logger.debug(f"USGS_API_TEST: Response Status Code: {r.status_code}")
        r.raise_for_status()
        data = r.json()
        items = data.get('items', [])
        if not items:
            logger.warning("USGS_API_TEST: API returned no items.")
            return None
        urls = [item.get("urls", {}).get("TIFF").replace('https://prd-tnm.s3.amazonaws.com','/vsis3/prd-tnm')
                for item in items if item.get("urls", {}).get("TIFF")]
        logger.info(f"USGS_API_TEST: Found {len(urls)} URLs.")
        return urls
    except Exception as e:
        logger.error(f"USGS_API_TEST: Error during API call: {type(e).__name__}: {e}", exc_info=True)
        return None

def _find_files_local_indexed_for_test(catalog_path, search_bbox_4326, index_name):
    """Internal function for local STAC search."""
    logger.info(f"LOCAL_STAC_TEST: Running local STAC search on {catalog_path} using index {index_name}...")
    try:
        index_dir = catalog_path.parent
        idx_path = str(index_dir / index_name)
        pkl_path = index_dir / f"{index_name}.pkl"
        if not Path(idx_path + ".idx").exists() or not pkl_path.exists():
            logger.error(f"LOCAL_STAC_TEST: R-tree index files not found: {idx_path}.idx or {pkl_path}")
            return None
        idx = index.Index(idx_path, read_only=True)
        with open(pkl_path, 'rb') as f:
            item_id_to_relative_path = pickle.load(f)
        intersecting_urls = []
        for hit in idx.intersection(search_bbox_4326, objects=True):
            item_id = hit.object
            relative_path_str = item_id_to_relative_path.get(item_id)
            if not relative_path_str: continue
            item_path_abs = (catalog_path.parent / relative_path_str).resolve()
            try:
                item = pystac.read_file(str(item_path_abs))
                if item.bbox and bboxes_intersect(item.bbox, search_bbox_4326):
                    asset = item.assets.get("elevation-geotiff")
                    if asset and asset.href and asset.href.lower().endswith(".tif"):
                        asset_href_full = asset.href
                        if not (asset_href_full.startswith("http") or asset_href_full.startswith("s3://")):
                            asset_href_full = str((item_path_abs.parent / asset_href_full).resolve())
                        download_url = None
                        if 'prd-tnm.s3.amazonaws.com' in asset_href_full:
                            download_url = asset_href_full.replace('https://prd-tnm.s3.amazonaws.com','/vsis3/prd-tnm')
                        elif asset_href_full.startswith('s3://'):
                            download_url = asset_href_full.replace('s3://','/vsis3/')
                        elif Path(asset_href_full).is_absolute() and Path(asset_href_full).exists():
                             download_url = f'/vsicurl/file://{asset_href_full}'
                        if download_url and download_url not in intersecting_urls:
                            intersecting_urls.append(download_url)
            except Exception as e_item:
                logger.error(f"LOCAL_STAC_TEST: Error processing item {item_id}: {e_item}", exc_info=True)
        logger.info(f"LOCAL_STAC_TEST: Found {len(intersecting_urls)} unique URLs.")
        return intersecting_urls
    except Exception as e:
        logger.error(f"LOCAL_STAC_TEST: Error during local STAC search: {e}", exc_info=True)
        return None

def _process_urls_to_geotiff(retrieved_urls, output_tiff_path, temp_files_dir, test_name_prefix):
    """Common GDAL processing steps: write URL list, build VRT, translate."""
    if not retrieved_urls:
        logger.error(f"{test_name_prefix}: No URLs to process.")
        return None, None

    temp_url_list_file = temp_files_dir / f"{test_name_prefix}_urls.txt"
    temp_vrt_file = temp_files_dir / f"{test_name_prefix}_tmp.vrt"
    
    vrt_build_time = None
    translate_time = None

    try:
        with open(temp_url_list_file, 'w') as f_out:
            for url in retrieved_urls:
                f_out.write(url + '\n')
        logger.info(f"{test_name_prefix}: Wrote {len(retrieved_urls)} URLs to {temp_url_list_file}")

        vrt_start_time = datetime.now()
        cmd_gdalbuildvrt = [
            str(TestConfig.GDAL_BUILDVRT_BIN), str(temp_vrt_file),
            "-input_file_list", str(temp_url_list_file)
        ]
        run_subprocess_command(cmd_gdalbuildvrt, suppress_403_warnings=True, log_prefix=f"{test_name_prefix}_GDALBUILDVRT")
        vrt_build_time = datetime.now() - vrt_start_time

        translate_start_time = datetime.now()
        projwin_ulx = TestConfig.MIN_LON
        projwin_uly = TestConfig.MAX_LAT
        projwin_lrx = TestConfig.MAX_LON
        projwin_lry = TestConfig.MIN_LAT
        cmd_gdal_translate_list = [
            str(TestConfig.GDAL_TRANSLATE_BIN), str(temp_vrt_file),
            "-projwin", str(projwin_ulx), str(projwin_uly), str(projwin_lrx), str(projwin_lry),
            "-projwin_srs", "EPSG:4326",
            "-of", "GTIFF",
            "-co", "COMPRESS=deflate", "-co", "TILED=YES",
            "-co", "blockxsize=512", "-co", "blockysize=512",
            "-co", "NUM_THREADS=ALL_CPUS",
            str(output_tiff_path)
        ]
        run_subprocess_command(cmd_gdal_translate_list, suppress_403_warnings=False, log_prefix=f"{test_name_prefix}_GDAL_TRANSLATE")
        translate_time = datetime.now() - translate_start_time
        logger.info(f"{test_name_prefix}: Successfully created {output_tiff_path}")

    except Exception as e:
        logger.error(f"{test_name_prefix}: Error during GDAL processing: {e}", exc_info=True)
        return None, None 
    finally:
        if not TestConfig.KEEP_TEMP_FILES: 
            logger.info(f"{test_name_prefix}: Cleaning up temporary files as KEEP_TEMP_FILES is False...")
            try:
                if temp_url_list_file.exists():
                    os.remove(temp_url_list_file)
                    logger.debug(f"{test_name_prefix}: Removed temporary URL list file: {temp_url_list_file}")
                if temp_vrt_file.exists():
                    os.remove(temp_vrt_file)
                    logger.debug(f"{test_name_prefix}: Removed temporary VRT file: {temp_vrt_file}")
            except OSError as e_cleanup:
                logger.warning(f"{test_name_prefix}: Failed to remove one or more temporary files: {e_cleanup}", exc_info=True)
        else:
            logger.info(f"{test_name_prefix}: Keeping temporary files as per configuration: {temp_url_list_file}, {temp_vrt_file}")
            
    return vrt_build_time, translate_time

# ----------------------------------------------------------------------
# Test Functions
# ----------------------------------------------------------------------
def run_test_usgs_api_workflow(unique_output_dir):
    """Test the workflow using USGS Product API."""
    test_name = "USGS_API_Test"
    logger.info(f"\n--- Starting Test: {test_name} ---")
    logger.info(f"Output for this test will be in: {unique_output_dir}")
    
    output_tiff = unique_output_dir / f"output_{test_name}.tif"

    urls_start_time = datetime.now()
    retrieved_urls = _get_usgs_api_urls_for_test(
        TestConfig.MIN_LON, TestConfig.MIN_LAT, TestConfig.MAX_LON, TestConfig.MAX_LAT
    )
    urls_query_time = datetime.now() - urls_start_time
    logger.info(f"{test_name}: URL Query Time: {urls_query_time}")

    if retrieved_urls:
        vrt_time, translate_time = _process_urls_to_geotiff(retrieved_urls, output_tiff, unique_output_dir, test_name)
        if vrt_time is not None and translate_time is not None:
            logger.info(f"{test_name}: VRT Build Time: {vrt_time}")
            logger.info(f"{test_name}: GDAL Translate Time: {translate_time}")
            total_processing_time = urls_query_time + vrt_time + translate_time
            logger.info(f"{test_name}: Total Workflow Time: {total_processing_time}")
            logger.info(f"--- {test_name} COMPLETED SUCCESSFULLY ---")
            return True
    
    logger.error(f"--- {test_name} FAILED ---")
    return False

def run_test_local_stac_fallback_workflow(unique_output_dir):
    """Test the workflow by simulating USGS API failure and using Local STAC."""
    test_name = "Local_STAC_Fallback_Test"
    logger.info(f"\n--- Starting Test: {test_name} ---")
    logger.info(f"Output for this test will be in: {unique_output_dir}")

    output_tiff = unique_output_dir / f"output_{test_name}.tif"

    logger.info(f"{test_name}: Simulating USGS API failure...")
    usgs_urls_start_time = datetime.now()
    _get_usgs_api_urls_for_test(
        TestConfig.MIN_LON, TestConfig.MIN_LAT, TestConfig.MAX_LON, TestConfig.MAX_LAT, simulate_failure=True
    )
    usgs_query_time = datetime.now() - usgs_urls_start_time
    logger.info(f"{test_name}: Simulated USGS API Query Time: {usgs_query_time}")

    logger.info(f"{test_name}: Falling back to Local STAC catalog...")
    local_stac_urls_start_time = datetime.now()
    retrieved_urls = _find_files_local_indexed_for_test(
        TestConfig.LOCAL_STAC_CATALOG_PATH,
        TestConfig.BBOX_4326,
        TestConfig.LOCAL_STAC_INDEX_NAME
    )
    local_stac_query_time = datetime.now() - local_stac_urls_start_time
    logger.info(f"{test_name}: Local STAC Query Time: {local_stac_query_time}")

    if retrieved_urls:
        vrt_time, translate_time = _process_urls_to_geotiff(retrieved_urls, output_tiff, unique_output_dir, test_name)
        if vrt_time is not None and translate_time is not None:
            logger.info(f"{test_name}: VRT Build Time: {vrt_time}")
            logger.info(f"{test_name}: GDAL Translate Time: {translate_time}")
            total_processing_time = usgs_query_time + local_stac_query_time + vrt_time + translate_time
            logger.info(f"{test_name}: Total Workflow Time (including simulated USGS fail): {total_processing_time}")
            logger.info(f"--- {test_name} COMPLETED SUCCESSFULLY ---")
            return True

    logger.error(f"--- {test_name} FAILED ---")
    return False

# ----------------------------------------------------------------------
# Main Execution
# ----------------------------------------------------------------------
if __name__ == "__main__":
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    unique_run_output_dir = TestConfig.TEST_OUTPUT_PARENT_DIR / f"test_run_{timestamp_str}"
    try:
        unique_run_output_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"CRITICAL: Could not create unique output directory {unique_run_output_dir}: {e}", file=sys.stderr)
        sys.exit(1)

    log_file_path = unique_run_output_dir / "test_processing_log.log"
    setup_dynamic_logging(log_file_path, TestConfig.LOG_LEVEL) 

    logger.info(f"===== Starting STAC Processing Test Script =====")
    logger.info(f"Test run mode: {TestConfig.RUN_TESTS}")
    logger.info(f"All outputs for this run will be in: {unique_run_output_dir}")

    try:
        osr.UseExceptions() 
        create_aoi_shapefile(TestConfig.BBOX_4326, unique_run_output_dir, "aoi_bbox.shp")
    except Exception as e: 
        logger.error(f"Failed to create AOI shapefile: {e}", exc_info=True)
    
    if TestConfig.RUN_TESTS.lower() == "both":
        run_test_usgs_api_workflow(unique_run_output_dir)
        run_test_local_stac_fallback_workflow(unique_run_output_dir)
    elif TestConfig.RUN_TESTS.lower() == "usgs_api_only":
        run_test_usgs_api_workflow(unique_run_output_dir)
    elif TestConfig.RUN_TESTS.lower() == "local_stac_only":
        logger.info("Running 'local_stac_only' by executing the fallback test workflow.")
        run_test_local_stac_fallback_workflow(unique_run_output_dir)
    else:
        logger.error(f"Invalid value for TestConfig.RUN_TESTS: '{TestConfig.RUN_TESTS}'. "
                     "Valid options are 'both', 'usgs_api_only', 'local_stac_only'.")

    logger.info("===== STAC Processing Test Script Finished =====")
