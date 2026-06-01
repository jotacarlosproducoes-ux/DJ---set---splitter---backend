from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
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
import urllib.parse

app = FastAPI()

# ─── CORS: permite qualquer origem (necessário para Lovable + preview) ─────────
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
        # Necessário para o player de áudio funcionar no browser
        response.headers["Accept-Ranges"] = "bytes"
        return response

app.add_middleware(CORSMiddleware)

ACR_ACCESS_KEY = "da746b8377796097a8b57b1cb4fe8a5c"
ACR_ACCESS_SECRET = "Qjxt4orcUoVSgkZPP4vfqdYGf4Vl3Au5j0SKRddl"
ACR_REQURL = "https://identify-us-west-2.acrcloud.com/v1/identify"

jobs = {}

def identify_song(audio_path, timestamp):
    try:
        duration_result = os.popen(
            f'ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "{audio_path}"'
        ).read().strip()
        duration = float(duration_result) if duration_result else 180
        sample_start = max(0, duration / 2 - 10)
        sample_path = audio_path + "_sample.mp3"
        os.system(f'ffmpeg -ss {sample_start} -t 20 -i "{audio_path}" -y "{sample_path}" -loglevel quiet')
        sample_file = sample_path if os.path.exists(sample_path) else audio_path

        with open(sample_file, "rb") as f:
            sample_bytes = os.path.getsize(sample_file)
            sample_data = f.read()

        http_method = "POST"
        http_uri = "/v1/identify"
        data_type = "audio"
        signature_version = "1"
        ts = time.time()

        string_to_sign = (
            http_method + "\n" + http_uri + "\n" +
            ACR_ACCESS_KEY + "\n" + data_type + "\n" +
            signature_version + "\n" + str(ts)
        )

        sign = base64.b64encode(
            hmac.new(
                ACR_ACCESS_SECRET.encode('ascii'),
                string_to_sign.encode('ascii'),
                digestmod=hashlib.sha1
            ).digest()
        ).decode('ascii')

        files = [('sample', ('sample.mp3', sample_data, 'audio/mpeg'))]
        data = {
            'access_key': ACR_ACCESS_KEY,
            'sample_bytes': sample_bytes,
            'timestamp': str(ts),
            'signature': sign,
            'data_type': data_type,
            'signature_version': signature_version,
        }

        response = requests.post(ACR_REQURL, files=files, data=data, timeout=15)
        result = response.json()
        print(f"ACRCloud response: {result}")

        if os.path.exists(sample_path):
            os.remove(sample_path)

        if result.get("status", {}).get("code") == 0:
            music = result["metadata"]["music"][0]
            title = music.get("title", "Desconhecida")
            artist = music.get("artists", [{}])[0].get("name", "Desconhecido")
            return {"title": title, "artist": artist}

    except Exception as e:
        print(f"ACRCloud error: {e}")

    return {"title": "Faixa " + str(int(timestamp)) + "s", "artist": "Desconhecido"}


def safe_filename(name: str) -> str:
    """Preserva UTF-8 (Tiësto, acentos) mas remove caracteres inválidos em nomes de arquivo"""
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip()


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
        "-vn", "-acodec", "mp3",
        "-ab", "192k", "-ar", "44100",
        "-y", output_pattern,
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
            ts = i * 180
            info = identify_song(track_path, ts)
            display_name = info["artist"] + " - " + info["title"]
            track_id = fname.replace(".mp3", "")
            tracks.append({
                "id": track_id,
                "name": display_name,
                "artist": info["artist"],
                "title": info["title"],
                "timestamp": ts,
                # URL sem query string — funciona para player E download
                "url": "/download/" + job_id + "/" + fname,
                "url_mp3": "/download/" + job_id + "/" + fname + "?format=mp3",
                "url_wav": "/download/" + job_id + "/" + fname + "?format=wav",
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
async def download_track(job_id: str, filename: str, format: str = "mp3"):
    """
    Serve o arquivo para player (sem format) ou download (format=mp3/wav).
    Suporta Range requests para o player de áudio funcionar no browser.
    """
    base_filename = filename if filename.endswith(".mp3") else filename + ".mp3"
    mp3_path = "outputs/" + job_id + "/" + base_filename

    if not os.path.exists(mp3_path):
        return Response(
            content='{"error": "Arquivo nao encontrado"}',
            status_code=404,
            media_type="application/json"
        )

    # Descobre nome amigável
    display_name = base_filename.replace(".mp3", "")
    if job_id in jobs:
        track_id = base_filename.replace(".mp3", "")
        for t in jobs[job_id].get("tracks", []):
            if t.get("id") == track_id:
                display_name = safe_filename(t["name"])
                break

    # ── WAV: converte e serve ──────────────────────────────────────────────────
    if format == "wav":
        wav_path = mp3_path.replace(".mp3", ".wav")
        if not os.path.exists(wav_path):
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-i", mp3_path,
                "-acodec", "pcm_s16le",
                "-ar", "44100", "-ac", "2",
                "-y", wav_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await proc.communicate()

        if not os.path.exists(wav_path):
            return Response(
                content='{"error": "Falha ao converter para WAV"}',
                status_code=500,
                media_type="application/json"
            )

        encoded = urllib.parse.quote(display_name + ".wav")
        return FileResponse(
            wav_path,
            media_type="audio/wav",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
                "Access-Control-Allow-Origin": "*",
                "Accept-Ranges": "bytes",
            }
        )

    # ── MP3: serve direto com suporte a Range (necessário para o player) ──────
    file_size = os.path.getsize(mp3_path)
    encoded = urllib.parse.quote(display_name + ".mp3")

    # Se for só streaming (player), serve sem forçar download
    is_download = format == "mp3"
    disposition = f"attachment; filename*=UTF-8''{encoded}" if is_download else "inline"

    return FileResponse(
        mp3_path,
        media_type="audio/mpeg",
        headers={
            "Content-Disposition": disposition,
            "Content-Length": str(file_size),
            "Access-Control-Allow-Origin": "*",
            "Accept-Ranges": "bytes",
        }
    )
