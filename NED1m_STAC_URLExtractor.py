import pystac
from pathlib import Path
import logging
import sys
from datetime import datetime

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
class ExtractConfig:
    # Path to the root STAC catalog.json file that you want to inspect
    CATALOG_PATH = Path("/data/matt/NED1m_STAC/catalog.json") # Match your existing catalog

    # ID of the collection within the catalog to traverse
    COLLECTION_ID_TO_INSPECT = "elevation-1m" # Match your existing collection ID

    # Asset key for the GeoTIFF
    ASSET_KEY = "elevation-geotiff"

    # Logging Settings
    LOG_LEVEL = logging.INFO # DEBUG, INFO, WARNING, ERROR, CRITICAL
    
    # Basenames for output files - they will be placed in the same directory as CATALOG_PATH
    # or a subdirectory if preferred. For simplicity, same directory as catalog.
    OUTPUT_URL_FILE_BASENAME = f"extracted_geotiff_urls_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    LOG_FILE_BASENAME = f"url_extractor_{datetime.now().strftime('%Y%m%d')}.log"

# ----------------------------------------------------------------------
# Logging Setup
# ----------------------------------------------------------------------
def setup_logging(log_file_path, log_level):
    """Configures the logging system."""
    # Ensure log file directory exists
    log_file_path.parent.mkdir(parents=True, exist_ok=True)
    
    logger_instance = logging.getLogger(__name__) # Use a specific logger name
    
    # Remove existing handlers to avoid duplicate logging if function is called multiple times
    for handler in logger_instance.handlers[:]:
        logger_instance.removeHandler(handler)
        handler.close()

    logger_instance.setLevel(log_level)
    
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    fh = logging.FileHandler(log_file_path, mode='a') 
    fh.setFormatter(formatter)
    logger_instance.addHandler(fh)
    
    sh = logging.StreamHandler(sys.stdout) 
    sh.setFormatter(formatter)
    logger_instance.addHandler(sh)
    
    return logger_instance

# Construct log file path using ExtractConfig attributes
# Log file will be in the same directory as the catalog.json
log_file_path_dynamic = ExtractConfig.CATALOG_PATH.parent / ExtractConfig.LOG_FILE_BASENAME
logger = setup_logging(log_file_path_dynamic, ExtractConfig.LOG_LEVEL)

# ----------------------------------------------------------------------
# Core URL Extraction Function
# ----------------------------------------------------------------------
def extract_asset_urls_from_stac(catalog_path_obj: Path, collection_id: str, asset_key_to_extract: str, output_url_file_path: Path):
    """
    Reads a STAC catalog, iterates through items in a specified collection,
    and writes the HREFs of a specific asset to an output file.

    Args:
        catalog_path_obj (Path): Path object for the root catalog.json file.
        collection_id (str): The ID of the collection to process.
        asset_key_to_extract (str): The key of the asset whose HREF to extract.
        output_url_file_path (Path): Path object for the output text file for URLs.
    """
    logger.info(f"Starting URL extraction from STAC catalog: {catalog_path_obj}")
    logger.info(f"Target collection ID: {collection_id}")
    logger.info(f"Target asset key: {asset_key_to_extract}")
    logger.info(f"Output file for URLs: {output_url_file_path}")

    start_time = datetime.now()
    extracted_urls = []
    items_processed_count = 0
    items_with_asset_count = 0

    if not catalog_path_obj.is_file():
        logger.error(f"Root catalog file not found: {catalog_path_obj}")
        return False

    try:
        logger.info(f"Loading STAC catalog from: {catalog_path_obj}...")
        catalog = pystac.read_file(str(catalog_path_obj))
        
        logger.info(f"Getting collection: '{collection_id}'...")
        collection = catalog.get_child(collection_id, recursive=True)
        
        if not collection:
            logger.error(f"Collection '{collection_id}' not found in catalog '{catalog_path_obj}'.")
            return False
        if not isinstance(collection, pystac.Collection):
            logger.error(f"Object with ID '{collection_id}' found, but it is not a STAC Collection.")
            return False

        logger.info(f"Successfully loaded collection '{collection.id}'. Iterating through items...")

        for item in collection.get_all_items():
            items_processed_count += 1
            asset = item.assets.get(asset_key_to_extract)
            if asset and asset.href:
                extracted_urls.append(asset.href)
                items_with_asset_count += 1
                logger.debug(f"Found URL for item {item.id}: {asset.href}")
            else:
                logger.warning(f"Asset key '{asset_key_to_extract}' or its href not found for item: {item.id} (Self Href: {item.get_self_href()})")
            
            if items_processed_count % 10000 == 0: # Log progress every 10000 items
                logger.info(f"Processed {items_processed_count} items so far...")

        logger.info(f"Finished iterating. Total items processed: {items_processed_count}. Items with target asset found: {items_with_asset_count}.")

        # Write URLs to the output file
        output_url_file_path.parent.mkdir(parents=True, exist_ok=True) # Ensure output directory exists
        with open(output_url_file_path, 'w') as f_out:
            for url in extracted_urls:
                f_out.write(url + '\n')
        logger.info(f"Successfully wrote {len(extracted_urls)} URLs to {output_url_file_path}")

        end_time = datetime.now()
        logger.info(f"URL extraction duration: {end_time - start_time}")
        return True

    except FileNotFoundError: 
        logger.error(f"Catalog file not found at {catalog_path_obj}", exc_info=True)
    except Exception as e:
        logger.error(f"An unexpected error occurred during URL extraction: {type(e).__name__} - {e}", exc_info=True)
    
    return False

# ----------------------------------------------------------------------
# Main Execution
# ----------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("===== Starting STAC Asset URL Extractor Script =====")
    script_overall_start_time = datetime.now()

    # Construct full output path for the URL list using the catalog's parent directory
    output_url_file_full_path = ExtractConfig.CATALOG_PATH.parent / ExtractConfig.OUTPUT_URL_FILE_BASENAME

    success = extract_asset_urls_from_stac(
        catalog_path_obj=ExtractConfig.CATALOG_PATH,
        collection_id=ExtractConfig.COLLECTION_ID_TO_INSPECT,
        asset_key_to_extract=ExtractConfig.ASSET_KEY,
        output_url_file_path=output_url_file_full_path
    )

    if success:
        logger.info("URL extraction process completed successfully.")
    else:
        logger.error("URL extraction process failed. Check logs for details.")

    script_overall_end_time = datetime.now()
    total_script_duration = script_overall_end_time - script_overall_start_time
    logger.info(f"Total script execution time: {total_script_duration}")
    logger.info("===== STAC Asset URL Extractor Script Finished =====")

