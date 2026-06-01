from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
import uuid
import os
import shutil
import asyncio
import requests
import hmac
import hashlib
import base64
import time

app = FastAPI()

class CORSMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return Response(
                status_code=200,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                    "Access-Control-Allow-Headers": "*",
                    "Access-Control-Max-Age": "86400",
                }
            )
        response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "*"
        return response

app.add_middleware(CORSMiddleware)

ACR_HOST = "identify-us-west-2.acrcloud.com"
ACR_KEY = "da746b8377796097a8b57b1cb4fe8a5c"
ACR_SECRET = "Qxt4orcUoVSgkZPP4vfqdYGf4V13Au5j0SKRddl"

jobs = {}

def identify_song(audio_path, timestamp):
    try:
        url = "https://identify-us-west-2.acrcloud.com/v1/identify"

        # Pega duração do arquivo
        duration_result = os.popen(
            f'ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "{audio_path}"'
        ).read().strip()
        duration = float(duration_result) if duration_result else 180

        # Pega amostra do meio da faixa
        sample_start = max(0, duration / 2 - 10)
        sample_path = audio_path + "_sample.mp3"
        os.system(
            f'ffmpeg -ss {sample_start} -t 20 -i "{audio_path}" -y "{sample_path}" -loglevel quiet'
        )
        sample_file = sample_path if os.path.exists(sample_path) else audio_path

        # Assinatura correta conforme documentação ACRCloud
        http_method = "POST"
        http_uri = "/v1/identify"
        data_type = "audio"
        signature_version = "1"
        ts = str(time.time())
        string_to_sign = "\n".join([http_method, http_uri, ACR_KEY, data_type, signature_version, ts])

        sign = base64.b64encode(
            hmac.new(
                ACR_SECRET.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                hashlib.sha1
            ).digest()
        ).decode("utf-8")

        with open(sample_file, "rb") as f:
            sample_bytes = os.path.getsize(sample_file)
            files = [("sample", ("sample.mp3", f, "audio/mpeg"))]
            data = {
                "access_key": ACR_KEY,
                "sample_bytes": sample_bytes,
                "timestamp": ts,
                "signature": sign,
                "data_type": data_type,
                "signature_version": signature_version,
            }
            response = requests.post(url, files=files, data=data, timeout=15)
            result = response.json()

        print(f"ACRCloud response: {result}")

        # Limpa arquivo temporário
        if os.path.exists(sample_path):
            os.remove(sample_path)

        if result.get("status", {}).get("code") == 0:
            music = result["metadata"]["music"][0]
            title = music.get("title", "Desconhecida")
            artist = music.get("artists", [{}])[0].get("name", "Desconhecido")
            return {"title": title, "artist": artist}

    except Exception as e:
        print(f"ACRCloud error: {e}")

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

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", input_path,
        "-f", "segment",
        "-segment_time", "180",
        "-vn",
        "-acodec", "mp3",
        "-ab", "192k",
        "-ar", "44100",
        "-y",
        output_pattern,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    await proc.communicate()

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
def get_status(job_id: str):
    if job_id not in jobs:
        return {"error": "Job nao encontrado"}
    return jobs[job_id]


@app.get("/download/{job_id}/{filename}")
def download_track(job_id: str, filename: str):
    path = "outputs/" + job_id + "/" + filename
    if not os.path.exists(path):
        return {"error": "Arquivo nao encontrado"}
    return FileResponse(path, media_type="audio/mpeg", filename=filename)
