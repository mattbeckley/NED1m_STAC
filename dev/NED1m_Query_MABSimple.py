import pystac
from rtree import index
import os,subprocess,pdb
import pickle
from shapely.geometry import Polygon
from datetime import datetime
import requests
import json

'''
MAB 06/03/2025.  This is a simplified version of code to extract the
NED1m data.  The code first tries the "old" method that accesses the
USSG Product API.  If that fails, the code then accessess the local
STAC catalog that I created.  I've timed the execution of bothe
methods, and they are similar.

'''


#Path to GDAL translate routine
gdal_translate = "/home/beckley/miniconda3/envs/workGDAL3ten/bin/gdal_translate"
#Path to GDAL buildvrt
gdal_buildvrt = "/home/beckley/miniconda3/envs/workGDAL3ten/bin/gdalbuildvrt"

# Local STAC Search Method----------------------------------------------
def Local_STAC(minlon,minlat,maxlon,maxlat,outdir,outtif,outURLs,vrt,debug=False):
    print("Running Local STAC search...")
    
    #Name of STAC catalog
    catalog_path = "/data/matt/NED1m_STAC/catalog.json"
    #Name of RTree Spatial Index in same dir as STAC catalog
    IndexName    = "stac_spatial_index"

    #AOI to search
    search_bbox = [minlon, minlat,maxlon, maxlat]

    query_start = datetime.now()
    #Get listing of all intersecting COGs.  This replaces the USGS Product API step.
    geotiff_urls = find_files_local_indexed(catalog_path, search_bbox,IndexName)
    query_end = datetime.now() 

    if geotiff_urls:
        print("Intersecting GeoTIFF URLs:")
        for url in geotiff_urls:
            print(url)
    else:
        print("No intersecting GeoTIFF URLs found.")

    #extract the geotiff download URLS..
    URLS = []
    for f in geotiff_urls:
        #replace http path with S3 path and /vsis3 interface
        download_url = f.replace('https://prd-tnm.s3.amazonaws.com','/vsis3/prd-tnm')
        URLS.append(download_url)


    #write URLs to a file--------------------------------------------------
    out_obj = open(outURLs,'w')
    for file in URLS:
        out_obj.write(file+'\n')

    out_obj.close()
    #----------------------------------------------------------------------
    
    #Call GDALbuildvrt via system call.  Build a temporary VRT from the list
    #of files.  This vrt can be discarded after job is completed.
    vrt_start = datetime.now() 
    cmd1 = [gdal_buildvrt+' '+vrt+' -input_file_list '+outURLs]
    p1   = subprocess.run(cmd1,shell=True)
    #Error check if gdalbuildvrt ran successfully
    if (p1.returncode == 1) or (p1.stderr is not None):
        print('Problem building the VRT')
        pdb.set_trace()
    vrt_end = datetime.now()
    
    #Call gdal_translate to subset the COGs (via the VRT). Use the map AOI to subset the data.

    Translate_start = datetime.now()     
    #projwin form: <ulx> <uly> <lrx> <lry>
    projwin_box = str(minlon)+' '+str(maxlat)+' '+str(maxlon)+' '+str(minlat)
    #Don't write out a COG - it takes MUCH longer than standard, compressed geotiff
    cmd2 = [gdal_translate+' '+vrt+' -projwin '+projwin_box+' -projwin_srs EPSG:4326 -of GTIFF -co COMPRESS=deflate  -co "TILED=YES" -co "blockxsize=512" -co "blockysize=512" '+outtif]
    p2 = subprocess.run(cmd2,shell=True)
    #Error check if gdal_translate ran successfully
    if (p2.returncode == 1) or (p2.stderr is not None):
        print('Problem running gdal_translate')
        pdb.set_trace()
    Translate_end = datetime.now()     

    #get time of execution....
    print('Time to do STAC Query: {}'.format(query_end - query_start))
    print('Time to create the VRT: {}'.format(vrt_end - vrt_start))
    print('Time to subset COG and create final output: {}'.format(Translate_end - Translate_start))     
    
# End of Local STAC Search Method---------------------------------------

