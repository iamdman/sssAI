from fastapi import FastAPI
from PIL import Image, ImageDraw

import requests
import logging
import base64
import time
import json
import pickle
import os
from polygon import *

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%m/%d/%Y %I:%M:%S %p')
Log("INFO",'App Started')
app = FastAPI()

with open('/config/cameras.json') as f:
    cameradata = json.load(f)

with open('/config/settings.json') as f:
    settings = json.load(f)

sssUrl = settings["sssUrl"]
deepstackUrl = settings["deepstackUrl"]
homebridgeWebhookUrl = settings["homebridgeWebhookUrl"]
username = settings["username"]
password = settings["password"]

timeout = 10
if "timeout" in settings:
    timeout = int(settings["timeout"])

if "SSSGetSessionURL" in settings:
    SSSGetSessionURL = settings["SSSGetSessionURL"]
    
# If no trigger interval set then make it 60s (i.e. don't send another event from the triggered camera for at least 60s to stop flooding event notifications
trigger_interval = 60
if "triggerInterval" in settings:
    trigger_interval = settings["triggerInterval"]

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
url = SSSGetSessionURL.format(sssUrl,username,password)

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
        print(entry)        

def contains(rOutside, rInside):
    return rOutside["x_min"] < rInside["x_min"] < rInside["x_max"] < rOutside["x_max"] and \
        rOutside["y_min"] < rInside["y_min"] < rInside["y_max"] < rOutside["y_max"]

# If you would like to ignore objects outside the ignore area instead of inside, set this to contains(rect, ignore_area):
def IsInsideArea(rect, ignore_areas, label):
    for ignore_area in ignore_areas:
        if not contains(ignore_area, rect):
            Log("INFO","'{}' matching {} is NOT within ignore area {}. Triggering...".format(label,rect,ignore_area))
            return False
        Log("DEBUG","'{}' matching {} is within ignore area {}. Not triggering.".format(label,rect,ignore_area))
    return True

@app.get("/{camera_id}")
async def read_item(camera_id):
    start = time.time()
    cameraname = cameradata["{}".format(camera_id)]["name"]
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

    url = settings["SSSGetSnapshotURL"].format(sssUrl, camera_id)
    triggerurl = cameradata["{}".format(camera_id)]["triggerUrl"]
    if "homekitAccId" in cameradata["{}".format(camera_id)]:
        homekit_acc_id = cameradata["{}".format(camera_id)]["homekitAccId"]

    response = requests.request("GET", url, cookies=load_cookies("cookie"))
    logging.debug('Requested snapshot: ' + url)
    if response.status_code == 200:
        with open("/tmp/{}.jpg".format(camera_id), "wb") as f:
            f.write(response.content)
            logging.debug("Snapshot downloaded")

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

    for prediction in response["predictions"]:
        if found: break
        i += 1
        confidence = round(100 * prediction["confidence"])
        label = prediction["label"]
        sizex = int(prediction["x_max"])-int(prediction["x_min"])
        sizey = int(prediction["y_max"])-int(prediction["y_min"])
        p = [(int(prediction["x_max"])+int(prediction["x_min"])) / 2, (int(prediction["y_max"])+int(prediction["y_min"])) / 2]
        Log("DEBUG","Suspected '{}' id={} found ({}%) size {}x{} with center @ {} on {}. Checking if '{}' is in ignore zones...".format(label,i,confidence,sizex,sizey, p, cameradata[camera_id]["name"],label))

        if not label in [item.get("type") for item in list(cameradata[camera_id]["detect_objects"])]:
            Log("DEBUG","Ignoring '{}' as it is not part of camera configuration detect object list.".format(label))
            continue;

        for detect_object in cameradata[camera_id]["detect_objects"]:
            if not found and detect_object["type"] == label:
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

                    Log("DEBUG","Minimum size and confidence passed. Checking if {} object is in ignore zone.".format(label))
                    if not IsInsideArea(prediction, ignore_areas, label):
                        found = True
                    elif not IsInsidePolygons(points = detect_object["ignore_polygons"], p = p, label = label):
                        found = True
                    if found:                                
                        payload = {}
                        response = requests.request("GET", triggerurl, data=payload)
                        end = time.time()
                        runtime = round(end - start, 1)
                        Log("INFO","{}% sure we found a {} - triggering {} - took {} seconds".format(confidence,label,cameraname,runtime))
                        last_trigger[camera_id] = time.time()
                        save_last_trigger(last_trigger)
                        logging.debug("Saving last camera time for {} as {}".format(camera_id,last_trigger[camera_id]))
                        if homebridgeWebhookUrl is not None and homekit_acc_id is not None:
                            hb = requests.get("{}/?accessoryId={}&state=true".format(homebridgeWebhookUrl,homekitAccId))
                            logging.debug("Sent message to homebridge webhook: {}".format(hb.status_code))
                        else:
                            logging.debug("Skipping HomeBridge Webhook since no webhookUrl or accessory Id")
                else:
                    Log("DEBUG","Ignoring '{}' as it did not meet minimum size or confidence setting.".format(label))

    end = time.time()
    runtime = round(end - start, 1)
    if found:
        ignore_polygons_list = [item.get("ignore_polygons") for item in list(cameradata[camera_id]["detect_objects"])]
        save_image(predictions, cameraname, snapshot_file, ignore_areas, ignore_polygons_list)
        return ("Triggering camera because something was found - took {} seconds").format(runtime)
    else:
        Log("INFO","{} not triggered - nothing found - took {} seconds".format(cameraname,runtime))
        return ("{} not triggered - nothing found".format(cameraname))


def save_image(predictions, camera_name, snapshot_file, ignore_areas, ignore_polygons_list):
    start = time.time()
    logging.debug("Saving new image file....")
    im = Image.open(snapshot_file)
    draw = ImageDraw.Draw(im)

    for object in predictions:
        confidence = round(100 * object["confidence"])
        label = "{} ({%)".format(object['label'],confidence)
        draw.rectangle((object["x_min"], object["y_min"], object["x_max"],
                        object["y_max"]), outline=(255, 230, 66), width=2)
        draw.text((object["x_min"]+10, object["y_min"]+10),
                  label, fill=(255, 230, 66))

    for ignore_area in ignore_areas:
        draw.rectangle((ignore_area["x_min"], ignore_area["y_min"],
                        ignore_area["x_max"], ignore_area["y_max"]), outline=(255, 66, 66), width=2)
        draw.text((ignore_area["x_min"]+10, ignore_area["y_min"]+10), "ignore area", fill=(255, 66, 66))

    for ignore_polygons in ignore_polygons_list:
        for ignore_polygon in ignore_polygons:
            print("new poly")
            draw.polygon(ignore_polygon, fill=(255, 66, 66), outline=(255, 66, 66))
            draw.text((ignore_polygon[0][0], ignore_polygon[0][1]), "ignore polygon", fill=(255, 66, 66))

    fn = "{}/{}-{}.jpg".format(capture_dir,camera_name,start)
    im.save(fn, quality=100)
    im.close()
    end = time.time()
    runtime = round(end - start, 1)
    logging.debug("Saved captured and annotated image: {} in {} seconds.".format(fn,runtime))
