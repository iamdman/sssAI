from typing import Optional
from fastapi import FastAPI
from PIL import Image, ImageDraw

import requests
import logging
import base64
import time
import json
import pickle
import sys
import os
from polygon import *
from sendmail import *

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%m/%d/%Y %I:%M:%S %p')
Log("INFO",'App Started')
app = FastAPI()

with open('/config/cameras.json') as f:
    cameradata = json.load(f)

with open('/config/settings.json') as f:
    settings = json.load(f)

SSSUrl = settings["SSSUrl"]
deepstackUrl = settings["deepstackUrl"]
homebridgeWebhookUrl = settings["homebridgeWebhookUrl"]
SSSUsername = settings["SSSUsername"]
SSSPassword = settings["SSSPassword"]
EmailSenderAddress = settings["EmailSenderAddress"]
EmailReceiverAddress = settings["EmailReceiverAddress"]
EmailSmtpHost = settings["EmailSmtpHost"]
EmailSmtpPort = settings["EmailSmtpPort"]
EmailPassword = settings["EmailPassword"]

timeout = 10
if "timeout" in settings:
    timeout = int(settings["timeout"])

if "SSSGetSessionURL" in settings:
    SSSGetSessionURL = settings["SSSGetSessionURL"]
    
# If no trigger interval set then make it 60s (i.e. don't send another event from the triggered camera for at least 60s to stop flooding event notifications
trigger_interval = 60
if "triggerInterval" in settings:
    trigger_interval = settings["triggerInterval"]
    
# if set to .5 the center of a detected deepstack object will be used to determine if the object is withing a polygon
# if set to a decimal number between such as .05 then 5% from the bottom of the detected object will be used instead
polygon_deepstack_bottom_offset = 0.5
if "polygon_deepstack_bottom_offset" in settings:
    polygon_deepstack_bottom_offset = settings["polygon_deepstack_bottom_offset"]    

capture_dir = "/captureDir"
if "captureDir" in settings:
    capture_dir = settings["captureDir"]

def save_cookies(requests_cookiejar, filename):
    with open(filename, 'wb') as f:
        pickle.dump(requests_cookiejar, f)

def load_cookies(filename):
    with open(filename, 'rb') as f:
        return pickle.load(f)

# Create a session with synology
url = SSSGetSessionURL.format(SSSUrl,SSSUsername,SSSPassword)

#  Save cookies
Log("INFO",'Session login: ' + url)
r = requests.get(url)
save_cookies(r.cookies, 'cookie')

# Dictionary to save last trigger times for camera to stop flooding the capability
last_trigger_fn = "/tmp/last.dict"

def save_last_trigger(last_trigger):
    with open(last_trigger_fn, 'wb') as f:
        pickle.dump(last_trigger, f)

def load_last_trigger():
    if os.path.exists(last_trigger_fn):
        with open(last_trigger_fn, 'rb') as f:
            return pickle.load(f)
    else:
        return {}
        
def Log(level, entry):
    if level == "DEBUG":
        logging.debug(entry)
    elif level == "ERROR":
        logging.error(entry)
    else:
        logging.info(entry)
        
def contains(rOutside, rInside):
    return rOutside["x_min"] < rInside["x_min"] < rInside["x_max"] < rOutside["x_max"] and \
        rOutside["y_min"] < rInside["y_min"] < rInside["y_max"] < rOutside["y_max"]

def IsInsidePolygons(points:list, p:tuple, label, confidence) -> bool:
    i = 0
    count = len(points)
    if count == 0: 
        return True
    else:
        Log("DEBUG","We have at least {} ignore polygon(s) to check, if {} is not in any of these zones then trigger will be initiated.".format(len(points),label))
        hit = []
        for polygon in points:
            i=i+1
            Log("DEBUG","{}. Checking polygon {}...".format(i,polygon))
            if not IsInsidePolygon(polygon, p, label):
                hit = polygon
            else:
                Log("DEBUG","'{}' ({}%) with center @ {} is within ignore polygon {}".format(label,confidence,p,polygon))
                return True
        Log("DEBUG","'{}' ({}%) with center @ {} is not within ignore polygon {}".format(label,confidence,p,polygon))
        return False
        