# USGS Product API Search Method----------------------------------------
def USGS_API(minlon,minlat,maxlon,maxlat,outdir,outtif,outURLs,vrt,debug=False):
    #Constants
    #----------------------------------------------------------------------
    #from this API you can get the name of all the available products:
    #https://tnmaccess.nationalmap.gov/api/v1/datasets?code=ned
    #For this, we only want the 1 Arc Second dataset
    Dataset = 'Digital Elevation Model (DEM) 1 meter'

    url = 'https://tnmaccess.nationalmap.gov/api/v1/products'
    #----------------------------------------------------------------------

    print("Running USGS Product API search...")

    #put bounding box into string format
    bbox = (minlon,minlat,maxlon,maxlat)
    bbox = str(bbox)
    bbox = bbox.strip('()')


    
    #Build call to USGS REST API
    params = dict(datasets=Dataset, bbox=bbox, prodFormats='GeoTIFF',outputFormat='JSON',max=1000)

    USGSAPI_start = datetime.now() 
    #Execute REST API call.  
    try:
        r = requests.get(url,params=params)
    except:
        print('Error with Service Call to USGS Product API:\n'+url)
        return
    USGSAPI_end = datetime.now()
    
    #load API JSON output into a variable
    try:
        data = json.loads(r.content)
    except:
        print('Error loading JSON output from USGS Product API')
        pdb.set_trace()
        #return

    #for debugging only, write json to file, but probably don't need this file
    if debug:
        out_json = os.path.join(outdir,'NED1mQuery.json')
        with open(out_json, 'w') as outfile:
            json.dump(data, outfile)

    #extract just the records from the JSON
    items = data['items']

    #extract the geotiff download URLS..
    URLS = []
    for f in items:
        #replace http path with S3 path and /vsis3 interface
        download_url = f["urls"]["TIFF"].replace('https://prd-tnm.s3.amazonaws.com','/vsis3/prd-tnm')
        URLS.append(download_url)


    #write URLs to a file
    out_obj = open(outURLs,'w')

    for file in URLS:
        out_obj.write(file+'\n')

    out_obj.close()

    #Call GDALbuildvrt via system call.  Build a temporary VRT from the list
    #of files.  This vrt can be discarded after job is completed.
    Ovrt_start = datetime.now() 
    cmd1 = [gdal_buildvrt+' '+vrt+' -input_file_list '+outURLs]
    p1   = subprocess.run(cmd1,shell=True)
    #Error check if gdalbuildvrt ran successfully
    if (p1.returncode == 1) or (p1.stderr is not None):
        print('Problem building the VRT')
        pdb.set_trace()
    Ovrt_end = datetime.now()
    
    #Call gdal_translate to subset the COGs (via the VRT). Use the map AOI to subset the data.
    OTranslate_start = datetime.now()     
    #projwin form: <ulx> <uly> <lrx> <lry>
    projwin_box = str(minlon)+' '+str(maxlat)+' '+str(maxlon)+' '+str(minlat)
    cmd2 = [gdal_translate+' '+vrt+' -projwin '+projwin_box+' -projwin_srs EPSG:4326 -co COMPRESS=deflate -of GTIFF -co "TILED=YES" -co "blockxsize=512" -co "blockysize=512" '+outtif]
    p2 = subprocess.run(cmd2,shell=True)
    OTranslate_end = datetime.now()     
    #Error check if gdal_translate ran successfully
    if (p2.returncode == 1) or (p2.stderr is not None):
        print('Problem running gdal_translate')
        pdb.set_trace()

    #get time of execution using USGS product API method...
    print('Time to do USGS Product API Query: {}'.format(USGSAPI_end - USGSAPI_start))
    print('Time to create the VRT: {}'.format(Ovrt_end - Ovrt_start))
    print('Time to subset COG and create final output: {}'.format(OTranslate_end - OTranslate_start))     

# End of USGS Product API Search Method----------------------------------------        

def find_files_local_indexed(catalog_path, search_bbox,IndexName):
    try:
        index_dir = os.path.dirname(catalog_path)  # Location of the index files
        idx = index.Index(os.path.join(index_dir, IndexName), read_only=True)
        with open(os.path.join(index_dir, IndexName+'.pkl'), 'rb') as f:
            item_id_to_relative_path = pickle.load(f)  # Load relative paths

        intersecting_urls = []
        search_polygon = Polygon([(search_bbox[0], search_bbox[1]), (search_bbox[2], search_bbox[1]),
                                  (search_bbox[2], search_bbox[3]), (search_bbox[0], search_bbox[3])])

        for hit in idx.intersection(search_bbox, objects=True):
            item_id = hit.object
            relative_path = item_id_to_relative_path[item_id]
            item_path_abs = os.path.join(os.path.dirname(catalog_path), relative_path)  # Join with current catalog location
            try:
                item = pystac.read_file(item_path_abs)
                item_bbox = item.bbox
                if item_bbox:
                    item_polygon = Polygon([(item_bbox[0], item_bbox[1]), (item_bbox[2], item_bbox[1]),
                                             (item_bbox[2], item_bbox[3]), (item_bbox[0], item_bbox[3])])
                    if item_polygon.intersects(search_polygon):
                        asset = item.assets.get("elevation-geotiff")
                        if asset and asset.href.lower().endswith(".tif"):
                            if not asset.href.startswith("http"):
                                base_dir_abs = os.path.dirname(os.path.abspath(item_path_abs))
                                asset_href_relative = asset.href
                                asset_href_abs = os.path.normpath(os.path.join(base_dir_abs, asset_href_relative))
                                intersecting_urls.append(asset_href_abs)
                            else:
                                intersecting_urls.append(asset.href)
            except Exception as e:
                print(f"Error reading item {item_id} from {item_path_abs}: {e}")

        return intersecting_urls

    except Exception as e:
        print(f"Error during query: {e}")
        return None
    
    
if __name__ == "__main__":
    #INPUTS-------------------------------------------------------------
    #AOI
    minlon = -74.00
    minlat = 44.705843
    maxlon = -73.8
    maxlat = 44.782501

    #output DTM Path
    outdir  = '/data/matt/testing/STAC1m'
    outfile = "output_simple_Jun03.tif"
    outtif  = os.path.join(outdir,outfile)

    #text file that contains URLs to all the interecting tiff files
    #This file can be removed after code completion
    AOIfile = 'AOI_tiff_URLs_simple_Jun03.txt'
    outURLs = os.path.join(outdir,AOIfile) 

    #Name of temporary VRT. This file can be removed after code completion
    vrt = os.path.join(outdir,'tmp_simple_Jun03.vrt')
    #End INPUTS---------------------------------------------------------

    #try the USGS Product API method first.  If that doesn't work, use the local STAC catalog method 
    try: 
        USGS_API(minlon,minlat,maxlon,maxlat,outdir,outtif,outURLs,vrt,debug=False)
    except:
        Local_STAC(minlon,minlat,maxlon,maxlat,outdir,outtif,outURLs,vrt,debug=False)
