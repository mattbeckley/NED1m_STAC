import requests
import json
import subprocess,pdb


"""Name: 
   Description:
   pseudo-code to extract USGS NED 1 m rasters from S3 Bucket.
   Date Created: 03/02/2021

   Workflow:
   1. Based on User Selection Area of Interest (AOI), feed coordinates to USGS
   National Map API (https://tnmaccess.nationalmap.gov/api/v1/products')
   This will return a JSON of all geotiffs that intersect the AOI
   2.  Read the JSON file, and extract the URLs to the geotiff.  This
   could be under TAG: ["downloadURL"], or ["urls"]["TIFF"]
   3.  Reform the URL to be the path to the S3 bucket and prepend all
   files with /vsis3.  Note this can also work with vsicurl, but it is
   probably more stable to access the S3 bucket as opposed to the HTTPS
   4.  Write out a temporary text file that has the URL to all the files
   in the S3 bucket that intersect the AOI 
   5.  Call gdalbuildvrt and build a VRT from the files in the text file
   from step 4.  This will also be a temporary file that can be
   discarded after the job suceeds.
   6.  Call gdal_translate.  This will call the vrt built in step5, and
   we will also feed in the AOI bounds to crop the data to the AOI.

   NOTE:  USGS is constantly changing their API.  To do a simple test,
   you could paste this into the browser to see what the API is
   returning for the "products".  this is for a small section near
   Taho:
https://tnmaccess.nationalmap.gov/api/v1/products?datasets=Digital%20Elevation%20Model%20(DEM)%201%20meter&bbox=-121.081217,39.426568,-120.901098,39.531114&prodFormats=GeoTIFF&outputFormat=JSON


"""


#INPUTS
#----------------------------------------------------------------------
minlon=-74.0
minlat=44.71
maxlon=-73.8
maxlat=44.78

#from this API you can get the name of all the available products:
#https://tnmaccess.nationalmap.gov/api/v1/datasets?code=ned
#For this, we only want the 1 Arc Second dataset
Dataset = 'Digital Elevation Model (DEM) 1 meter'

#Name of final output file:
out_tiff = 'NED_output_1m_UpstateNY.tif'
#----------------------------------------------------------------------


#put bounding box into string format
bbox = (minlon,minlat,maxlon,maxlat)
bbox = str(bbox)
bbox = bbox.strip('()')


#Build call to USGS REST API
url='https://tnmaccess.nationalmap.gov/api/v1/products'
params = dict(datasets=Dataset, bbox=bbox, prodFormats='GeoTIFF',outputFormat='JSON')

#Execute REST API call.  
try:
    r = requests.get(url,params=params)
except:
    print('Error with Service Call')
    pdb.set_trace()

#load API JSON output into a variable
try:
    data = json.loads(r.content)
except:
    print('Error loading JSON output')
    pdb.set_trace()

#for debugging, write json to file, but probably don't need this file
with open('NED_Query.json', 'w') as outfile:
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
outf    = 'AOI_tiff_URLs.txt'
out_obj = open(outf,'w')

for file in URLS:
    out_obj.write(file+'\n')

out_obj.close()

#Call GDALbuildvrt via system call.  Build a temporary VRT from the list
#of files.  This vrt can be discarded after job is completed.
vrt = 'tmp.vrt'
cmd1 = ['time gdalbuildvrt '+vrt+' -input_file_list '+outf]
p1   = subprocess.run(cmd1,shell=True)
#Error check if gdalbuildvrt ran successfully
if (p1.returncode == 1) or (p1.stderr is not None):
    print('Problem building the VRT')
    pdb.set_trace()

#Call gdal_translate to subset the COGs (via the VRT). Use the map AOI to subset the data.

#projwin form: <ulx> <uly> <lrx> <lry>
projwin_box = str(minlon)+' '+str(maxlat)+' '+str(maxlon)+' '+str(minlat)
cmd2 = ['time gdal_translate '+vrt+' -projwin '+projwin_box+' -projwin_srs EPSG:4326 -of GTiff -co COMPRESS=deflate -of GTIFF -co "TILED=YES" -co "blockxsize=512" -co "blockysize=512" '+out_tiff]
p2 = subprocess.run(cmd2,shell=True)
#Error check if gdal_translate ran successfully
if (p2.returncode == 1) or (p2.stderr is not None):
    print('Problem running gdal_translate')
    pdb.set_trace()

    
#remove temporary files
#os.remove('tmp.vrt')
#os.remove(''AOI_tiff_URLs.txt')