# If you would like to ignore objects outside the ignore area instead of inside, set this to contains(rect, ignore_area):
def IsInsideAreas(rect, ignore_areas, label, confidence):
    i = 0
    count = len(ignore_areas)
    if count == 0: 
        return True
    else:
        hit = []
        Log("DEBUG","We have at least {} ignore areas(s) to check, if {} is not in any of these zones then trigger will be initiated.".format(count,label))
        for ignore_area in ignore_areas:
            i+=1
            Log("DEBUG","{}. Checking area {}...".format(i,ignore_area))
            if not contains(ignore_area, rect):
                hit = ignore_area
            else:
                Log("DEBUG","'{}' ({}%) matching {} is within ignore area {}".format(label,confidence,rect,ignore_area))
                return True
        Log("DEBUG","'{}' ({}%) matching {} is NOT within ignore area {}".format(label,confidence,rect,ignore_area))
        return False

def CheckZones(prediction, ignore_areas, label, confidence, points, p)-> bool:
    if not IsInsideAreas(prediction, ignore_areas, label, confidence):
        return True
    elif not IsInsidePolygons(points = points, p = p, label = label, confidence = confidence):
        return True
    return False
    
@app.get("/{camera_id}")
async def read_item(camera_id, debug: Optional[str] = None):
    start = time.time()
    cameraname = cameradata["{}".format(camera_id)]["name"]
    Log("INFO","***Call started for {} (camera_id={} and debug={})".format(cameraname,camera_id,debug))
    predictions = None
    last_trigger = load_last_trigger()

    # Check we are outside the trigger interval for this camera
    if camera_id in last_trigger:
        t = last_trigger[camera_id]
        Log("INFO","Found last camera time for {} was {}".format(camera_id,t))
        if (start - t) < trigger_interval:
            msg = "Skipping detection on camera {} since it was only triggered {}s ago".format(camera_id,(start-t))
            Log("INFO",msg)
            return (msg)
        else:
            Log("INFO","Processing event on camera (last trigger was {}s ago)".format(start-t))
    else:
        Log("INFO","No last camera time for {}".format(camera_id))

    url = settings["SSSGetSnapshotURL"].format(SSSUrl, camera_id)
    triggerurl = cameradata["{}".format(camera_id)]["triggerUrl"]
    if "homekitAccId" in cameradata["{}".format(camera_id)]:
        homekit_acc_id = cameradata["{}".format(camera_id)]["homekitAccId"]

    response = requests.request("GET", url, cookies=load_cookies("cookie"))
    Log("DEBUG",'Requested snapshot: ' + url)
    if response.status_code == 200:
        with open("/tmp/{}.jpg".format(camera_id), "wb") as f:
            f.write(response.content)
            Log("DEBUG","Snapshot downloaded")

    snapshot_file = "/tmp/{}.jpg".format(camera_id)
    image_data = open(snapshot_file, "rb").read()
    Log("INFO","Requesting detection from DeepStack...")
    s = time.perf_counter()
    response = requests.post("{}/v1/vision/detection".format(deepstackUrl), files={"image": image_data}, timeout=timeout).json()

    e = time.perf_counter()
    Log("DEBUG","Got result: {}. Time: {}s".format(json.dumps(response, indent=2), e-s))
    if not response["success"]:
        return ("Error calling Deepstack: " + response["error"])

    labels = ""
    predictions = response["predictions"]
    for object in predictions:
        label = object["label"]
        if label != 'person':
            labels = labels + label + " "

    i = 0
    found = False
    founditems = []
    ignore_areas = []
	
    for prediction in response["predictions"]:
        if found: break
        i += 1
        confidence = round(100 * prediction["confidence"])
        label = prediction["label"]
        sizex = int(prediction["x_max"])-int(prediction["x_min"])
        sizey = int(prediction["y_max"])-int(prediction["y_min"])
        p = [(int(prediction["x_max"])+int(prediction["x_min"])) / 2, int((int(prediction["y_min"]) + (int(prediction["y_max"]) - int(prediction["y_min"])) * (1 - polygon_deepstack_bottom_offset))) ]
        Log("DEBUG","Suspected '{}' id={} found ({}%) size {}x{} with center @ {} on {}. Checking if '{}' is in ignore zones...".format(label,i,confidence,sizex,sizey, p, cameradata[camera_id]["name"],label))

        if not label in [item.get("type") for item in list(cameradata[camera_id]["detect_objects"])]:
            Log("DEBUG","Ignoring '{}' as it is not part of camera configuration detect object list.".format(label))
            continue;

        for detect_object in cameradata[camera_id]["detect_objects"]:
            if not found and \
                detect_object["type"] == label:
                    min_sizex = detect_object["min_sizex"]
                    min_sizey = detect_object["min_sizey"]
                    min_confidence = detect_object["min_confidence"]
                    ignore_areas = []
                    if "ignore_areas" in detect_object:
                        for ignore_area in detect_object["ignore_areas"]:
                            ignore_areas.append({
                                "y_min": int(ignore_area["y_min"]),
                                "x_min": int(ignore_area["x_min"]),
                                "y_max": int(ignore_area["y_max"]),
                                "x_max": int(ignore_area["x_max"])
                            })
                    if not found and \
                       sizex > min_sizex and \
                       sizey > min_sizey and \
                       confidence > min_confidence:

                        Log("DEBUG","Minimum size and confidence haved passed for {} id={}...".format(label,i))
                        found = CheckZones(prediction, ignore_areas, label, confidence, detect_object["ignore_polygons"], p)

                        if found:
                            founditems.append(label)
                            payload = {}
                            response = requests.request("GET", triggerurl, data=payload)
                            end = time.time()
                            runtime = round(end - start, 1)
                            Log("INFO","{}% sure we found a {} - triggering {} - took {} seconds".format(confidence,label,cameraname,runtime))
                            last_trigger[camera_id] = time.time()
                            save_last_trigger(last_trigger)
                            Log("DEBUG","Saving last camera time for {} as {}".format(camera_id,last_trigger[camera_id]))
                            if homebridgeWebhookUrl is not None and homekit_acc_id is not None:
                                hb = requests.get("{}/?accessoryId={}&state=true".format(homebridgeWebhookUrl,homekitAccId))
                                Log("DEBUG","Sent message to homebridge webhook: {}".format(hb.status_code))
                            else:
                                Log("DEBUG","Skipping HomeBridge Webhook since no webhookUrl or accessory Id")
                        else:
                            Log("DEBUG","Ignoring '{}' id={} as it was in an ignore zone.".format(label,i))
                            
                    else:
                        Log("DEBUG","Ignoring '{}' id={} as it did not meet minimum size or confidence setting.".format(label,i))

    end = time.time()
    runtime = round(end - start, 1)
    start = time.time()
    fn = "{}/{}-{}.jpg".format(capture_dir,cameraname,start)
    
    #test assistance, force found
    if str(debug) == "99":
        founditems = ['Test Request']
        found=True
        fn = "{}/{}-{}.jpg".format(capture_dir,"TestOnly",start)
        Log("INFO","Debug Mode On = {} - This trigger was manually invoked".format(debug))

    founditems = ' '.join(map(str, founditems))
    ignore_polygons_list = [item.get("ignore_polygons") for item in list(cameradata[camera_id]["detect_objects"])]

    if found:
        try:
           send_email(cameraname, founditems, snapshot_file, fn)
           save_image(predictions, cameraname, snapshot_file, ignore_areas, ignore_polygons_list, fn, p)
           Log("INFO","***Call completed for {} (camera_id={} image_name={} and debug={})".format(cameraname,camera_id,fn,debug))
           return ("Triggering camera because {} was found - took {} seconds".format(founditems,runtime))
        except Exception as e:
           Log("ERROR","Error: {}".format(e))
           Log("INFO","***Call completed for {} (camera_id={} and debug={})".format(cameraname,camera_id,debug))
           return (e)
    else:
    	#Uncomment below to debug issues when notifications are not being sent
        #fn = "{}/{}-{}.jpg".format(capture_dir,"NOTHING_{}".format(cameraname),start)
        #save_image(predictions, cameraname, snapshot_file, ignore_areas, ignore_polygons_list, fn, p)
        Log("INFO","{} not triggered - nothing found - took {} seconds".format(cameraname,runtime))
        Log("INFO","***Call completed for {} (camera_id={} and debug={})".format(cameraname,camera_id,debug))
        return ("{} not triggered - nothing found".format(cameraname))
    


