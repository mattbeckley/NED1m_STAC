import argparse
from collections import defaultdict
import boto3
import xml.etree.ElementTree as ET
import json
import pystac
from pystac.extensions.eo import EOExtension
from shapely.geometry import Polygon, box
from shapely.ops import unary_union
import os
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
import logging
import sys
from rtree import index as rtree_index
import pickle

# -----------------------------------------------------------------------------
# STAC Catalog Builder, Updater, Indexer, and OSN Migrator
# for USGS TNM 1m Elevation Data
# -----------------------------------------------------------------------------
#
# Purpose:
# Builds and maintains a local STAC (SpatioTemporal Asset Catalog) from the
# XML metadata files for USGS The National Map (TNM) 1-meter DEM products
# hosted on the prd-tnm S3 bucket. Each STAC item stores two asset URLs:
#   - elevation-geotiff     : public USGS S3 URL
#   - elevation-geotiff-osn : OSN mirror URL (used as failover by NED1m_Query.py)
#
# The OSN mirror (usgs.osn.mghpcc.org) is a full copy of the USGS prd-tnm
# bucket maintained via rclone sync. It serves as a reliable failover when
# USGS S3 is unavailable. OSN requires S3-compatible credentials and is not
# publicly accessible.
#
# Operating Modes:
#
#   Default (no flags):
#     Automatically detects whether an existing catalog is present and runs
#     in create mode (no catalog found) or update mode (catalog exists).
#     - Create mode: processes all project folders on S3 from scratch.
#     - Update mode: identifies project folders not yet in the catalog and
#       adds only those, then rebuilds the R-tree index if any changes were made.
#     Both modes add elevation-geotiff and elevation-geotiff-osn assets to
#     every new STAC item. Run weekly via cron to keep the catalog current.
#
#   --add-osn-assets:
#     One-time migration mode. Adds the elevation-geotiff-osn asset to all
#     existing catalog items that were created before OSN support was added.
#     Supports --dry-run to preview changes without writing, and checkpointing
#     so an interrupted run can be resumed from where it left off.
#     After all items are updated, saves the catalog and rebuilds the R-tree.
#
# Command-Line Arguments:
#   (no flags)             Run the normal weekly create/update pipeline.
#
#   --add-osn-assets       Migration mode: add OSN mirror assets to existing
#                          catalog items that are missing them.
#                          Default: False
#
#   --dry-run              Used with --add-osn-assets. Logs what would be
#                          changed without writing anything to disk.
#                          Default: False
#
#   --stac_catalog_path PATH
#                          Used with --add-osn-assets. Override the catalog
#                          directory (derived from the given catalog.json path).
#                          Useful for running the migration against a test
#                          catalog before touching production.
#                          Default: uses Config.OUTPUT_DIRECTORY_BASE
#
# Config Variables (edit in the Config class to change behavior):
#   OUTPUT_DIRECTORY_BASE  Root directory for the catalog and index files.
#                          Default: /data/matt/NED1m_STAC/
#   PROJECTS_TO_PROCESS    List of specific project folder names to process,
#                          or None to process all. Useful for small-batch tests.
#                          Default: None (process all)
#   MAX_PROJECT_FOLDERS_TO_PROCESS
#                          Integer cap on the number of folders processed, or
#                          None for no limit. Applied after PROJECTS_TO_PROCESS
#                          filtering. Useful for quick smoke tests.
#                          Default: None (no limit)
#   OSN_ENDPOINT           Base URL of the OSN S3-compatible endpoint.
#                          Default: https://usgs.osn.mghpcc.org
#   OSN_BUCKET_NAME        OSN bucket containing the USGS mirror.
#                          Default: ot-usgs-osn
#   LOG_LEVEL              Logging verbosity (DEBUG, INFO, WARNING, ERROR).
#                          Default: INFO
#
# Key Behaviors:
# - Empty project folders (no XML files) are skipped and will be picked up on
#   a future run once XML files are added.
# - The R-tree index is only rebuilt when the catalog is actually modified,
#   avoiding unnecessary work on no-change weekly runs.
# - URL normalization handles multiple networkr path formats found in USGS XML
#   metadata: HTTP/HTTPS, S3 URIs, FTP (rockyftp/rockyweb), and Windows UNC paths.
# - The --add-osn-assets migration writes a checkpoint file
#   (osn_migration_progress.txt) after each project so a failed run can be
#   resumed without restarting from scratch.
#
# Prerequisites: boto3, pystac, shapely, rtree, requests
# Last Substantial Modification: 2026-04-24
# -----------------------------------------------------------------------------

