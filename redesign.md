## Background / Current State                                                                                                         
The code, run_stac_pipeline.py, builds and updates a STAC catalog based on the xml files from the USGS 3DEP 1m files located here:
https://prd-tnm.s3.amazonaws.com/index.html?prefix=StagedProducts/Elevation/1m/Projects/

The main thing the STAC needs is the URLs to the actual tif files, and that is within those xml files.  So, that code harvests all that info, so that I know where to find the URLs to a set of tifs given a certain area on interest (i.e. latitude/longitude input box).  The reason I had to build this STAC is because the previous method to get these URLs was using the USGS Product API: https://apps.nationalmap.gov/tnmaccess/#/product  This API get the same URLs, but unfortunately this service is very often down as seen by the uptime reports 
for ScienceBase: https://stats.uptimerobot.com/gxzRZFARLZ/783928857

This 1m data is critical to our operations - it is a foundational data product for our academic users as well as our paying customers.  In 2025 there was a scare where we thought the data could disappear as part of the government funding issues.  So, we have since acquired space on the open space network (OSN).  We now sync the bucket s3://prd-tnm/StagedProducts/Elevation/1m/ to our OSN storage here:
OSN Bucket Endpoint: https://usgs.osn.mghpcc.org
OSN Bucket Name: ot-usgs-osn



## Problem / Motivation
We have the code, NED1m_Query.py, which first checks the USGS Product API (https://apps.nationalmap.gov/tnmaccess/#/product) to get the tif URLS for a given input AOI.  If it fails, which it often does, it then tries to get the tif URLs from my localSTAC which was built from run_stac_pipeline.py.  Since the USGS Product API is so unstable, and we now have a local copy of the actual data (in the OSN bucket) we can probably not use the USGS Product API at all.

So, the code needs to be refactored to first check the local STAC.  If this fails for some reason, then the code should then use our local backup on OSN.  There are some points that need to be made here for clarification.  The local STAC I refer to is simply a metadata catalog that points to the USGS URLs which almost exlusively reside in s3://prd-tnm/StagedProducts/Elevation/1m/ although there is some metadata in the STAC that also points to URLs in:
https://rockyweb.usgs.gov/vdelivery/Datasets/Staged/  There was an issue a couple of months ago where the bucket, s3://prd-tnm/StagedProducts/Elevation/1m/, was deleted by accident.  In this type of scenario, I would like the code to check our local STAC...that part will probably work fine, but the URLs would all point to a bucket that is either down or has issue, and in this scenario, it should fail-over and get the data from our OSN storage which has a local copy of not only the metadata, but the actual tif files.

The complication, is that the OSN data is a mirror of the USGS s3 bucket, so all the metadata in the OSN bucket is pointing to USGS S3 resources.  So, I need to append our local STAC to add an "alternative URL" or some equivalent named parameter that contains links to the equivalent resources in our OSN storage.  In this way, when I feed it an AOI, it can find all the intersecting tif URLs that reside in OSN.  I'm not sure if STAC can have multiple URLs for a given resource, or what the best way to organize this is.

In general, I expect there to not be many problems accessing the local STAC since this will be on OpenTopography-controlled resources.  If there is a problem, the STAC probably won't be able to be reached at all.  Where I anticpate an issue, is with the USGS resources.  The usgs bucket could be down, or they renamed resources, etc.  So, if the code cannot find the resources that the STAC is reporting, the code should fail over to getting the URLs and ultimately the tif files from the OSN bucket.

## Goals                                                                           

1.  Remove the need to use USGS Product API to get tif URLs.  It is too unstable.
2.  Adjust the local STAC, and for each collection item, and a URL to the equivalent tif file that resides in OSN.  In this way, each item in teh STAC should have two URLS...one pointing to the  s3://prd-tnm/StagedProducts/Elevation/1m/ bucket and another pointing to the https://usgs.osn.mghpcc.org bucket.
3. Adjust the NED1m_Query.py code so that by default it uses the local STAC (which is currently working as the failover).  The new failover workflow will be to get the data from OSN bucket.  
4.  I would like input parameters to be able to force using one method over the other.  The current NED1m_Query.py has an option called, --force-local-stac.  This could be modified to be either two parameters: --force-local-stac and --force_OSN or one paramter that accepts different options...whichever makes more sense.  I need this to be able to test if both work, but also if there isn an issue with one method in the future, it could be useful to force the code to use one method over the other.  
5.  The code currently has good error checking and logging.  I would want to retain that, and make sure there is accurate logging to see where the actual data is coming from (i.e. from USGS S# resource or OSN, for example)  I also want to maintain timing of operations to be able to track where there are bottlenecks.  I have not worked with the OSN resources before, so I'm not sure how performant they are.
                                                                                                                  
## Open Questions 
1.  Is it adviseable to adjust the existing local STAC to add an alternate URL?  Is this even possible?  Is it best practice? Or would it make more sense to build a second local STAC that is just for the OSN resources?  It seems like having a single STAC is more manageable, but I am not sure