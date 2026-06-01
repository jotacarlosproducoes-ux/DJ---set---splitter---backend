from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import uuid
import os
import shutil
import subprocess
import requests
import hmac
import hashlib
import base64
import time

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ACR_HOST = "identify-us-west-2.acrcloud.com"
ACR_KEY = "da746b8377796097a8b57b1cb4fe8a5c"
ACR_SECRET = "Qxt4orcUoVSgkZPP4vfqdYGf4Vl3Au5j0SKRddl"

jobs = {}

def identify_song(audio_path, timestamp):
    try:
        url = "https://identify-us-west-2.acrcloud.com/v1/identify"
        ts = str(int(time.time()))
        string_to_sign = "POST\n/v1/identify\n" + ACR_KEY + "\naudio\n1\n" + ts
        sign = base64.b64encode(
            hmac.new(ACR_SECRET.encode(), string_to_sign.encode(), hashlib.sha1).digest()
        ).decode()
        with open(audio_path, "rb") as f:
            files = [("sample", ("sample.mp3", f, "audio/mpeg"))]
            data = {
                "access_key": ACR_KEY,
                "sample_bytes": os.path.getsize(audio_path),
                "timestamp": ts,
                "signature": sign,
                "data_type": "audio",
                "signature_version": "1",
            }
            response = requests.post(url, files=files, data=data, timeout=10)
            result = response.json()
        if result.get("status", {}).get("code") == 0:
            music = result["metadata"]["music"][0]
            title = music.get("title", "Desconhecida")
            artist = music.get("artists", [{}])[0].get("name", "Desconhecido")
            return {"title": title, "artist": artist}
    except Exception:
        pass
    return {"title": "Faixa " + str(timestamp) + "s", "artist": "Desconhecido"}

@app.get("/")
def root():
    return {"status": "DJ Set Splitter API online"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/split")
async def split_audio(file: UploadFile = File(...)):
    job_id = str(uuid.uuid4())
    folder = "outputs/" + job_id
    os.makedirs(folder, exist_ok=True)
    input_path = folder + "/input.mp3"
    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    output_pattern = folder + "/track_%03d.mp3"
    subprocess.run([
        "ffmpeg", "-i", input_path,
        "-f", "segment",
        "-segment_time", "180",
        "-vn",
        "-acodec", "mp3",
        "-ab", "192k",
        "-ar", "44100",
        "-y",
        output_pattern
    ], check=True)
    tracks = []
    fnames = sorted(os.listdir(folder))
    i = 0
    for fname in fnames:
        if fname.startswith("track_") and fname.endswith(".mp3"):
            track_path = folder + "/" + fname
            timestamp = i * 180
            info = identify_song(track_path, timestamp)
            tracks.append({
                "name": info["artist"] + " - " + info["title"],
                "url": "/download/" + job_id + "/" + fname,
                "artist": info["artist"],
                "title": info["title"],
                "timestamp": timestamp
            })
            i += 1
    jobs[job_id] = {"status": "done", "tracks": tracks}
    return {"job_id": job_id, "status": "done", "tracks": tracks}

@app.get("/status/{job_id}")
def get_status(job_id):
    if job_id not in jobs:
        return {"error": "Job nao encontrado"}
    return jobs[job_id]

@app.get("/download/{job_id}/{filename}")
def download_track(job_id, filename):
    path = "outputs/" + job_id + "/" + filename
    if not os.path.exists(path):
        return {"error": "Arquivo nao encontrado"}
    return FileResponse(path, media_type="audio/mpeg", filename=filename)