# --- Configuration ---
class Config:
    # --- Part 1: STAC Update/Create Config ---
    OUTPUT_DIRECTORY_BASE = Path("/data/matt/NED1m_STAC/")
    BUCKET_NAME = 'prd-tnm'
    S3_PREFIX = 'StagedProducts/Elevation/1m/Projects/'
    COLLECTION_ID = "elevation-1m"
    COLLECTION_TITLE = "1m Elevation Data (USGS TNM)"
    COLLECTION_DESCRIPTION = "1-meter resolution elevation data from the USGS The National Map (TNM) projects, processed into a STAC Catalog."
    S3_PUBLIC_BASE_URL = "https://prd-tnm.s3.amazonaws.com/"

    #set to an int to process a small subset for testing, otherwise set to None to process ALL DATA
    MAX_PROJECT_FOLDERS_TO_PROCESS = None

    # Set to None to process ALL DATA, or set to a list of specific projects for testing.
    # Testing example: PROJECTS_TO_PROCESS = ["AZ_LowerColoradoRiver_2015", "LA_NortheastDOTD_2017_C20"]
    PROJECTS_TO_PROCESS = None

    # OSN mirror settings
    # The rclone sync maps prd-tnm/StagedProducts/Elevation/1m/ → ot-usgs-osn/Elevation/1m/
    # so the StagedProducts/ prefix is dropped in OSN paths.
    OSN_ENDPOINT = "https://usgs.osn.mghpcc.org"
    OSN_BUCKET_NAME = "ot-usgs-osn"
    OSN_BASE_URL = f"{OSN_ENDPOINT}/{OSN_BUCKET_NAME}/"
    USGS_S3_PREFIX_IN_OSN = "StagedProducts/"  # this prefix is stripped when mapping to OSN
    ROCKYWEB_PREFIX = "https://rockyweb.usgs.gov/vdelivery/Datasets/Staged/"

    #Log settings can be set to: DEBUG, INFO, WARNING, ERROR, CRITICAL
    LOG_LEVEL = logging.INFO

    # --- Part 2: STAC Indexing Config ---
    # These paths derive from the settings above
    CATALOG_PATH = OUTPUT_DIRECTORY_BASE / "catalog.json"
    COLLECTION_ID_TO_INDEX = COLLECTION_ID
    INDEX_BASENAME = 'stac_spatial_index'

# --- Logging Setup ---
current_date_str = datetime.now().strftime("%Y%m%d")
LOG_FILE_NAME = f"stac_pipeline_{current_date_str}.log"
LOG_FILE = Config.OUTPUT_DIRECTORY_BASE / "logs" / LOG_FILE_NAME
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=Config.LOG_LEVEL,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, mode='a'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# =============================================================================
# --- STAC UPDATE/CREATE HELPER FUNCTIONS ---
# =============================================================================

