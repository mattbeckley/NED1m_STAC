import logging
import os
import subprocess
import json
import re # For regex in warning suppression
from datetime import datetime
from pathlib import Path
import requests
from urllib.parse import urlencode, quote # Import quote for custom encoding
import pystac # For pystac.read_file
from rtree import index # For R-tree spatial index
import pickle # For loading the .pkl file for the R-tree index
import sys # For sys.exit and stream handler logging
import argparse # For command-line arguments

# GDAL/OGR operations are done via subprocess calls to GDAL binaries.

# -----------------------------------------------------------------------------
# Refined STAC Processing Script (USGS API & Local Fallback)
# -----------------------------------------------------------------------------
#
# Purpose:
# This script retrieves USGS 1-meter Digital Elevation Model (DEM) data for a
# specified Area of Interest (AOI). It first attempts to use the USGS National
# Map Product API. If that fails (e.g., server issues), it falls back to using
# a pre-built local STAC (SpatioTemporal Asset Catalog) with an R-tree spatial
# index for faster local queries. An option exists to force the use of the
# local STAC catalog, bypassing the USGS API attempt.
#
# The script then generates a VRT (Virtual Raster) from the identified GeoTIFF
# URLs (using GDAL VSI paths) and uses gdal_translate to create a final GeoTIFF
# subsetted to the AOI. It also outputs a separate text file containing the
# original S3 HTTPS URLs for documentation.
#
# Command-Line Arguments:
#   (Run with -h or --help to see all options and their defaults)
#   --minlon             Minimum longitude of the AOI (EPSG:4326).
#   --minlat             Minimum latitude of the AOI (EPSG:4326).
#   --maxlon             Maximum longitude of the AOI (EPSG:4326).
#   --maxlat             Maximum latitude of the AOI (EPSG:4326).
#   --output_dir         Base directory for all outputs (logs, temp files, final GeoTIFF).
#                        This directory MUST exist.
#   --keep_temp_files    If specified, temporary files (VSI URL list, VRT) will not be deleted.
#   --force_local_stac   If specified, bypass the USGS Product API and use only the
#                        local STAC catalog.
#
# How it Works:
# 1. Configuration & Argument Parsing: Script settings are defined in a `Config`
#    class, which can be overridden by command-line arguments.
# 2. Logging: Comprehensive logging to console and a daily log file.
# 3. URL Retrieval:
#    - If --force_local_stac is used, it directly queries the local STAC catalog.
#    - Otherwise, it tries USGS Product API first.
#    - If USGS API fails, it falls back to/uses the local STAC.
#    - Returns a list of dictionaries, each containing a 'vsi' path for GDAL
#      and the original 's3_https' URL if applicable.
# 4. GDAL Processing:
#    - Writes GDAL VSI paths to a temporary list file.
#    - Writes original S3 HTTPS URLs to a separate persistent list file.
#    - Calls `gdalbuildvrt` to create a VRT from the VSI path list.
#    - Calls `gdal_translate` to subset the VRT to the AOI and output a GeoTIFF.
# 5. Error Handling & Reporting: Includes try-except blocks, logs errors.
# 6. Cleanup: By default, deletes temporary VSI URL list and VRT files.
#
# Last Substantial Modification: [Gemini - 2025-06-05]
#
# -----------------------------------------------------------------------------


