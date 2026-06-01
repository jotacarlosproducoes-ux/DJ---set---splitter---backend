from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import uuid, os, shutil, subprocess, requests, hmac, hashlib, base64, time

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

def identify_song(audio_path: str, timestamp: int):
    url = f"https://{ACR_HOST}/v1/identify"
    http_method = "POST"
    http_uri = "/v1/identify"
    data_type = "audio"
    signature_version = "1"
    ts = str(int(time.time()))
    string_to_sign = "\n".join([http_method, http_uri, ACR_KEY, data_type, signature_version, ts])
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
            "data_type": data_type,
            "signature_version": signature_version,
        }
        response = requests.post(url, files=files, data=data)
        result = response.json()

    if result.get("status", {}).get("code") == 0:
        music = result["metadata"]["music"][0]
        return {
            "title": music.get("title", "Desconhecida"),
            "artist": music.get("artists", [{}])[0].get("name", "Desconhecido"),
            "timestamp": timestamp
        }
    return {"title": f"Faixa_{timestamp}s", "artist": "Desconhecido", "timestamp": timestamp}

@app.get("/")
def root():
    return {"status": "DJ Set Splitter API online"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/split")
async def split_audio(file: UploadFile = File(...)):
    job_id = str(uuid.uuid4())
    os.makedirs(f"outputs/{job_id}", exist_ok=True)

    input_path = f"outputs/{job_id}/input.mp3"
    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    output_pattern = f"outputs/{job_id}/track_%03d.mp3"
    subprocess.run([
        "ffmpeg", "-i", input_path,
        "-f", "segment", "-segment_time", "180",
        "-vn", "-acodec", "libmp3lame",
        "-ab", "192k", "-ar", "44100", "-y",
        output_pattern
    ], check=True)

    tracks = []
    for i, fname in enumerate(sorted(os.listdir(f"outputs/{job_id}"))):
        if fname.startswith("track_") and fname.endswith(".mp3"):
            track_path = f"outputs/{job_id}/{fname}"
            timestamp = i * 180
            info = identify_song(track_path, timestamp)
            tracks.append({
                "name": f"{info['artist']} - {info['title']}",
                "url": f"/download/{job_id}/{fname}",
                "artist": info["artist"],
                "title": info["title"],
                "timestamp": timestamp
            })

    jobs[job_id] = {"status": "done", "tracks": tracks}
    return {"job_id": job_id, "status": "done", "tracks": tracks}

@app.get("/status/{job_id}")
def get_status(job_id: str):
    if job_id not in jobs:
        return {"error": "Job não encontrado"}
    return jobs[job_id]

@app.get("/download/{job_id}/{filename}")
def download_track(job_id: str, filename: str):
    path = f"outputs/{job_id}/{filename}"
    if not os.path.exists(path):
        return {"error": "Arquivo não encontrado"}
    return FileResponse(path, media_type="audio/mpeg", filename=filename)