def _parse_xml_to_stac_item_properties(xml_content, xml_key, s3_project_folder_prefix):
    try:
        root = ET.fromstring(xml_content)
        properties = {}
        westbc = root.findtext('.//westbc')
        eastbc = root.findtext('.//eastbc')
        northbc = root.findtext('.//northbc')
        southbc = root.findtext('.//southbc')
        networkr_path = root.findtext('.//networkr')
        begdate_str = root.findtext('.//begdate')
        enddate_str = root.findtext('.//enddate')

        if not all([westbc, eastbc, northbc, southbc, networkr_path]):
            logger.warning(f"Missing essential metadata (bbox or networkr) in {xml_key}. Skipping item.")
            return None, None, None, None, None, None
        minx, miny, maxx, maxy = float(westbc), float(southbc), float(eastbc), float(northbc)
        bbox = [minx, miny, maxx, maxy]
        geometry_shapely = box(minx, miny, maxx, maxy)
        geometry_geojson = geometry_shapely.__geo_interface__
        item_id = xml_key.replace('/', '_').replace('.xml', '')
        properties['s3_project_folder'] = s3_project_folder_prefix
        item_datetime_obj = None
        if begdate_str:
            try:
                dt_obj = datetime.strptime(begdate_str, '%Y%m%d').replace(tzinfo=timezone.utc)
                properties["start_datetime"] = dt_obj.isoformat().replace('+00:00', 'Z')
                item_datetime_obj = dt_obj
            except ValueError as ve:
                logger.warning(f"Could not parse begdate '{begdate_str}' in {xml_key}: {ve}. Setting item datetime to None.")
        if enddate_str:
            try:
                properties["end_datetime"] = datetime.strptime(enddate_str, '%Y%m%d').replace(tzinfo=timezone.utc).isoformat().replace('+00:00', 'Z')
            except ValueError as ve:
                logger.warning(f"Could not parse enddate '{enddate_str}' in {xml_key}: {ve}")
        if not item_datetime_obj and "end_datetime" in properties:
             item_datetime_obj = datetime.fromisoformat(properties["end_datetime"].replace('Z', '+00:00')).replace(tzinfo=timezone.utc)

        if item_datetime_obj is None and "start_datetime" not in properties and "end_datetime" not in properties:
            properties["start_datetime"] = None
            properties["end_datetime"] = None

        https_url = ""
        networkr_lower = networkr_path.lower()

        if networkr_lower.startswith("http://") or networkr_lower.startswith("https://"):
            https_url = networkr_path
            logger.debug(f"Using networkr_path directly as it's a full URL: {https_url}")
        elif networkr_lower.startswith("s3://"):
            s3_path_parts = networkr_path[5:].split('/')
            if len(s3_path_parts) > 1:
                s3_bucket_in_uri = s3_path_parts[0]
                s3_key_for_url = "/".join(s3_path_parts[1:])
                https_url = f"https://{s3_bucket_in_uri}.s3.amazonaws.com/{s3_key_for_url}"
                logger.debug(f"Converted S3 URI '{networkr_path}' to HTTPS URL: {https_url}")
            else:
                logger.warning(f"Could not parse S3 URI in networkr_path: {networkr_path}")

        elif networkr_lower.startswith("ftp://rockyftp.cr.usgs.gov/vdelivery/datasets/staged/"):
            ftp_prefix_to_remove = "ftp://rockyftp.cr.usgs.gov/vdelivery/datasets/staged/"
            try:
                path_after_ftp_staged = networkr_path[len(ftp_prefix_to_remove):]
                # Prepend "StagedProducts/" to the remainder of the path
                s3_key_from_ftp = f"StagedProducts/{path_after_ftp_staged.lstrip('/')}"
                https_url = f"{Config.S3_PUBLIC_BASE_URL}{s3_key_from_ftp}"
                logger.debug(f"Converted specific FTP URL (with '/Staged/') '{networkr_path}' to HTTPS URL: {https_url}")
            except Exception as e_ftp:
                logger.warning(f"Error parsing specific FTP URL (with '/Staged/') '{networkr_path}': {e_ftp}")
        elif networkr_lower.startswith("ftp://rockyftp.cr.usgs.gov/vdelivery/datasets/stagedproducts/"): # If FTP URL uses StagedProducts
            ftp_prefix_to_remove = "ftp://rockyftp.cr.usgs.gov/vdelivery/datasets/stagedproducts/"
            try:
                s3_key_from_ftp = networkr_path[len(ftp_prefix_to_remove):].lstrip('/')
                https_url = f"{Config.S3_PUBLIC_BASE_URL}{s3_key_from_ftp}"
                logger.debug(f"Converted FTP URL (with '/StagedProducts/') '{networkr_path}' to HTTPS URL: {https_url}")
            except Exception as e_ftp:
                logger.warning(f"Error parsing FTP URL (with '/StagedProducts/') '{networkr_path}': {e_ftp}")

        elif networkr_path.startswith("\\\\") and Config.BUCKET_NAME in networkr_path:
            path_after_share = None
            path_components = networkr_path.replace("\\", "/").split('/')
            found_s3_like_path = False
            for i, component in enumerate(path_components):
                if component.lower() in ["stagedproducts", "staged"]:
                    if component.lower() == "staged":
                        path_after_share = "StagedProducts/" + "/".join(path_components[i+1:])
                    else: # It was "StagedProducts"
                        path_after_share = "/".join(path_components[i:])
                    s3_key_from_network_path = path_after_share.lstrip("/")
                    https_url = f"{Config.S3_PUBLIC_BASE_URL}{s3_key_from_network_path}"
                    logger.debug(f"Converted network path '{networkr_path}' to HTTPS URL: {https_url}")
                    found_s3_like_path = True
                    break
            if not found_s3_like_path:
                logger.warning(f"Could not reliably parse Windows network path for S3 key: {networkr_path}")
        else: # Fallback for relative paths or simple filenames
            if "TIFF/" in networkr_path.upper():
                asset_s3_key = f"{s3_project_folder_prefix.rstrip('/')}/{networkr_path.lstrip('/')}"
            else:
                asset_s3_key = f"{s3_project_folder_prefix.rstrip('/')}/TIFF/{Path(networkr_path).name}"
            https_url = f"{Config.S3_PUBLIC_BASE_URL}{asset_s3_key}"
            logger.debug(f"Constructed HTTPS URL from relative networkr_path '{networkr_path}': {https_url}")

        if not https_url:
            logger.error(f"Failed to determine a valid asset HTTPS URL from networkr_path: '{networkr_path}' in {xml_key}")
            return None, None, None, None, None, None

        return item_id, bbox, geometry_geojson, item_datetime_obj, properties, https_url
    except Exception as e:
        logger.error(f"Error parsing XML {xml_key}: {type(e).__name__} - {e}", exc_info=True)
        return None, None, None, None, None, None