# ----------------------------------------------------------------------
# Configuration & Constants
# ----------------------------------------------------------------------
class Config:
    # Paths to GDAL binaries
    GDAL_TRANSLATE_BIN = Path("/home/beckley/miniconda3/envs/workGDAL3ten/bin/gdal_translate")
    GDAL_BUILDVRT_BIN = Path("/home/beckley/miniconda3/envs/workGDAL3ten/bin/gdalbuildvrt")

    # USGS Product API Settings
    USGS_API_URL_BASE = 'https://tnmaccess.nationalmap.gov/api/v1/products'
    USGS_DATASET_NAME = 'Digital Elevation Model (DEM) 1 meter'
    USGS_API_TIMEOUT_SEC = 60
    USGS_API_MAX_RESULTS = 1000

    # Local STAC Catalog Settings
    LOCAL_STAC_CATALOG_PATH = Path("/data/matt/NED1m_STAC/catalog.json") 
    LOCAL_STAC_INDEX_NAME = "stac_spatial_index"

    # Default Input AOI (EPSG:4326)
    DEFAULT_MIN_LON = -74.00
    DEFAULT_MIN_LAT = 44.705843
    DEFAULT_MAX_LON = -73.8
    DEFAULT_MAX_LAT = 44.782501

    # Default Output Directory and File Names
    DEFAULT_OUTPUT_BASE_DIR = Path("/data/matt/testing/STAC1m/test1")
    OUTPUT_FILE_NAME = "output.tif" 
    AOI_VSI_URL_LIST_FILE_NAME = 'AOI_tiff_VSI_URLs.txt' # For GDAL VSI paths
    OUTPUT_S3_URL_LIST_FILE_NAME = 'Original_USGS1mTiles_URLs.txt' # For original S3 HTTPS URLs
    TEMP_VRT_FILE_NAME = 'tmp.vrt'

    #Log settings can be set to: DEBUG, INFO, WARNING, ERROR, CRITICAL
    LOG_LEVEL = logging.INFO 

    HTTP_403_WARNING_PATTERN = r"Warning 1: HTTP response code on .*?: 403"
    
    KEEP_TEMP_FILES = True # Temporary VSI URL list and VRT

    #Set to True to bypass USGS Product API query and only use local STAC
    FORCE_LOCAL_STAC = False 


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

def get_geotiff_urls(method_type, minlon, minlat, maxlon, maxlat, catalog_path_config: Path, search_index_name_config: str):
    """
    Retrieves GeoTIFF URLs.
    Returns:
        tuple: (list of dicts [{'vsi': ..., 's3_https': ...}], query_duration) or (None, None)
    """
    query_start_time = datetime.now()
    results_list = [] # List of {'vsi': ..., 's3_https': ...}

    if method_type == "USGS_API":
        logger.info("Attempting URL retrieval via USGS Product API...")
        try:
            safe_params_to_encode = {
                'datasets': Config.USGS_DATASET_NAME,
                'prodFormats': 'GeoTIFF',
                'outputFormat': 'JSON',
                'max': Config.USGS_API_MAX_RESULTS
            }
            encoded_params = urlencode(safe_params_to_encode, quote_via=quote)
            bbox_val_str = f"{minlon},{minlat},{maxlon},{maxlat}"
            final_url = f"{Config.USGS_API_URL_BASE}?{encoded_params}&bbox={bbox_val_str}"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            logger.info(f"USGS API Request URL to be sent: {final_url}")
            r = requests.get(final_url, headers=headers, timeout=Config.USGS_API_TIMEOUT_SEC)
            logger.info(f"USGS API Effective URL after request: {r.url}")
            r.raise_for_status()
            data = r.json()
            query_end_time = datetime.now()
            items = data.get('items', [])
            if not items:
                logger.warning("USGS API returned no items for the query.")
                return None, query_end_time - query_start_time
            
            for item in items:
                s3_https_url = item.get("urls", {}).get("TIFF")
                if s3_https_url:
                    vsi_path = s3_https_url.replace('https://prd-tnm.s3.amazonaws.com','/vsis3/prd-tnm')
                    results_list.append({'vsi': vsi_path, 's3_https': s3_https_url})
            
            logger.info(f"USGS API found {len(results_list)} URLs.")
            return results_list, query_end_time - query_start_time
        except Exception as e: # Catching broad exceptions for brevity, specific ones are better
            logger.error(f"Error during USGS API call: {type(e).__name__}: {e}", exc_info=True)
        return None, datetime.now() - query_start_time

    elif method_type == "LOCAL_STAC":
        if not catalog_path_config or not search_index_name_config: 
            logger.error("Local STAC method requires catalog_path and search_index_name.")
            return None, None
        logger.info(f"Attempting URL retrieval via Local STAC catalog: {catalog_path_config}")
        results_list = find_files_local_indexed(catalog_path_config, [minlon, minlat, maxlon, maxlat], search_index_name_config)
        query_end_time = datetime.now()
        if results_list: # find_files_local_indexed now returns the list of dicts
            logger.info(f"Local STAC found {len(results_list)} URLs.")
        else:
            logger.warning("Local STAC search found no URLs or an error occurred.")
        return results_list, query_end_time - query_start_time
    else:
        logger.error(f"Unknown URL retrieval method: {method_type}")
        return None, None

