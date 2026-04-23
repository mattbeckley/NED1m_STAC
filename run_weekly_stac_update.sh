#!/bin/bash
#
# This script performs a weekly update of the NED 1m STAC catalog.
# It first creates a backup of the existing catalog, then runs the
# Python update script using the correct conda environment.
# It is designed to be called from a cron job.

set -e # Exit immediately if a command exits with a non-zero status.

# --- Configuration ---
SOURCE_DIR="/data/matt/NED1m_STAC"
BACKUP_DIR="/data/matt/NED1m_STAC_BU"
PYTHON_SCRIPT_PATH="/home/beckley/NED/NED1m_STAC/run_stac_pipeline.py"
CONDA_PATH="/home/beckley/miniconda3" 

# --- Script Start ---
echo "--- Starting weekly STAC pipeline at $(date) ---"

# --- Step 1: Backup the STAC directory ---
# Using rsync is efficient. 
echo "Backing up ${SOURCE_DIR} to ${BACKUP_DIR}..."
rsync -a "${SOURCE_DIR}/" "${BACKUP_DIR}/"
echo "Backup completed successfully."


# --- Step 2: Run the Python update script ---
# The following block is a robust way to activate a conda environment
# from within a script, which is often necessary for cron jobs.
echo "Activating conda environment 'stac'..."
source "${CONDA_PATH}/etc/profile.d/conda.sh"
conda activate stac

echo "Conda environment activated. Running Python script..."
# Now run the python script. It will use the python from the activated 'stac' environment.
python "${PYTHON_SCRIPT_PATH}"

echo "--- STAC pipeline script finished at $(date) ---"