def _usgs_url_to_osn_url(usgs_https_url):
    """
    Converts a USGS HTTPS URL to its OSN mirror equivalent.
    Handles both prd-tnm S3 and rockyweb.usgs.gov URLs — both map to the
    same OSN path after stripping their respective prefixes up to 'Staged/':
      https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1m/...
      https://rockyweb.usgs.gov/vdelivery/Datasets/Staged/Elevation/1m/...
      → https://usgs.osn.mghpcc.org/ot-usgs-osn/Elevation/1m/...
    Returns None if the URL cannot be mapped.
    """
    if not usgs_https_url:
        return None
    usgs_s3_prefix = f"{Config.S3_PUBLIC_BASE_URL}{Config.USGS_S3_PREFIX_IN_OSN}"
    if usgs_https_url.startswith(usgs_s3_prefix):
        return f"{Config.OSN_BASE_URL}{usgs_https_url[len(usgs_s3_prefix):]}"
    if usgs_https_url.startswith(Config.ROCKYWEB_PREFIX):
        return f"{Config.OSN_BASE_URL}{usgs_https_url[len(Config.ROCKYWEB_PREFIX):]}"
    return None

def _create_stac_item(item_id, bbox, geometry_geojson, item_datetime_obj, properties, asset_href):
    item = pystac.Item(id=item_id, geometry=geometry_geojson, bbox=bbox, datetime=item_datetime_obj, properties=properties)
    item.add_asset("elevation-geotiff", pystac.Asset(href=asset_href, media_type=pystac.MediaType.COG, title="1m Elevation GeoTIFF", roles=["data", "elevation"]))
    osn_url = _usgs_url_to_osn_url(asset_href)
    if osn_url:
        item.add_asset("elevation-geotiff-osn", pystac.Asset(href=osn_url, media_type=pystac.MediaType.COG, title="1m Elevation GeoTIFF (OSN mirror)", roles=["data", "elevation"]))
    else:
        logger.debug(f"Could not derive OSN URL for item {item_id}, asset href: {asset_href}")
    return item