def find_files_local_indexed(catalog_path: Path, search_bbox_4326, index_name: str):
    """
    Searches a local STAC catalog using an R-tree index.
    Returns a list of dictionaries: [{'vsi': gdal_vsi_url, 's3_https': s3_https_url_or_none}, ...]
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
                            
                            if gdal_vsi_url: # Only add if we could form a VSI path
                                # Avoid duplicates based on VSI path
                                if not any(d['vsi'] == gdal_vsi_url for d in results_list):
                                    results_list.append({'vsi': gdal_vsi_url, 's3_https': s3_https_url})
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
    """Main execution function, using parsed CLI arguments."""
    
    Config.MIN_LON = cli_args.minlon
    Config.MIN_LAT = cli_args.minlat
    Config.MAX_LON = cli_args.maxlon
    Config.MAX_LAT = cli_args.maxlat
    Config.BBOX_4326 = [Config.MIN_LON, Config.MIN_LAT, Config.MAX_LON, Config.MAX_LAT]
    Config.OUTPUT_BASE_DIR = Path(cli_args.output_dir)
    Config.KEEP_TEMP_FILES = cli_args.keep_temp_files
    Config.FORCE_LOCAL_STAC = cli_args.force_local_stac

    log_file_path = Config.OUTPUT_BASE_DIR / f"processing_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    setup_logging(log_file_path, Config.LOG_LEVEL)

    logger.info("--- Script Started ---")
    logger.info(f"Running with AOI: minlon={Config.MIN_LON}, minlat={Config.MIN_LAT}, maxlon={Config.MAX_LON}, maxlat={Config.MAX_LAT}")
    logger.info(f"Output directory: {Config.OUTPUT_BASE_DIR}")
    logger.info(f"Keep temporary files: {Config.KEEP_TEMP_FILES}")
    logger.info(f"Force local STAC: {Config.FORCE_LOCAL_STAC}")

    if not Config.OUTPUT_BASE_DIR.exists():
        logger.error(f"Output directory does not exist: {Config.OUTPUT_BASE_DIR}. Please create it or provide a valid path. Exiting.")
        sys.exit(1)
    if not Config.OUTPUT_BASE_DIR.is_dir():
        logger.error(f"Provided output path is not a directory: {Config.OUTPUT_BASE_DIR}. Exiting.")
        sys.exit(1)
    logger.info(f"Output directory confirmed: {Config.OUTPUT_BASE_DIR}")

    projwin_ulx = Config.MIN_LON
    projwin_uly = Config.MAX_LAT
    projwin_lrx = Config.MAX_LON
    projwin_lry = Config.MIN_LAT
    logger.info(f"Using Bounding Box (EPSG:4326) for projwin: ulx={projwin_ulx}, uly={projwin_uly}, lrx={projwin_lrx}, lry={projwin_lry}")

    logger.info("\n--- Attempting URL Retrieval ---")
    url_data_list = None # This will be a list of dicts: [{'vsi': ..., 's3_https': ...}]
    query_time = None
    retrieval_method_used = "None"

    if Config.FORCE_LOCAL_STAC:
        logger.info("FORCE_LOCAL_STAC is True. Using Local STAC catalog directly.")
        try:
            url_data_list, query_time = get_geotiff_urls(
                "LOCAL_STAC", Config.MIN_LON, Config.MIN_LAT, Config.MAX_LON, Config.MAX_LAT,
                Config.LOCAL_STAC_CATALOG_PATH, Config.LOCAL_STAC_INDEX_NAME
            )
            if url_data_list:
                logger.info(f"Successfully retrieved {len(url_data_list)} URL entries from Local STAC Catalog.")
                retrieval_method_used = "LOCAL_STAC (Forced)"
            else:
                 logger.critical("Local STAC (forced) failed or returned no URLs.")
        except Exception as e_stac:
            logger.critical(f"Local STAC catalog search (forced) failed ({type(e_stac).__name__}: {e_stac}). Exiting.", exc_info=True)
            sys.exit(1)
    else:
        try:
            url_data_list, query_time = get_geotiff_urls(
                "USGS_API", Config.MIN_LON, Config.MIN_LAT, Config.MAX_LON, Config.MAX_LAT,
                Config.LOCAL_STAC_CATALOG_PATH, Config.LOCAL_STAC_INDEX_NAME 
            )
            if url_data_list:
                logger.info(f"Successfully retrieved {len(url_data_list)} URL entries from USGS Product API.")
                retrieval_method_used = "USGS_API"
            else:
                raise Exception("USGS API failed or returned no URLs.")
        except Exception as e_usgs:
            logger.warning(f"USGS Product API failed ({type(e_usgs).__name__}: {e_usgs}). Falling back to Local STAC catalog.")
            try:
                url_data_list, query_time = get_geotiff_urls(
                    "LOCAL_STAC", Config.MIN_LON, Config.MIN_LAT, Config.MAX_LON, Config.MAX_LAT,
                    Config.LOCAL_STAC_CATALOG_PATH, Config.LOCAL_STAC_INDEX_NAME
                )
                if url_data_list:
                    logger.info(f"Successfully retrieved {len(url_data_list)} URL entries from Local STAC Catalog.")
                    retrieval_method_used = "LOCAL_STAC (Fallback)"
                else:
                     logger.critical("Local STAC (fallback) also failed or returned no URLs.")
            except Exception as e_stac:
                logger.critical(f"Local STAC catalog search (fallback) also failed ({type(e_stac).__name__}: {e_stac}). Exiting.", exc_info=True)
                sys.exit(1)

    if not url_data_list:
        logger.critical("Failed to retrieve any GeoTIFF URLs using chosen method(s). Exiting.")
        sys.exit(1)

    logger.info("\n--- Processing GDAL Operations ---")
    out_vsi_url_list_file = Config.OUTPUT_BASE_DIR / Config.AOI_VSI_URL_LIST_FILE_NAME
    out_s3_https_url_list_file = Config.OUTPUT_BASE_DIR / Config.OUTPUT_S3_URL_LIST_FILE_NAME
    vrt_file = Config.OUTPUT_BASE_DIR / Config.TEMP_VRT_FILE_NAME
    output_tiff_file = Config.OUTPUT_BASE_DIR / Config.OUTPUT_FILE_NAME

    vrt_build_time = None
    translate_time = None

    try:
        vsi_urls_for_vrt = [entry['vsi'] for entry in url_data_list if entry['vsi']]
        s3_https_urls_for_log = [entry['s3_https'] for entry in url_data_list if entry['s3_https']]

        if not vsi_urls_for_vrt:
            logger.critical("No valid VSI URLs to process for VRT. Exiting.")
            sys.exit(1)

        with open(out_vsi_url_list_file, 'w') as f_out:
            for url in vsi_urls_for_vrt:
                f_out.write(url + '\n')
        logger.info(f"Successfully wrote {len(vsi_urls_for_vrt)} VSI URLs to {out_vsi_url_list_file}")

        if s3_https_urls_for_log:
            with open(out_s3_https_url_list_file, 'w') as f_out_s3:
                for url in s3_https_urls_for_log:
                    f_out_s3.write(url + '\n')
            logger.info(f"Successfully wrote {len(s3_https_urls_for_log)} original S3 HTTPS URLs to {out_s3_https_url_list_file}")
        else:
            logger.info(f"No S3 HTTPS URLs to write to {out_s3_https_url_list_file} (possibly all local files or non-S3 sources).")


        vrt_start_time = datetime.now()
        cmd_gdalbuildvrt = [
            str(Config.GDAL_BUILDVRT_BIN), str(vrt_file),
            "-input_file_list", str(out_vsi_url_list_file) # Use VSI URLs for VRT
        ]
        run_subprocess_command(cmd_gdalbuildvrt, suppress_403_warnings=True, log_prefix="GDALBUILDVRT")
        vrt_build_time = datetime.now() - vrt_start_time

        translate_start_time = datetime.now()
        projwin_args = [str(val) for val in [projwin_ulx, projwin_uly, projwin_lrx, projwin_lry]]
        cmd_gdal_translate_list = [
            str(Config.GDAL_TRANSLATE_BIN), str(vrt_file),
            "-projwin"] + projwin_args + [
            "-projwin_srs", "EPSG:4326",
            "-of", "GTIFF",
            "-co", "COMPRESS=deflate", "-co", "TILED=YES",
            "-co", "blockxsize=512", "-co", "blockysize=512",
            "-co", "NUM_THREADS=ALL_CPUS",
            str(output_tiff_file)
        ]
        run_subprocess_command(cmd_gdal_translate_list, suppress_403_warnings=False, log_prefix="GDAL_TRANSLATE")
        translate_time = datetime.now() - translate_start_time
        logger.info(f"Successfully created {output_tiff_file}")

    except subprocess.CalledProcessError:
        logger.critical("A GDAL command failed. Check logs. Exiting.")
        sys.exit(1)
    except IOError as e:
        logger.critical(f"File I/O error during GDAL processing: {e}. Exiting.", exc_info=True)
        sys.exit(1)
    except Exception as e:
        logger.critical(f"Unexpected error during GDAL processing: {type(e).__name__}: {e}. Exiting.", exc_info=True)
        sys.exit(1)
    finally:
        if not Config.KEEP_TEMP_FILES:
            logger.info("\n--- Performing Cleanup (KEEP_TEMP_FILES is False) ---")
            try:
                if out_vsi_url_list_file.exists(): # Changed variable name
                    os.remove(out_vsi_url_list_file)
                    logger.info(f"Removed temporary VSI URL list file: {out_vsi_url_list_file}")
                if vrt_file.exists():
                    os.remove(vrt_file)
                    logger.info(f"Removed temporary VRT file: {vrt_file}")
            except OSError as e:
                logger.warning(f"Failed to remove one or more temporary files: {e}", exc_info=True)
        else:
            logger.info(f"\n--- Skipping Cleanup (KEEP_TEMP_FILES is True) ---")
            logger.info(f"Temporary VSI URL list file: {out_vsi_url_list_file}") # Changed variable name
            logger.info(f"Temporary VRT file: {vrt_file}")
            logger.info(f"Original S3 HTTPS URL list file (not temp): {out_s3_https_url_list_file}")


    logger.info("\n--- Final Execution Times ---")
    if query_time:  logger.info(f"Time to query URLs ({retrieval_method_used}): {query_time}")
    if vrt_build_time: logger.info(f"Time to build VRT:             {vrt_build_time}")
    if translate_time: logger.info(f"Time to run GDAL translate:    {translate_time}")
    
    logger.info("--- Script Finished Successfully ---")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Retrieve USGS 1m DEM data for a given AOI, using USGS API with a local STAC fallback.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="If no command-line arguments are specified for AOI or output directory, "
               "the script will use the default values defined in its configuration."
    )
    parser.add_argument("--minlon", type=float, default=Config.DEFAULT_MIN_LON, 
                        help="Minimum longitude of AOI (EPSG:4326).")
    parser.add_argument("--minlat", type=float, default=Config.DEFAULT_MIN_LAT, 
                        help="Minimum latitude of AOI (EPSG:4326).")
    parser.add_argument("--maxlon", type=float, default=Config.DEFAULT_MAX_LON, 
                        help="Maximum longitude of AOI (EPSG:4326).")
    parser.add_argument("--maxlat", type=float, default=Config.DEFAULT_MAX_LAT, 
                        help="Maximum latitude of AOI (EPSG:4326).")
    parser.add_argument("--output_dir", type=str, default=str(Config.DEFAULT_OUTPUT_BASE_DIR),
                        help="Base directory for all outputs (logs, temp files, final GeoTIFF). This directory MUST exist.")
    parser.add_argument("--keep_temp_files", action='store_true',
                        help="If specified, temporary files (VSI URL list, VRT) will not be deleted (default: False).")
    parser.add_argument("--force_local_stac", action='store_true',
                        help="If specified, bypass the USGS Product API and use only the local STAC catalog (default: False).")

    cli_args = parser.parse_args()
    main(cli_args)
