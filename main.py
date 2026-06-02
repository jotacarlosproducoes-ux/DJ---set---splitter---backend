from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import FileResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
import uuid, os, shutil, asyncio, requests, hmac, hashlib, base64, time, urllib.parse
import numpy as np

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

jobs = {}


# ─── Identifica música via ACRCloud ───────────────────────────────────────────
def identify_song(audio_path, timestamp):
    try:
        duration_result = os.popen(
            f'ffprobe -v error -show_entries format=duration '
            f'-of default=noprint_wrappers=1:nokey=1 "{audio_path}"'
        ).read().strip()
        duration    = float(duration_result) if duration_result else 180
        sample_start = max(0, duration / 2 - 10)
        sample_path  = audio_path + "_sample.mp3"
        os.system(f'ffmpeg -ss {sample_start} -t 20 -i "{audio_path}" -y "{sample_path}" -loglevel quiet')
        sample_file  = sample_path if os.path.exists(sample_path) else audio_path

        with open(sample_file, "rb") as f:
            sample_bytes = os.path.getsize(sample_file)
            sample_data  = f.read()

        ts  = time.time()
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
            music  = result["metadata"]["music"][0]
            return {"title":  music.get("title", "Desconhecida"),
                    "artist": music.get("artists", [{}])[0].get("name", "Desconhecido")}
    except Exception as e:
        print(f"ACRCloud error: {e}")
    return {"title": f"Faixa {int(timestamp)}s", "artist": "Desconhecido"}