def add_osn_assets_to_catalog(output_dir_path, dry_run=False):
    """
    One-time migration: adds elevation-geotiff-osn assets to existing catalog items
    that have a mappable USGS URL. Supports dry-run and checkpointing so a failed
    mid-run can be resumed without restarting from scratch.
    """
    output_dir = Path(output_dir_path)
    catalog_file = output_dir / "catalog.json"
    checkpoint_file = output_dir / "osn_migration_progress.txt"

    if not catalog_file.exists():
        logger.error(f"Catalog not found: {catalog_file}")
        return False

    completed_projects = set()
    if checkpoint_file.exists():
        completed_projects = {l.strip() for l in checkpoint_file.read_text().splitlines() if l.strip()}
        logger.info(f"Checkpoint: {len(completed_projects)} project(s) already processed, will skip.")

    logger.info(f"Loading catalog from {catalog_file}...")
    catalog = pystac.read_file(str(catalog_file))
    collection = catalog.get_child(Config.COLLECTION_ID, recursive=True)
    if not collection:
        logger.error(f"Collection '{Config.COLLECTION_ID}' not found in catalog.")
        return False

    logger.info("Grouping items by project folder...")
    projects = defaultdict(list)
    for item in collection.get_all_items():
        projects[item.properties.get('s3_project_folder', '')].append(item)

    total_projects = len(projects)
    skippable = len(completed_projects & projects.keys())
    logger.info(f"Catalog has {total_projects} project(s). {skippable} already checkpointed, "
                f"{total_projects - skippable} to process.")

    total_added = 0
    total_already_have_osn = 0
    total_unmappable = 0

    for project_folder, items in sorted(projects.items()):
        if project_folder in completed_projects:
            continue

        project_added = 0
        project_unmappable = 0

        for item in items:
            if 'elevation-geotiff-osn' in item.assets:
                total_already_have_osn += 1
                continue
            usgs_asset = item.assets.get('elevation-geotiff')
            if not usgs_asset:
                logger.warning(f"Item {item.id}: no elevation-geotiff asset, skipping.")
                project_unmappable += 1
                continue
            osn_url = _usgs_url_to_osn_url(usgs_asset.href)
            if osn_url:
                if not dry_run:
                    item.add_asset("elevation-geotiff-osn", pystac.Asset(
                        href=osn_url,
                        media_type=pystac.MediaType.COG,
                        title="1m Elevation GeoTIFF (OSN mirror)",
                        roles=["data", "elevation"]
                    ))
                project_added += 1
            else:
                logger.warning(f"Item {item.id}: cannot map to OSN URL ({usgs_asset.href}), skipping.")
                project_unmappable += 1

        total_added += project_added
        total_unmappable += project_unmappable
        prefix = "[DRY RUN] " if dry_run else ""
        verb = "would add" if dry_run else "added"
        logger.info(f"{prefix}{project_folder}: {project_added} OSN asset(s) {verb}, "
                    f"{project_unmappable} unmappable.")

        if not dry_run:
            with open(checkpoint_file, 'a') as f:
                f.write(project_folder + '\n')

    prefix = "[DRY RUN] " if dry_run else ""
    verb = "would be added" if dry_run else "added"
    logger.info(f"{prefix}Migration summary: {total_added} OSN asset(s) {verb}, "
                f"{total_already_have_osn} item(s) already had OSN asset, "
                f"{total_unmappable} item(s) unmappable (see warnings above).")

    if not dry_run and total_added > 0:
        logger.info("Saving updated catalog...")
        catalog.normalize_hrefs(str(output_dir))
        catalog.save(catalog_type=pystac.CatalogType.SELF_CONTAINED, dest_href=str(output_dir))
        logger.info("Rebuilding R-tree index...")
        index_stac_collection(
            catalog_path_str=str(catalog_file),
            collection_id=Config.COLLECTION_ID_TO_INDEX,
            index_basename=Config.INDEX_BASENAME
        )
    elif not dry_run:
        logger.info("No items needed updating.")

    return True


