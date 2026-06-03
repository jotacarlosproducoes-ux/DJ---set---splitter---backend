from fastapi import FastAPI, UploadFile, File, Request, BackgroundTasks
from fastapi.responses import FileResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
import uuid, os, shutil, asyncio, requests, hmac, hashlib, base64, time, urllib.parse
import numpy as np
import boto3
from botocore.config import Config
import redis

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
AUDD_API_TOKEN    = os.environ.get("AUDD_API_TOKEN", "")

R2_ACCESS_KEY_ID     = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_ENDPOINT          = os.environ.get("R2_ENDPOINT", "")
R2_BUCKET            = os.environ.get("R2_BUCKET", "djsetsplitter")
REDIS_URL            = os.environ.get("REDIS_URL", "")

# ─── Redis ────────────────────────────────────────────────────────────────────
def get_redis():
    if not REDIS_URL:
        return None
    try:
        r = redis.from_url(REDIS_URL, decode_responses=True)
        r.ping()
        return r
    except Exception as e:
        print(f"[REDIS] Erro: {e}")
        return None

def job_set(job_id: str, data: dict):
    r = get_redis()
    if r:
        import json
        r.setex(f"job:{job_id}", 86400, json.dumps(data))  # expira em 24h
    else:
        jobs[job_id] = data

def job_get(job_id: str) -> dict | None:
    r = get_redis()
    if r:
        import json
        val = r.get(f"job:{job_id}")
        return json.loads(val) if val else None
    return jobs.get(job_id)

# Fallback local se Redis não disponível
jobs = {}

# ─── R2 ───────────────────────────────────────────────────────────────────────
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

def download_from_r2(r2_key: str, local_path: str) -> bool:
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


# ─── AudD fallback ────────────────────────────────────────────────────────────
def identify_with_audd(audio_path: str) -> dict | None:
    if not AUDD_API_TOKEN:
        return None
    try:
        with open(audio_path, "rb") as f:
            resp = requests.post(
                "https://api.audd.io/",
                data={"api_token": AUDD_API_TOKEN, "return": "apple_music,spotify"},
                files={"file": f},
                timeout=15,
            )
        result = resp.json()
        print(f"AudD: {result}")
        if result.get("status") == "success" and result.get("result"):
            r = result["result"]
            return {"title": r.get("title", "Desconhecida"),
                    "artist": r.get("artist", "Desconhecido")}
    except Exception as e:
        print(f"AudD error: {e}")
    return None


# ─── ACRCloud ────────────────────────────────────────────────────────────────
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

    # Fallback AudD
    print("[ID] ACRCloud sem resultado, tentando AudD...")
    audd = identify_with_audd(audio_path)
    if audd:
        return audd

    return {"title": f"Faixa {int(timestamp)}s", "artist": "Desconhecido"}