# ─── Extensão inteligente de faixa ───────────────────────────────────────────
def extend_track(input_mp3: str, output_mp3: str, target_extra_seconds: int = 60) -> bool:
    """
    Estende uma faixa usando análise de estrutura musical:
    1. Converte MP3 → WAV
    2. Detecta BPM e batidas com librosa
    3. Divide em segmentos de 8 compassos
    4. Identifica o segmento de maior energia (refrão/drop)
    5. Duplica esse segmento na posição certa (cria versão estendida)
    6. Aplica crossfade suave entre seções
    7. Exporta como MP3
    """
    try:
        import librosa
        import soundfile as sf
        from scipy.signal import fftconvolve

        tmp_wav     = input_mp3 + "_tmp.wav"
        tmp_ext_wav = input_mp3 + "_extended.wav"

        # 1. MP3 → WAV
        ret = os.system(f'ffmpeg -i "{input_mp3}" -ar 44100 -ac 2 -y "{tmp_wav}" -loglevel quiet')
        if ret != 0 or not os.path.exists(tmp_wav):
            return False

        # 2. Carrega áudio
        y, sr = librosa.load(tmp_wav, sr=44100, mono=False)
        if y.ndim == 1:
            y = np.stack([y, y])          # garante stereo
        y_mono = librosa.to_mono(y)

        # 3. Detecta BPM e batidas
        tempo, beats = librosa.beat.beat_track(y=y_mono, sr=sr)
        # Garante que tempo é escalar Python
        tempo_val = float(np.array(tempo).flatten()[0])
        print("[EXTEND] BPM detectado: " + str(round(tempo_val, 1)))

        # Converte frames para samples e garante array 1D de ints Python
        beat_frames = [int(x) for x in librosa.frames_to_samples(np.array(beats).flatten())]

        if len(beat_frames) < 4:
            print("[EXTEND] Poucas batidas detectadas, usando segmentação por tempo")
            # Fallback: divide em segmentos de 30s
            seg_len = int(sr * 30)
            total   = y_mono.shape[0]
            beat_frames = list(range(0, total, seg_len))

        # 4. Divide em segmentos de 8 compassos (32 batidas)
        beats_per_segment = min(32, max(4, len(beat_frames) // 4))
        segments = []
        for i in range(0, len(beat_frames) - beats_per_segment, beats_per_segment):
            start  = int(beat_frames[i])
            end    = int(beat_frames[min(i + beats_per_segment, len(beat_frames) - 1)])
            if end <= start:
                continue
            seg    = y_mono[start:end]
            energy = float(np.mean(seg ** 2))
            segments.append({"start": start, "end": end, "energy": energy, "index": i})

        if not segments:
            # Fallback: usa a metade central da faixa
            mid   = y_mono.shape[0] // 2
            start = mid - int(sr * 30)
            end   = mid + int(sr * 30)
            segments = [{"start": max(0, start), "end": min(y_mono.shape[0], end), "energy": 1.0, "index": 0}]

        # 5. Segmento de maior energia = drop/refrão
        best = max(segments, key=lambda s: s["energy"])
        print("[EXTEND] Melhor segmento: " + str(round(best['start']/sr, 1)) + "s – " + str(round(best['end']/sr, 1)) + "s")

        # 6. Monta versão estendida:
        #    [original completa] + [crossfade] + [segmento do drop repetido]
        seg_audio    = y[:, best["start"]:best["end"]]   # stereo
        fade_samples = min(sr * 4, seg_audio.shape[1])   # 4s de crossfade

        # Fade out no final do segmento de repetição
        fade_out = np.linspace(1.0, 0.0, fade_samples)
        seg_faded = seg_audio.copy()
        seg_faded[:, -fade_samples:] *= fade_out

        # Fade in no início do segmento de repetição
        fade_in = np.linspace(0.0, 1.0, fade_samples)
        seg_faded[:, :fade_samples] *= fade_in

        # Quantas repetições para atingir o tempo extra desejado
        seg_duration = seg_audio.shape[1] / sr
        reps = max(1, int(np.ceil(target_extra_seconds / seg_duration)))
        extension = np.concatenate([seg_faded] * reps, axis=1)

        # Junta: original + extensão
        extended = np.concatenate([y, extension], axis=1)

        # 7. Salva WAV e converte para MP3
        extended_t = extended.T   # soundfile espera (samples, channels)
        sf.write(tmp_ext_wav, extended_t, sr, subtype="PCM_16")

        ret2 = os.system(
            f'ffmpeg -i "{tmp_ext_wav}" -acodec libmp3lame -ab 320k -ar 44100 -y "{output_mp3}" -loglevel quiet'
        )

        # Limpa temporários
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
        tracks.append({
            "id":       track_id,
            "name":     display_name,
            "artist":   info["artist"],
            "title":    info["title"],
            "timestamp": ts,
            "url":      f"/download/{job_id}/{fname}",
            "url_mp3":  f"/download/{job_id}/{fname}?format=mp3",
            "url_wav":  f"/download/{job_id}/{fname}?format=wav",
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

    if not os.path.exists(mp3_path):
        return Response(content='{"error":"Arquivo nao encontrado"}',
                        status_code=404, media_type="application/json")

    # Nome amigável
    display_name = base_filename.replace(".mp3", "")
    if job_id in jobs:
        tid = base_filename.replace(".mp3", "")
        for t in jobs[job_id].get("tracks", []):
            if t.get("id") == tid:
                display_name = safe_filename(t["name"])
                break

    # ── Versão estendida ──────────────────────────────────────────────────────
    if format == "extended":
        ext_path = mp3_path.replace(".mp3", "_extended.mp3")
        if not os.path.exists(ext_path):
            loop = asyncio.get_event_loop()
            ok   = await loop.run_in_executor(None, extend_track, mp3_path, ext_path, 60)
            if not ok:
                return Response(content='{"error":"Falha ao gerar versao estendida"}',
                                status_code=500, media_type="application/json")
        encoded = urllib.parse.quote(display_name + "_extended.mp3")
        return FileResponse(ext_path, media_type="audio/mpeg", headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
            "Access-Control-Allow-Origin": "*",
            "Accept-Ranges": "bytes",
        })

    # ── WAV ───────────────────────────────────────────────────────────────────
    if format == "wav":
        wav_path = mp3_path.replace(".mp3", ".wav")
        if not os.path.exists(wav_path):
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-i", mp3_path, "-acodec", "pcm_s16le",
                "-ar", "44100", "-ac", "2", "-y", wav_path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            await proc.communicate()
        if not os.path.exists(wav_path):
            return Response(content='{"error":"Falha ao converter para WAV"}',
                            status_code=500, media_type="application/json")
        encoded = urllib.parse.quote(display_name + ".wav")
        return FileResponse(wav_path, media_type="audio/wav", headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
            "Access-Control-Allow-Origin": "*",
            "Accept-Ranges": "bytes",
        })

    # ── MP3 / stream (player) ─────────────────────────────────────────────────
    encoded     = urllib.parse.quote(display_name + ".mp3")
    disposition = "inline" if format == "stream" else f"attachment; filename*=UTF-8''{encoded}"
    return FileResponse(mp3_path, media_type="audio/mpeg", headers={
        "Content-Disposition": disposition,
        "Content-Length":      str(os.path.getsize(mp3_path)),
        "Access-Control-Allow-Origin": "*",
        "Accept-Ranges": "bytes",
    })