def _process_s3_project_folder(s3_client, s3_project_folder_prefix_str):
    items_in_folder = []
    logger.info(f"Processing project folder: {s3_project_folder_prefix_str}")
    metadata_s3_prefix = f"{s3_project_folder_prefix_str.rstrip('/')}/metadata/"
    paginator = s3_client.get_paginator('list_objects_v2')
    page_iterator = paginator.paginate(Bucket=Config.BUCKET_NAME, Prefix=metadata_s3_prefix)
    xml_count = 0
    for page in page_iterator:
        for obj in page.get('Contents', []):
            if obj['Key'].endswith('.xml'):
                xml_count += 1
                xml_key = obj['Key']
                try:
                    xml_object = s3_client.get_object(Bucket=Config.BUCKET_NAME, Key=xml_key)
                    xml_content = xml_object['Body'].read().decode('utf-8')
                    item_id, bbox, geom, dt, props, asset_href = _parse_xml_to_stac_item_properties(xml_content, xml_key, s3_project_folder_prefix_str)
                    if item_id and asset_href:
                        stac_item = _create_stac_item(item_id, bbox, geom, dt, props, asset_href)
                        items_in_folder.append(stac_item)
                except Exception as e:
                    logger.error(f"Error processing XML file {xml_key}: {e}", exc_info=True)
    logger.info(f"Found {xml_count} XML files, created {len(items_in_folder)} STAC items for {s3_project_folder_prefix_str}.")
    return items_in_folder

def _update_collection_extents(collection_object):
    all_item_bboxes = [item.bbox for item in collection_object.get_all_items() if item.bbox]
    all_item_datetimes = []
    for item in collection_object.get_all_items():
        dt_to_add = None
        if item.datetime:
            dt_to_add = item.datetime
        elif "start_datetime" in item.properties:
            try:
                dt_to_add = datetime.fromisoformat(item.properties["start_datetime"].replace('Z', '+00:00')).replace(tzinfo=timezone.utc)
            except Exception: pass
        if dt_to_add:
            all_item_datetimes.append(dt_to_add)

    if all_item_bboxes:
        min_lon = min(b[0] for b in all_item_bboxes)
        min_lat = min(b[1] for b in all_item_bboxes)
        max_lon = max(b[2] for b in all_item_bboxes)
        max_lat = max(b[3] for b in all_item_bboxes)
        collection_object.extent.spatial = pystac.SpatialExtent([[min_lon, min_lat, max_lon, max_lat]])
        logger.info(f"Updated collection spatial extent: {[min_lon, min_lat, max_lon, max_lat]}")

    if all_item_datetimes:
        aware_datetimes = [dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt for dt in all_item_datetimes]
        if aware_datetimes:
            min_start_time = min(aware_datetimes)
            max_end_time = max(aware_datetimes)
            collection_object.extent.temporal = pystac.TemporalExtent([[min_start_time, max_end_time]])
            logger.info(f"Updated collection temporal extent: {min_start_time.isoformat().replace('+00:00', 'Z')} / {max_end_time.isoformat().replace('+00:00', 'Z')}")