def save_image(predictions, camera_name, snapshot_file, ignore_areas, ignore_polygons_list, fn, p):
    try:	
       start = time.time()
       im = Image.open(snapshot_file)
       draw = ImageDraw.Draw(im, "RGBA")
       tint_color = (0, 0, 0)  # Black
       transparency = .25  # Degree of transparency, 0-100%
       opacity = int(255 * transparency)
   
       for ignore_area in ignore_areas:
           draw.rectangle((ignore_area["x_min"], ignore_area["y_min"],
                           ignore_area["x_max"], ignore_area["y_max"]), outline=(255, 66, 66), fill=(255, 66, 66, 127))
           draw.text((ignore_area["x_min"]+10, ignore_area["y_min"]+10), "ignore area", fill=(255, 66, 66, 255))

       for ignore_polygons in ignore_polygons_list:
           for ignore_polygon in ignore_polygons:
               poly_tuple = list(map(tuple,ignore_polygon))
               draw.polygon(poly_tuple, fill=(255, 66, 66, 127), outline=(255, 66, 66))
               draw.text([(ignore_polygon[0][0]+10, ignore_polygon[0][1]+10)], "ignore polygon", fill=(255, 255, 255, 255))

       for object in predictions:
           confidence = round(100 * object["confidence"])
           label = "{} ({}%)".format(object['label'], confidence)
           draw.rectangle((object["x_min"], object["y_min"], object["x_max"], object["y_max"]), outline=(255, 230, 66), width=2)
           draw.text((object["x_min"]+10, object["y_min"]+10), label, fill=(255, 230, 66, 255))
       
       #Draw a circle where detection was found.    
       X, Y = p
       r = 9
       draw.ellipse([(X-r, Y-r), (X+r, Y+r)], fill=(0, 255, 0, 127), outline=(0, 255, 0))
         
       im.save(fn, quality=100)
       im.close()
       end = time.time()
       runtime = round(end - start, 1)
       Log("DEBUG","Saved captured and annotated image: {} in {} seconds.".format(fn,runtime))
    except Exception as e:
       Log("ERROR","Error: {}".format(e))
       
    
def send_email(camera_name, founditems, filename, captured_image):
	# Add body to email
	subject = "Alert: A {} was found on {}".format(founditems, camera_name)

	html = """\
	<html>
	  <body>
	    <p><div><div><b>Please check your DS CAM app for the corresponding 
	    video.</b><br></div><div><b><br></b></div><div><b>Photo is attached.</b>
	    <br/><br/><b>Log into the server to see the analyzed image here: {}</b>
		</div><div><br></div></div>
	    </p>
	  </body>
	</html>
	""".format(captured_image)

	try:		 
		sendmail(EmailSenderAddress, EmailReceiverAddress, EmailSmtpHost, EmailSmtpPort, EmailPassword, subject, html, filename)
	except Exception as e:
		Log("ERROR","Error: {}".format(e))
