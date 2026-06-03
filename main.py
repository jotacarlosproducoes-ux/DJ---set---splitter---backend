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


# ─── Extensão profissional com Demucs (Meta AI) ─────────────────────────────
def extend_track(input_mp3: str, output_mp3: str, target_extra_seconds: int = 60) -> bool:
    """
    Extensão de alta qualidade usando Demucs:
    1. Separa a faixa em stems: drums, vocals, bass, other
    2. Detecta beat grid exato via librosa
    3. Cria intro de 16 compassos SÓ com bateria (drums stem)
    4. Adiciona outro de 16 compassos SÓ com bateria no final
    5. Crossfade suave nas transições
    6. Exporta como MP3 320k
    """
    import tempfile
    tmp_dir = tempfile.mkdtemp()
    tmp_wav = os.path.join(tmp_dir, "input.wav")

    try:
        import librosa
        import soundfile as sf
        import subprocess

        # 1. MP3 → WAV 44100Hz stereo
        ret = os.system(f'ffmpeg -i "{input_mp3}" -ar 44100 -ac 2 -y "{tmp_wav}" -loglevel quiet')
        if ret != 0 or not os.path.exists(tmp_wav):
            raise Exception("Falha na conversão MP3→WAV")

        y_full, sr = librosa.load(tmp_wav, sr=44100, mono=False)
        if y_full.ndim == 1:
            y_full = np.stack([y_full, y_full])

        y_mono = librosa.to_mono(y_full)
        tempo, beats = librosa.beat.beat_track(y=y_mono, sr=sr)
        beat_samples = [int(x) for x in librosa.frames_to_samples(np.array(beats).flatten())]
        tempo_val = float(np.array(tempo).flatten()[0])
        print(f"[EXTEND] BPM detectado: {round(tempo_val, 1)}")

        if len(beat_samples) < 8:
            seg_len = int(sr * 2)
            beat_samples = list(range(0, y_mono.shape[0], seg_len))

        # 2. Separar bateria com Demucs (4stems para melhor qualidade)
        drums_audio = None
        try:
            stems_dir = os.path.join(tmp_dir, "stems")
            os.makedirs(stems_dir, exist_ok=True)
            result = subprocess.run(
                ["python", "-m", "demucs",
                 "-n", "htdemucs",
                 "--four-stems",
                 "-o", stems_dir, tmp_wav],
                capture_output=True, text=True, timeout=400
            )
            print(f"[EXTEND] Demucs retornou: {result.returncode}")
            # htdemucs salva em stems_dir/htdemucs/input/
            drums_path = os.path.join(stems_dir, "htdemucs", "input", "drums.wav")
            if os.path.exists(drums_path):
                drums_audio, _ = librosa.load(drums_path, sr=44100, mono=False)
                if drums_audio.ndim == 1:
                    drums_audio = np.stack([drums_audio, drums_audio])
                # Garante mesmo tamanho que a faixa original
                min_len = min(y_full.shape[1], drums_audio.shape[1])
                y_full = y_full[:, :min_len]
                drums_audio = drums_audio[:, :min_len]
                print(f"[EXTEND] Demucs drums OK — {round(min_len/sr, 1)}s")
            else:
                print(f"[EXTEND] drums.wav não encontrado em {drums_path}")
                # Lista o que foi gerado
                for root, dirs, files in os.walk(stems_dir):
                    for f in files:
                        print(f"[EXTEND] Arquivo gerado: {os.path.join(root, f)}")
        except Exception as e:
            print(f"[EXTEND] Demucs erro: {e}")

        # Fallback: se Demucs falhou, usa high-pass filter para simular bateria
        if drums_audio is None:
            print("[EXTEND] Usando fallback — high-pass filter para simular bateria")
            from scipy.signal import butter, sosfilt
            def highpass(data, cutoff=200, fs=44100, order=4):
                sos = butter(order, cutoff / (fs/2), btype='high', output='sos')
                return sosfilt(sos, data)
            drums_audio = np.stack([
                highpass(y_full[0]),
                highpass(y_full[1])
            ])

        # 3. Calcula duração do loop de bateria — 16 compassos alinhados no beat
        beats_per_bar = 4
        bars = 16
        beats_needed = bars * beats_per_bar  # 64 beats ≈ 30s a 128BPM

        # Pega amostras alinhadas ao beat
        if len(beat_samples) > beats_needed:
            intro_len = int(beat_samples[beats_needed]) - int(beat_samples[0])
            outro_len = int(beat_samples[-1]) - int(beat_samples[-(beats_needed+1)])
        else:
            intro_len = int(sr * 30)
            outro_len = int(sr * 30)

        intro_len = min(intro_len, int(sr * 35), drums_audio.shape[1] // 3)
        outro_len = min(outro_len, int(sr * 35), drums_audio.shape[1] // 3)

        # 4. Extrai segmentos de bateria para intro e outro
        # Intro: pega a bateria do MEIO da música (mais representativo do groove)
        mid = drums_audio.shape[1] // 2
        mid_start = int(beat_samples[len(beat_samples)//2]) if len(beat_samples) > beats_needed else mid
        mid_start = max(0, mid_start)
        mid_end   = min(drums_audio.shape[1], mid_start + intro_len)
        drums_loop = drums_audio[:, mid_start:mid_end]

        # 5. Monta a estrutura: [bateria intro] + [música] + [bateria outro]
        fade_samples = min(int(sr * 2), intro_len // 4, outro_len // 4)
        f_in  = np.linspace(0.0, 1.0, fade_samples)
        f_out = np.linspace(1.0, 0.0, fade_samples)

        # Intro de bateria (sem fade in, começa direto; fade out no final)
        intro_drum = drums_loop[:, :intro_len].copy()
        intro_drum[:, -fade_samples:] = (
            intro_drum[:, -fade_samples:] * f_out +
            y_full[:, :fade_samples] * f_in
        )

        # Música completa (sem fade — não mexe no meio)
        music = y_full.copy()

        # Outro de bateria (fade in no início; fade out suave no final)
        outro_drum = drums_loop[:, :outro_len].copy()
        outro_drum[:, :fade_samples] = (
            y_full[:, -fade_samples:] * f_out +
            outro_drum[:, :fade_samples] * f_in
        )
        # Fade out final suave (últimos 4s)
        final_fade = min(int(sr * 4), outro_len)
        outro_drum[:, -final_fade:] *= np.linspace(1.0, 0.0, final_fade)

        # 6. Concatena: intro_drum (com transição embutida) + música + outro_drum
        # A transição já está embutida nos fades — sem sobreposição dupla
        extended = np.concatenate([
            intro_drum[:, :-fade_samples],   # intro sem os últimos fade_samples
            y_full,                           # música completa
            outro_drum[:, fade_samples:]      # outro sem os primeiros fade_samples
        ], axis=1)

        # 7. Normaliza
        peak = np.max(np.abs(extended))
        if peak > 0.95:
            extended = extended * (0.95 / peak)

        # 8. Exporta
        combined_path = os.path.join(tmp_dir, "combined.wav")
        sf.write(combined_path, extended.T, sr, subtype="PCM_16")
        ret2 = os.system(
            f'ffmpeg -i "{combined_path}" -acodec libmp3lame -ab 320k -ar 44100 -y "{output_mp3}" -loglevel quiet'
        )

        total = extended.shape[1] / sr
        print(f"[EXTEND] OK — {round(intro_len/sr,1)}s bateria + {round(y_full.shape[1]/sr,1)}s música + {round(outro_len/sr,1)}s bateria = {round(total,1)}s total")
        return ret2 == 0 and os.path.exists(output_mp3)

    except Exception as e:
        print(f"[EXTEND] Erro fatal: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
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