# --- Main Update/Create Function ---
def create_or_update_stac_catalog(output_dir_path, s3_bucket_name, s3_prefix_base, update_mode=False):
    """
    Creates or updates the STAC catalog.
    Returns:
        bool: True if changes were made, False otherwise.
    """
    process_start_time = datetime.now()
    s3 = boto3.client('s3')
    output_dir = Path(output_dir_path)
    catalog_file = output_dir / "catalog.json"
    expected_collection_path = output_dir / Config.COLLECTION_ID / "collection.json"

    catalog = None
    elevation_collection = None

    if update_mode:
        if not catalog_file.exists() or not expected_collection_path.exists():
            logger.info(f"Update mode selected, but catalog or collection not found. Will create new.")
            update_mode = False
        else:
            try:
                logger.info(f"Update mode: Loading existing catalog from {catalog_file}")
                catalog = pystac.read_file(str(catalog_file))
                elevation_collection = catalog.get_child(Config.COLLECTION_ID, recursive=True)
                if not elevation_collection:
                    logger.error(f"Collection '{Config.COLLECTION_ID}' not found. Reverting to create mode.")
                    update_mode = False
            except Exception as e:
                logger.error(f"Failed to load existing catalog for update: {e}. Will create new.", exc_info=True)
                update_mode = False

    if not update_mode:
        logger.info("Create mode: Initializing new STAC catalog.")
        catalog = pystac.Catalog(id="tnm-elevation-catalog", title="USGS TNM 1m Elevation Data", description="STAC Catalog of 1-meter Elevation Data from USGS The National Map (TNM)")
        elevation_collection = pystac.Collection(
            id=Config.COLLECTION_ID, title=Config.COLLECTION_TITLE, description=Config.COLLECTION_DESCRIPTION,
            extent=pystac.Extent(pystac.SpatialExtent([[-180, -90, 180, 90]]), pystac.TemporalExtent([[None, None]])),
            license="CC0-1.0", keywords=["elevation", "dem", "lidar", "usgs", "tnm", "1m"]
        )
        catalog.add_child(elevation_collection)

    try:
        paginator = s3.get_paginator('list_objects_v2')
        page_iterator = paginator.paginate(Bucket=s3_bucket_name, Prefix=s3_prefix_base, Delimiter='/')
        s3_project_folders_on_s3_all = [p['Prefix'] for page in page_iterator for p in page.get('CommonPrefixes', [])]
    except Exception as e:
        logger.error(f"Failed to list S3 project folders: {e}", exc_info=True)
        return False # Return False on error

    folders_to_process = []
    new_items_added_count = 0

    if update_mode and elevation_collection:
        processed_s3_folders_in_stac = {item.properties['s3_project_folder'] for item in elevation_collection.get_all_items() if 's3_project_folder' in item.properties}
        folders_to_process = [f for f in s3_project_folders_on_s3_all if f not in processed_s3_folders_in_stac]
        logger.info(f"Identified {len(folders_to_process)} new project folders to process.")
    else:
        folders_to_process = s3_project_folders_on_s3_all
        logger.info(f"Create mode: Will process {len(folders_to_process)} project folders.")

    if Config.PROJECTS_TO_PROCESS is not None:
        folders_to_process = [f for f in folders_to_process if Path(f.rstrip('/')).name in Config.PROJECTS_TO_PROCESS]
        logger.info(f"Filtered to {len(folders_to_process)} project folder(s) via PROJECTS_TO_PROCESS.")

    if Config.MAX_PROJECT_FOLDERS_TO_PROCESS is not None:
        folders_to_process = folders_to_process[:Config.MAX_PROJECT_FOLDERS_TO_PROCESS]
        logger.info(f"Capped to {len(folders_to_process)} folder(s) via MAX_PROJECT_FOLDERS_TO_PROCESS.")

    if folders_to_process:
        for s3_project_folder_prefix in folders_to_process:
            items_from_folder = _process_s3_project_folder(s3, s3_project_folder_prefix)
            if items_from_folder and elevation_collection:
                for item in items_from_folder:
                    elevation_collection.add_item(item)
                    new_items_added_count += 1

    if update_mode and new_items_added_count == 0:
        logger.info("Update mode: No new items were added. Catalog is up-to-date.")
        return False

    if new_items_added_count > 0 or not update_mode:
        logger.info(f"Added {new_items_added_count} new items. Updating collection and saving.")
        _update_collection_extents(elevation_collection)
        output_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Normalizing HREFs relative to {output_dir}...")
        catalog.normalize_hrefs(str(output_dir))
        logger.info(f"Saving STAC Catalog to {output_dir}...")
        catalog.save(catalog_type=pystac.CatalogType.SELF_CONTAINED, dest_href=str(output_dir))
        logger.info("STAC Catalog processing complete.")
        return True
    else:
        # This case handles when create mode is run but finds no S3 folders.
        logger.info("No items were added and no catalog was saved.")
        return False


# =============================================================================
# --- STAC INDEXING FUNCTIONS ---
# =============================================================================

