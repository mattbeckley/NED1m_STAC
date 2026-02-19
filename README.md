# NED1m_STAC
Create, Update, and Index a STAC catalog of USGS 1m COGs

# Code Description
- run_stac_pipeline.py. Creates or updates the STAC catalog. Updates take
  a bit of time, but they are still faster than having to recreate the
  catalog from scratch everytime.  If a new create or updates have been
  made, it runs the r-index to speed up the queries.
- NED1m_Query.py.  This code subsets the USGS 1m catalog and creates a
  mosaic.  It takes the min/max lat/lon as command line parameters.
  There is also the option to enter an output directory, and some other
  parameters.  There is a help, which is useful.  This code tries to
  access the USGS Product API, but if it fails, then it fails over and
  tries the STAC catalog.  There is also an option to force it to ONLY
  use the STAC catalog.
- NED1m_Query_Testing.py.  This is for my own testing.  This will
  basically do what NED1m_Query_forPROD.py does, but with some other
  testing options.  It can test only the USGS method, only the STAC
  method, or both.
- NED1m_STAC_URLExtractor.py  This hasn't been run yet, but the code
  is supposed to loop through the entire STAC catalog and output the
  URLs to the geotiffs to a text file.  This would be used to
  troubleshoot if some of the URLs are incorrect.  Haven't tried it yet.
- RasterNED1mService.py.  This is the version of code that is on production.  
  This code is from: 
  https://github.com/OpenTopography/Algorithms/blob/main/RasterNED1mService.py
  last updated on 02/19/2026.  Note there are slight differences between this 
  code and my local version.  Mainly paths, and how to do error logging.
  