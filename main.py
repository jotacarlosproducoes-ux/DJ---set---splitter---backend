from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import FileResponse, Response, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
import uuid, os, shutil, asyncio, requests, hmac, hashlib, base64, time, urllib.parse
import numpy as np
import boto3
from botocore.config import Config

app = FastAPI()

class CORSMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return Response(status_code=200, headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Max-Age": "86400",
            })
        response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "*"
        response.headers["Accept-Ranges"] = "bytes"
        return response

app.add_middleware(CORSMiddleware)

ACR_ACCESS_KEY    = "da746b8377796097a8b57b1cb4fe8a5c"
ACR_ACCESS_SECRET = "Qjxt4orcUoVSgkZPP4vfqdYGf4Vl3Au5j0SKRddl"
ACR_REQURL        = "https://identify-us-west-2.acrcloud.com/v1/identify"

# ─── Cloudflare R2 ────────────────────────────────────────────────────────────
R2_ACCESS_KEY_ID     = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_ENDPOINT          = os.environ.get("R2_ENDPOINT", "")
R2_BUCKET            = os.environ.get("R2_BUCKET", "djsetsplitter")
R2_PUBLIC_URL        = os.environ.get("R2_PUBLIC_URL", "")  # opcional

def get_r2():
    if not R2_ACCESS_KEY_ID or not R2_ENDPOINT:
        return None
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )

def upload_to_r2(local_path: str, r2_key: str) -> bool:
    """Faz upload de um arquivo local para o R2."""
    try:
        s3 = get_r2()
        if not s3:
            return False
        s3.upload_file(local_path, R2_BUCKET, r2_key)
        print(f"[R2] Upload OK: {r2_key}")
        return True
    except Exception as e:
        print(f"[R2] Upload erro: {e}")
        return False

def get_r2_presigned_url(r2_key: str, expires: int = 3600) -> str | None:
    """Gera URL temporária (1h) para download direto do R2."""
    try:
        s3 = get_r2()
        if not s3:
            return None
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET, "Key": r2_key},
            ExpiresIn=expires,
        )
        return url
    except Exception as e:
        print(f"[R2] Presigned URL erro: {e}")
        return None

def download_from_r2(r2_key: str, local_path: str) -> bool:
    """Baixa arquivo do R2 para disco local (para processar)."""
    try:
        s3 = get_r2()
        if not s3:
            return False
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        s3.download_file(R2_BUCKET, r2_key, local_path)
        return True
    except Exception as e:
        print(f"[R2] Download erro: {e}")
        return False

jobs = {}


# ─── Identifica música via ACRCloud ───────────────────────────────────────────
def identify_song(audio_path, timestamp):
    try:
        duration_result = os.popen(
            f'ffprobe -v error -show_entries format=duration '
            f'-of default=noprint_wrappers=1:nokey=1 "{audio_path}"'
        ).read().strip()
        duration     = float(duration_result) if duration_result else 180
        sample_start = max(0, duration / 2 - 10)
        sample_path  = audio_path + "_sample.mp3"
        os.system(f'ffmpeg -ss {sample_start} -t 20 -i "{audio_path}" -y "{sample_path}" -loglevel quiet')
        sample_file  = sample_path if os.path.exists(sample_path) else audio_path

        with open(sample_file, "rb") as f:
            sample_bytes = os.path.getsize(sample_file)
            sample_data  = f.read()

        ts   = time.time()
        stts = f"POST\n/v1/identify\n{ACR_ACCESS_KEY}\naudio\n1\n{ts}"
        sign = base64.b64encode(
            hmac.new(ACR_ACCESS_SECRET.encode(), stts.encode(), hashlib.sha1).digest()
        ).decode()

        resp   = requests.post(ACR_REQURL, timeout=15,
                               files=[('sample', ('sample.mp3', sample_data, 'audio/mpeg'))],
                               data={'access_key': ACR_ACCESS_KEY, 'sample_bytes': sample_bytes,
                                     'timestamp': str(ts), 'signature': sign,
                                     'data_type': 'audio', 'signature_version': '1'})
        result = resp.json()
        print(f"ACRCloud: {result}")
        if os.path.exists(sample_path):
            os.remove(sample_path)
        if result.get("status", {}).get("code") == 0:
            music = result["metadata"]["music"][0]
            return {"title":  music.get("title", "Desconhecida"),
                    "artist": music.get("artists", [{}])[0].get("name", "Desconhecido")}
    except Exception as e:
        print(f"ACRCloud error: {e}")
    return {"title": f"Faixa {int(timestamp)}s", "artist": "Desconhecido"}