def index_stac_collection(catalog_path_str, collection_id, index_basename):
    """
    Creates an R-tree spatial index for items in a specified STAC collection.
    """
    logger.info(f"--- Starting STAC Indexing for collection: {collection_id} ---")
    start_time = datetime.now()

    catalog_path = Path(catalog_path_str)
    if not catalog_path.is_file():
        logger.error(f"Catalog file not found for indexing: {catalog_path}")
        return False

    catalog_dir = catalog_path.parent
    index_file_path = catalog_dir / index_basename

    try:
        logger.info(f"Loading STAC catalog from: {catalog_path}")
        catalog = pystac.read_file(str(catalog_path))
        collection = catalog.get_child(collection_id, recursive=True)
        if not collection:
            logger.error(f"Collection '{collection_id}' not found in catalog.")
            return False

        # Remove existing index files for a fresh build
        for ext in ['.idx', '.dat', '.pkl']:
            if (old_file := index_file_path.with_suffix(ext)).exists():
                os.remove(old_file)
                logger.info(f"Removed existing index file: {old_file}")

        idx = rtree_index.Index(str(index_file_path))
        item_id_to_relative_path_map = {}
        indexed_item_count = 0

        logger.info("Iterating through collection items to build R-tree index...")
        # Use a generator for memory efficiency on very large collections
        all_items = collection.get_all_items()
        for item in all_items:
            if item.bbox:
                indexed_item_count += 1
                # R-tree insert format: insert(id, (minx, miny, maxx, maxy), obj)
                idx.insert(indexed_item_count, item.bbox, obj=item.id)
                # Store the relative path to the item's JSON file for later retrieval
                relative_path = Path(item.get_self_href()).relative_to(catalog_dir.resolve())
                item_id_to_relative_path_map[item.id] = str(relative_path)

        idx.close()
        logger.info(f"R-tree index created with {indexed_item_count} items.")

        # Save the item ID to path mapping
        pickle_file_path = index_file_path.with_suffix('.pkl')
        with open(pickle_file_path, 'wb') as f:
            pickle.dump(item_id_to_relative_path_map, f)
        logger.info(f"Item ID to path mapping saved to: {pickle_file_path}")
        logger.info(f"Indexing duration: {datetime.now() - start_time}")
        return True

    except Exception as e:
        logger.error(f"An unexpected error occurred during STAC indexing: {e}", exc_info=True)
        return False

# =============================================================================
# --- Main Execution ---
# =============================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="STAC Catalog Builder, Updater, Indexer, and OSN Migrator")
    parser.add_argument("--add-osn-assets", action="store_true",
                        help="Migration mode: add elevation-geotiff-osn assets to existing catalog items.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without writing (only applies to --add-osn-assets).")
    parser.add_argument("--stac_catalog_path", type=str, default=None,
                        help="Override the catalog path for --add-osn-assets (default: uses Config.OUTPUT_DIRECTORY_BASE).")
    cli_args = parser.parse_args()

    script_start_time = datetime.now()

    if cli_args.add_osn_assets:
        catalog_dir = Path(cli_args.stac_catalog_path).parent if cli_args.stac_catalog_path else Config.OUTPUT_DIRECTORY_BASE
        logger.info("===== Starting OSN Asset Migration =====")
        if cli_args.dry_run:
            logger.info("DRY RUN mode: no changes will be written.")
        success = add_osn_assets_to_catalog(catalog_dir, dry_run=cli_args.dry_run)
        if not success:
            sys.exit(1)
        logger.info(f"Total execution time: {datetime.now() - script_start_time}")
        logger.info("===== OSN Asset Migration Finished =====")
    else:
        logger.info("===== Starting STAC Update and Indexing Pipeline =====")

        mode = "create"
        if Config.CATALOG_PATH.exists() and (Config.OUTPUT_DIRECTORY_BASE / Config.COLLECTION_ID / "collection.json").exists():
            logger.info("Existing catalog found. Automatically running in 'update' mode.")
            mode = "update"
        else:
            logger.info("No existing catalog found. Automatically running in 'create' mode.")

        updates_were_made = create_or_update_stac_catalog(
            output_dir_path=Config.OUTPUT_DIRECTORY_BASE,
            s3_bucket_name=Config.BUCKET_NAME,
            s3_prefix_base=Config.S3_PREFIX,
            update_mode=(mode == "update")
        )

        if updates_were_made:
            index_stac_collection(
                catalog_path_str=str(Config.CATALOG_PATH),
                collection_id=Config.COLLECTION_ID_TO_INDEX,
                index_basename=Config.INDEX_BASENAME
            )
        else:
            logger.info("Catalog was already up-to-date. Indexing is not required.")

        total_script_duration = datetime.now() - script_start_time
        logger.info(f"Total pipeline execution time: {total_script_duration}")
        logger.info("===== STAC Pipeline Finished =====")