# ─── Extensão profissional com Spleeter (Opção 3) ────────────────────────────
def extend_track(input_mp3: str, output_mp3: str, target_extra_seconds: int = 60) -> bool:
    """
    Extensão de alta qualidade usando Spleeter:
    1. Separa a faixa em 4 stems: vocals, drums, bass, other
    2. Detecta o beat grid exato via librosa em cada stem
    3. Encontra o melhor loop point (alta energia, beat-aligned)
    4. Repete o segmento com crossfade harmônico em cada stem
    5. Recombina os stems e exporta como MP3 320k
    """
    import tempfile
    tmp_dir = tempfile.mkdtemp()
    tmp_wav = os.path.join(tmp_dir, "input.wav")

    try:
        import librosa
        import soundfile as sf

        # 1. MP3 → WAV 44100Hz stereo
        ret = os.system(f'ffmpeg -i "{input_mp3}" -ar 44100 -ac 2 -y "{tmp_wav}" -loglevel quiet')
        if ret != 0 or not os.path.exists(tmp_wav):
            raise Exception("Falha na conversão MP3→WAV")

        # 2. Separação de stems com Spleeter
        try:
            from spleeter.separator import Separator
            separator = Separator("spleeter:4stems")
            stems_dir = os.path.join(tmp_dir, "stems")
            os.makedirs(stems_dir, exist_ok=True)
            separator.separate_to_file(tmp_wav, stems_dir)
            stem_base = os.path.join(stems_dir, "input")
            stem_files = {
                "vocals": os.path.join(stem_base, "vocals.wav"),
                "drums":  os.path.join(stem_base, "drums.wav"),
                "bass":   os.path.join(stem_base, "bass.wav"),
                "other":  os.path.join(stem_base, "other.wav"),
            }
            use_spleeter = all(os.path.exists(f) for f in stem_files.values())
            print("[EXTEND] Spleeter: " + ("OK" if use_spleeter else "FALHOU, usando fallback"))
        except Exception as e:
            print(f"[EXTEND] Spleeter erro: {e} — usando fallback")
            use_spleeter = False

        if use_spleeter:
            # 3. Processa cada stem individualmente
            extended_stems = {}
            sr_global = 44100

            for stem_name, stem_path in stem_files.items():
                y, sr = librosa.load(stem_path, sr=44100, mono=False)
                if y.ndim == 1:
                    y = np.stack([y, y])
                sr_global = sr

                y_mono = librosa.to_mono(y)
                tempo, beats = librosa.beat.beat_track(y=y_mono, sr=sr)
                beat_samples = [int(x) for x in librosa.frames_to_samples(np.array(beats).flatten())]

                if len(beat_samples) < 8:
                    seg_len = int(sr * 16)
                    beat_samples = list(range(0, y_mono.shape[0], seg_len))

                # Encontra o melhor loop de 32 beats (alta energia)
                loop_beats = 32
                best_seg = None
                best_energy = -1

                for i in range(0, len(beat_samples) - loop_beats, 4):
                    s = int(beat_samples[i])
                    e = int(beat_samples[min(i + loop_beats, len(beat_samples) - 1)])
                    if e <= s:
                        continue
                    seg = y_mono[s:e]
                    energy = float(np.mean(seg ** 2))
                    if energy > best_energy:
                        best_energy = energy
                        best_seg = (s, e)

                if best_seg is None:
                    mid = y_mono.shape[0] // 2
                    best_seg = (max(0, mid - int(sr * 16)), min(y_mono.shape[0], mid + int(sr * 16)))

                s, e = best_seg
                loop_audio = y[:, s:e]

                # Crossfade longo de 8s para junção imperceptível
                fade_len = min(int(sr * 8), loop_audio.shape[1] // 3)
                fade_in  = np.linspace(0.0, 1.0, fade_len) ** 2  # curva suave
                fade_out = np.linspace(1.0, 0.0, fade_len) ** 2

                loop_faded = loop_audio.copy()
                loop_faded[:, :fade_len]  *= fade_in
                loop_faded[:, -fade_len:] *= fade_out

                seg_dur = loop_audio.shape[1] / sr
                reps    = max(1, int(np.ceil(target_extra_seconds / seg_dur)))
                extended_stems[stem_name] = np.concatenate([y] + [loop_faded] * reps, axis=1)

            # 4. Garante que todos os stems têm o mesmo tamanho
            min_len = min(s.shape[1] for s in extended_stems.values())
            for k in extended_stems:
                extended_stems[k] = extended_stems[k][:, :min_len]

            # 5. Recombina os stems
            combined = sum(extended_stems.values())
            # Normaliza para evitar clipping
            peak = np.max(np.abs(combined))
            if peak > 0.95:
                combined = combined * (0.95 / peak)

            combined_path = os.path.join(tmp_dir, "combined.wav")
            sf.write(combined_path, combined.T, sr_global, subtype="PCM_16")

        else:
            # Fallback: extensão simples sem Spleeter mas com beat-align
            y, sr = librosa.load(tmp_wav, sr=44100, mono=False)
            if y.ndim == 1:
                y = np.stack([y, y])
            y_mono = librosa.to_mono(y)

            tempo, beats = librosa.beat.beat_track(y=y_mono, sr=sr)
            beat_samples = [int(x) for x in librosa.frames_to_samples(np.array(beats).flatten())]

            if len(beat_samples) < 8:
                seg_len = int(sr * 16)
                beat_samples = list(range(0, y_mono.shape[0], seg_len))

            loop_beats = min(32, len(beat_samples) // 4)
            best_seg = None
            best_energy = -1

            for i in range(0, len(beat_samples) - loop_beats, 4):
                s = int(beat_samples[i])
                e = int(beat_samples[min(i + loop_beats, len(beat_samples) - 1)])
                if e <= s:
                    continue
                energy = float(np.mean(y_mono[s:e] ** 2))
                if energy > best_energy:
                    best_energy = energy
                    best_seg = (s, e)

            if best_seg is None:
                mid = y_mono.shape[0] // 2
                best_seg = (max(0, mid - int(sr * 16)), min(y_mono.shape[0], mid + int(sr * 16)))

            s, e = best_seg
            loop_audio = y[:, s:e]
            fade_len   = min(int(sr * 8), loop_audio.shape[1] // 3)
            fade_in    = np.linspace(0.0, 1.0, fade_len) ** 2
            fade_out   = np.linspace(1.0, 0.0, fade_len) ** 2

            loop_faded = loop_audio.copy()
            loop_faded[:, :fade_len]  *= fade_in
            loop_faded[:, -fade_len:] *= fade_out

            seg_dur  = loop_audio.shape[1] / sr
            reps     = max(1, int(np.ceil(target_extra_seconds / seg_dur)))
            combined = np.concatenate([y] + [loop_faded] * reps, axis=1)

            peak = np.max(np.abs(combined))
            if peak > 0.95:
                combined = combined * (0.95 / peak)

            combined_path = os.path.join(tmp_dir, "combined.wav")
            sf.write(combined_path, combined.T, sr, subtype="PCM_16")

        # 6. Exporta como MP3 320kbps
        ret2 = os.system(
            f'ffmpeg -i "{combined_path}" -acodec libmp3lame -ab 320k -ar 44100 -y "{output_mp3}" -loglevel quiet'
        )

        return ret2 == 0 and os.path.exists(output_mp3)

    except Exception as e:
        print(f"[EXTEND] Erro: {e}")
        return False
    finally:
        # Limpa arquivos temporários
        shutil.rmtree(tmp_dir, ignore_errors=True)


def safe_filename(name: str) -> str:
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip()


# ─── Processamento em background ─────────────────────────────────────────────
def process_job(job_id: str, input_path: str, folder: str):
    """Processa o job em background — divide, identifica e salva no R2."""
    try:
        job_set(job_id, {"status": "processing", "tracks": [], "progress": 0})

        output_pattern = folder + "/track_%03d.mp3"
        os.system(
            f'ffmpeg -i "{input_path}" -f segment -segment_time 180 '
            f'-vn -acodec mp3 -ab 192k -ar 44100 -y "{output_pattern}" -loglevel quiet'
        )

        fnames = sorted(f for f in os.listdir(folder)
                        if f.startswith("track_") and f.endswith(".mp3"))
        total  = len(fnames)
        tracks = []

        for i, fname in enumerate(fnames):
            track_path   = folder + "/" + fname
            ts           = i * 180
            info         = identify_song(track_path, ts)
            display_name = info["artist"] + " - " + info["title"]
            track_id     = fname.replace(".mp3", "")

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

            progress = int((i + 1) / total * 100)
            job_set(job_id, {"status": "processing", "tracks": tracks, "progress": progress})

        job_set(job_id, {"status": "done", "tracks": tracks, "progress": 100})
        print(f"[JOB] {job_id} concluído — {total} faixas")

    except Exception as e:
        print(f"[JOB] Erro: {e}")
        job_set(job_id, {"status": "error", "error": str(e), "tracks": []})


# ─── Endpoints ────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "DJ Set Splitter API online", "r2": bool(R2_ACCESS_KEY_ID), "redis": bool(REDIS_URL)}

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/split")
async def split_audio(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    """Recebe o arquivo e inicia o processamento em background imediatamente."""
    job_id = str(uuid.uuid4())
    folder = "outputs/" + job_id
    os.makedirs(folder, exist_ok=True)

    input_path = folder + "/input.mp3"
    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Inicia processamento em background
    job_set(job_id, {"status": "processing", "tracks": [], "progress": 0})
    background_tasks.add_task(process_job, job_id, input_path, folder)

    return {"job_id": job_id, "status": "processing"}


@app.get("/status/{job_id}")
def get_status(job_id: str):
    data = job_get(job_id)
    if not data:
        return {"error": "Job nao encontrado"}
    return data


@app.get("/download/{job_id}/{filename}")
async def download_track(job_id: str, filename: str, format: str = "stream"):
    base_filename = filename if filename.endswith(".mp3") else filename + ".mp3"
    mp3_path      = f"outputs/{job_id}/{base_filename}"
    r2_key        = f"{job_id}/{base_filename}"

    if not os.path.exists(mp3_path):
        ok = download_from_r2(r2_key, mp3_path)
        if not ok:
            return Response(content='{"error":"Arquivo nao encontrado"}',
                            status_code=404, media_type="application/json")

    # Nome amigável
    display_name = base_filename.replace(".mp3", "")
    data = job_get(job_id)
    if data:
        for t in data.get("tracks", []):
            if t.get("id") == base_filename.replace(".mp3", ""):
                display_name = safe_filename(t["name"])
                break

    # ── Versão estendida ──────────────────────────────────────────────────────
    if format == "extended":
        ext_path  = mp3_path.replace(".mp3", "_extended.mp3")
        ext_r2key = r2_key.replace(".mp3", "_extended.mp3")

        if not os.path.exists(ext_path):
            download_from_r2(ext_r2key, ext_path)

        if not os.path.exists(ext_path):
            loop = asyncio.get_event_loop()
            ok   = await loop.run_in_executor(None, extend_track, mp3_path, ext_path, 60)
            if not ok:
                return Response(content='{"error":"Falha ao gerar versao estendida"}',
                                status_code=500, media_type="application/json")
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