# ─── Extensão inteligente de faixa ───────────────────────────────────────────
def extend_track(input_mp3: str, output_mp3: str, target_extra_seconds: int = 60) -> bool:
    try:
        import librosa
        import soundfile as sf

        tmp_wav     = input_mp3 + "_tmp.wav"
        tmp_ext_wav = input_mp3 + "_extended.wav"

        ret = os.system(f'ffmpeg -i "{input_mp3}" -ar 44100 -ac 2 -y "{tmp_wav}" -loglevel quiet')
        if ret != 0 or not os.path.exists(tmp_wav):
            return False

        y, sr = librosa.load(tmp_wav, sr=44100, mono=False)
        if y.ndim == 1:
            y = np.stack([y, y])
        y_mono = librosa.to_mono(y)

        tempo, beats = librosa.beat.beat_track(y=y_mono, sr=sr)
        tempo_val    = float(np.array(tempo).flatten()[0])
        print("[EXTEND] BPM: " + str(round(tempo_val, 1)))

        beat_frames = [int(x) for x in librosa.frames_to_samples(np.array(beats).flatten())]

        if len(beat_frames) < 4:
            seg_len     = int(sr * 30)
            beat_frames = list(range(0, y_mono.shape[0], seg_len))

        beats_per_segment = min(32, max(4, len(beat_frames) // 4))
        segments = []
        for i in range(0, len(beat_frames) - beats_per_segment, beats_per_segment):
            start  = int(beat_frames[i])
            end    = int(beat_frames[min(i + beats_per_segment, len(beat_frames) - 1)])
            if end <= start:
                continue
            energy = float(np.mean(y_mono[start:end] ** 2))
            segments.append({"start": start, "end": end, "energy": energy})

        if not segments:
            mid      = y_mono.shape[0] // 2
            segments = [{"start": max(0, mid - int(sr*30)),
                         "end":   min(y_mono.shape[0], mid + int(sr*30)), "energy": 1.0}]

        best         = max(segments, key=lambda s: s["energy"])
        seg_audio    = y[:, best["start"]:best["end"]]
        fade_samples = min(sr * 4, seg_audio.shape[1])

        seg_faded = seg_audio.copy()
        seg_faded[:, :fade_samples]  *= np.linspace(0.0, 1.0, fade_samples)
        seg_faded[:, -fade_samples:] *= np.linspace(1.0, 0.0, fade_samples)

        seg_duration = seg_audio.shape[1] / sr
        reps         = max(1, int(np.ceil(target_extra_seconds / seg_duration)))
        extended     = np.concatenate([y] + [seg_faded] * reps, axis=1)

        sf.write(tmp_ext_wav, extended.T, sr, subtype="PCM_16")
        ret2 = os.system(
            f'ffmpeg -i "{tmp_ext_wav}" -acodec libmp3lame -ab 320k -ar 44100 -y "{output_mp3}" -loglevel quiet'
        )

        for f in [tmp_wav, tmp_ext_wav]:
            if os.path.exists(f):
                os.remove(f)

        return ret2 == 0 and os.path.exists(output_mp3)

    except Exception as e:
        print(f"[EXTEND] Erro: {e}")
        for f in [input_mp3 + "_tmp.wav", input_mp3 + "_extended.wav"]:
            if os.path.exists(f):
                os.remove(f)
        return False


def safe_filename(name: str) -> str:
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip()


# ─── Endpoints ────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "DJ Set Splitter API online", "r2": bool(R2_ACCESS_KEY_ID)}

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

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", input_path,
        "-f", "segment", "-segment_time", "180",
        "-vn", "-acodec", "mp3", "-ab", "192k", "-ar", "44100",
        "-y", folder + "/track_%03d.mp3",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    await proc.communicate()

    tracks = []
    for i, fname in enumerate(sorted(f for f in os.listdir(folder)
                                     if f.startswith("track_") and f.endswith(".mp3"))):
        track_path   = folder + "/" + fname
        ts           = i * 180
        info         = identify_song(track_path, ts)
        display_name = info["artist"] + " - " + info["title"]
        track_id     = fname.replace(".mp3", "")

        # Upload para R2
        r2_key = f"{job_id}/{fname}"
        upload_to_r2(track_path, r2_key)

        tracks.append({
            "id":           track_id,
            "name":         display_name,
            "artist":       info["artist"],
            "title":        info["title"],
            "timestamp":    ts,
            "url":          f"/download/{job_id}/{fname}",
            "url_mp3":      f"/download/{job_id}/{fname}?format=mp3",
            "url_wav":      f"/download/{job_id}/{fname}?format=wav",
            "url_extended": f"/download/{job_id}/{fname}?format=extended",
        })

    jobs[job_id] = {"status": "done", "tracks": tracks}
    return {"job_id": job_id, "status": "done", "tracks": tracks}


@app.get("/status/{job_id}")
def get_status(job_id: str):
    if job_id not in jobs:
        return {"error": "Job nao encontrado"}
    return jobs[job_id]


@app.get("/download/{job_id}/{filename}")
async def download_track(job_id: str, filename: str, format: str = "stream"):
    base_filename = filename if filename.endswith(".mp3") else filename + ".mp3"
    mp3_path      = f"outputs/{job_id}/{base_filename}"
    r2_key        = f"{job_id}/{base_filename}"

    # Se não existe localmente, tenta buscar do R2
    if not os.path.exists(mp3_path):
        print(f"[R2] Arquivo não encontrado localmente, buscando do R2: {r2_key}")
        ok = download_from_r2(r2_key, mp3_path)
        if not ok:
            return Response(content='{"error":"Arquivo nao encontrado"}',
                            status_code=404, media_type="application/json")

    # Nome amigável
    display_name = base_filename.replace(".mp3", "")
    if job_id in jobs:
        for t in jobs[job_id].get("tracks", []):
            if t.get("id") == base_filename.replace(".mp3", ""):
                display_name = safe_filename(t["name"])
                break

    # ── Versão estendida ──────────────────────────────────────────────────────
    if format == "extended":
        ext_path  = mp3_path.replace(".mp3", "_extended.mp3")
        ext_r2key = r2_key.replace(".mp3", "_extended.mp3")

        if not os.path.exists(ext_path):
            # Tenta buscar do R2 primeiro (cache)
            download_from_r2(ext_r2key, ext_path)

        if not os.path.exists(ext_path):
            loop = asyncio.get_event_loop()
            ok   = await loop.run_in_executor(None, extend_track, mp3_path, ext_path, 60)
            if not ok:
                return Response(content='{"error":"Falha ao gerar versao estendida"}',
                                status_code=500, media_type="application/json")
            # Salva no R2 para próximas requisições
            upload_to_r2(ext_path, ext_r2key)

        encoded = urllib.parse.quote(display_name + "_extended.mp3")
        return FileResponse(ext_path, media_type="audio/mpeg", headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
            "Access-Control-Allow-Origin": "*",
            "Accept-Ranges": "bytes",
        })

    # ── WAV ───────────────────────────────────────────────────────────────────
    if format == "wav":
        wav_path  = mp3_path.replace(".mp3", ".wav")
        wav_r2key = r2_key.replace(".mp3", ".wav")

        if not os.path.exists(wav_path):
            download_from_r2(wav_r2key, wav_path)

        if not os.path.exists(wav_path):
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-i", mp3_path, "-acodec", "pcm_s16le",
                "-ar", "44100", "-ac", "2", "-y", wav_path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            await proc.communicate()
            upload_to_r2(wav_path, wav_r2key)

        if not os.path.exists(wav_path):
            return Response(content='{"error":"Falha ao converter para WAV"}',
                            status_code=500, media_type="application/json")

        encoded = urllib.parse.quote(display_name + ".wav")
        return FileResponse(wav_path, media_type="audio/wav", headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
            "Access-Control-Allow-Origin": "*",
            "Accept-Ranges": "bytes",
        })

    # ── MP3 / stream ──────────────────────────────────────────────────────────
    encoded     = urllib.parse.quote(display_name + ".mp3")
    disposition = "inline" if format == "stream" else f"attachment; filename*=UTF-8''{encoded}"
    return FileResponse(mp3_path, media_type="audio/mpeg", headers={
        "Content-Disposition": disposition,
        "Content-Length":      str(os.path.getsize(mp3_path)),
        "Access-Control-Allow-Origin": "*",
        "Accept-Ranges": "bytes",
    })
