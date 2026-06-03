from fastapi import FastAPI, UploadFile, File, Form, Request, BackgroundTasks
from fastapi.responses import FileResponse, Response
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
AUDD_API_TOKEN    = os.environ.get("AUDD_API_TOKEN", "")

R2_ACCESS_KEY_ID     = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_ENDPOINT          = os.environ.get("R2_ENDPOINT", "")
R2_BUCKET            = os.environ.get("R2_BUCKET", "djsetsplitter")

# ─── Job storage ─────────────────────────────────────────────────────────────
JOBS_DIR = "jobs"
os.makedirs(JOBS_DIR, exist_ok=True)

def job_set(job_id: str, data: dict):
    import json
    try:
        path = os.path.join(JOBS_DIR, f"{job_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"[JOB] Erro ao salvar job: {e}")

def job_get(job_id: str) -> dict | None:
    import json
    try:
        path = os.path.join(JOBS_DIR, f"{job_id}.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"[JOB] Erro ao ler job: {e}")
    return None

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

def safe_filename(name: str) -> str:
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip()


# ─── Identificação de música (só para nomear, não para cortar) ───────────────
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
        if result.get("status") == "success" and result.get("result"):
            r = result["result"]
            return {"title": r.get("title", "Desconhecida"),
                    "artist": r.get("artist", "Desconhecido")}
    except Exception as e:
        print(f"AudD error: {e}")
    return None

def identify_song(audio_path: str, timestamp: float) -> dict:
    """Identifica uma música usando ACRCloud + AudD fallback."""
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
        print(f"ACRCloud: {result.get('status', {}).get('msg')} @ {round(timestamp)}s")
        if os.path.exists(sample_path):
            os.remove(sample_path)
        if result.get("status", {}).get("code") == 0:
            music = result["metadata"]["music"][0]
            return {"title":  music.get("title", "Desconhecida"),
                    "artist": music.get("artists", [{}])[0].get("name", "Desconhecido")}
    except Exception as e:
        print(f"ACRCloud error: {e}")

    audd = identify_with_audd(audio_path)
    if audd:
        return audd

    return {"title": f"Faixa {int(timestamp)}s", "artist": "Desconhecido"}


# ─── DETECÇÃO DE TRANSIÇÕES — Análise espectral pura ─────────────────────────
def detect_transitions_spectral(audio_path: str, min_track_duration: float = 60.0) -> list[float]:
    """
    Detecta transições de músicas em um DJ set usando análise espectral pura.
    Usa múltiplas janelas de comparação e thresholds adaptativos.
    """
    try:
        import librosa
        from scipy.ndimage import uniform_filter1d
        from scipy.signal import find_peaks

        SR  = 11025
        HOP = int(SR * 0.5)   # 1 frame = 0.5s
        WIN = int(SR * 2)

        print(f"[DETECT] Carregando {audio_path}...")
        y, sr = librosa.load(audio_path, sr=SR, mono=True, res_type="kaiser_fast")
        total_duration = len(y) / sr
        print(f"[DETECT] Duração: {round(total_duration/60, 1)} min")

        if total_duration < min_track_duration * 2:
            print("[DETECT] Áudio muito curto para ter transições")
            return []

        # ── 1. Features espectrais ────────────────────────────────────────────
        rms      = librosa.feature.rms(y=y, hop_length=HOP, frame_length=WIN)[0]
        centroid = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=HOP, n_fft=WIN)[0]
        rolloff  = librosa.feature.spectral_rolloff(y=y, sr=sr, hop_length=HOP, n_fft=WIN)[0]
        zcr      = librosa.feature.zero_crossing_rate(y=y, hop_length=HOP, frame_length=WIN)[0]

        n_frames = min(len(rms), len(centroid), len(rolloff), len(zcr))
        rms      = rms[:n_frames].astype(float)
        centroid = centroid[:n_frames].astype(float)
        rolloff  = rolloff[:n_frames].astype(float)
        zcr      = zcr[:n_frames].astype(float)

        def smooth_normalize(x, smooth_frames=10):
            x = uniform_filter1d(x, size=smooth_frames)
            mn, mx = x.min(), x.max()
            return (x - mn) / (mx - mn + 1e-9)

        rms_n      = smooth_normalize(rms, smooth_frames=4)
        centroid_n = smooth_normalize(centroid)
        rolloff_n  = smooth_normalize(rolloff)
        zcr_n      = smooth_normalize(zcr)

        # ── 2. Distância espectral em múltiplas escalas de janela ─────────────
        # Usa 3 tamanhos de janela — captura transições lentas e rápidas
        all_changes = []
        for win_sec in [30, 60, 90]:
            wf = int(win_sec / 0.5)
            change = np.zeros(n_frames)
            for i in range(wf, n_frames - wf):
                prev = np.array([
                    centroid_n[i - wf:i].mean(),
                    rolloff_n[i - wf:i].mean(),
                    zcr_n[i - wf:i].mean(),
                ])
                nxt = np.array([
                    centroid_n[i:i + wf].mean(),
                    rolloff_n[i:i + wf].mean(),
                    zcr_n[i:i + wf].mean(),
                ])
                change[i] = float(np.linalg.norm(nxt - prev))
            all_changes.append(smooth_normalize(change, smooth_frames=6))

        # Combina as 3 escalas com peso maior para janela média (60s)
        spectral_change = (all_changes[0] * 0.25 + all_changes[1] * 0.5 + all_changes[2] * 0.25)
        spectral_change = smooth_normalize(spectral_change, smooth_frames=4)

        # ── 3. Score de transição ─────────────────────────────────────────────
        # Zona de mix = mudança espectral alta + RMS não está no silêncio
        rms_active = np.where(rms_n > 0.1, 1.0, 0.0)   # exclui silêncios
        transition_score = spectral_change * rms_active
        transition_score = smooth_normalize(transition_score, smooth_frames=4)

        # ── 4. Threshold ADAPTATIVO ───────────────────────────────────────────
        # Em vez de threshold fixo, usa percentil do próprio sinal
        # assim funciona independente da intensidade das mudanças
        adaptive_threshold = float(np.percentile(transition_score, 70))
        adaptive_threshold = max(0.20, min(adaptive_threshold, 0.55))
        print(f"[DETECT] Threshold adaptativo: {round(adaptive_threshold, 3)}")

        min_dist_frames = int(min_track_duration / 0.5)

        peaks, props = find_peaks(
            transition_score,
            height=adaptive_threshold,
            distance=min_dist_frames,
            prominence=0.08,   # baixo — não filtra demais
        )

        frame_times = librosa.frames_to_time(np.arange(n_frames), sr=sr, hop_length=HOP)
        print(f"[DETECT] {len(peaks)} picos encontrados com threshold {round(adaptive_threshold, 3)}")

        # ── 5. Se ainda não achou nada, tenta com threshold menor ─────────────
        if len(peaks) == 0:
            fallback_threshold = float(np.percentile(transition_score, 50))
            print(f"[DETECT] Tentando threshold menor: {round(fallback_threshold, 3)}")
            peaks, _ = find_peaks(
                transition_score,
                height=fallback_threshold,
                distance=min_dist_frames,
                prominence=0.05,
            )
            print(f"[DETECT] {len(peaks)} picos com threshold reduzido")

        # ── 6. Se ainda zero, fallback por tempo ──────────────────────────────
        if len(peaks) == 0:
            print("[DETECT] Sem transições detectadas — fallback 3.5min")
            fallback = []
            t = 210.0
            while t < total_duration - min_track_duration:
                fallback.append(round(t, 1))
                t += 210.0
            return fallback

        # ── 7. Beat tracking para alinhar cortes ──────────────────────────────
        tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, hop_length=HOP)
        beat_times = librosa.frames_to_time(beat_frames, sr=sr, hop_length=HOP)
        tempo_val  = float(np.array(tempo).flatten()[0])
        print(f"[DETECT] BPM: {round(tempo_val, 1)}")

        transitions = []
        for peak_idx in peaks:
            t_peak = float(frame_times[min(int(peak_idx), n_frames - 1)])

            # Ponto de corte = pico de mudança espectral
            # Refina buscando mínima variância local (±30s)
            f_start = max(0, int(peak_idx) - int(30 / 0.5))
            f_end   = min(n_frames, int(peak_idx) + int(30 / 0.5))
            t_cut   = t_peak

            if f_end - f_start > 8:
                var_window = int(10 / 0.5)
                if f_end - var_window > f_start:
                    local_var = np.array([
                        centroid_n[i:i+var_window].var()
                        for i in range(f_start, f_end - var_window)
                    ])
                    min_var_idx = f_start + int(np.argmin(local_var)) + var_window // 2
                    t_cut = float(frame_times[min(min_var_idx, n_frames - 1)])

            t_cut = max(min_track_duration, min(total_duration - min_track_duration, t_cut))

            # Alinha ao beat mais próximo (dentro de 4s)
            if len(beat_times) > 0:
                diffs = np.abs(beat_times - t_cut)
                closest_idx = int(np.argmin(diffs))
                if diffs[closest_idx] < 4.0:
                    bar_beats = beat_times[::4] if len(beat_times) > 4 else beat_times
                    diffs_bar = np.abs(bar_beats - t_cut)
                    closest_bar = int(np.argmin(diffs_bar))
                    if diffs_bar[closest_bar] < 4.0:
                        t_cut = float(bar_beats[closest_bar])
                    else:
                        t_cut = float(beat_times[closest_idx])

            transitions.append(round(t_cut, 2))
            print(f"[DETECT] Transição @ {round(t_cut/60, 2)} min")

        # Remove duplicatas muito próximas
        transitions.sort()
        filtered = [transitions[0]]
        for t in transitions[1:]:
            if t - filtered[-1] >= min_track_duration:
                filtered.append(t)

        print(f"[DETECT] {len(filtered)} transições finais: {[round(t/60, 2) for t in filtered]} min")
        return filtered

    except Exception as e:
        print(f"[DETECT] Erro fatal: {e}")
        import traceback; traceback.print_exc()
        try:
            result = os.popen(
                f'ffprobe -v error -show_entries format=duration '
                f'-of default=noprint_wrappers=1:nokey=1 "{audio_path}"'
            ).read().strip()
            duration = float(result) if result else 3600
            fallback = []
            t = 210.0
            while t < duration - 60:
                fallback.append(round(t, 1))
                t += 210.0
            return fallback
        except:
            return []


# ─── Trim inteligente — remove zona de mix do início e fim ──────────────────
def find_clean_start(audio_path: str, search_seconds: float = 30.0) -> float:
    """
    Encontra o ponto exato onde a música começa "limpa":
    onde a energia começa a ser estável e o espectro convergiu.
    Retorna o offset em segundos para usar no corte.
    """
    try:
        import librosa
        from scipy.ndimage import uniform_filter1d

        SR  = 11025
        HOP = int(SR * 0.25)   # 0.25s por frame — mais fino para trim

        y, sr = librosa.load(audio_path, sr=SR, mono=True,
                             duration=search_seconds, res_type="kaiser_fast")
        if len(y) < SR * 2:
            return 0.0

        rms      = librosa.feature.rms(y=y, hop_length=HOP)[0]
        centroid = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=HOP)[0]

        n = min(len(rms), len(centroid))
        rms      = rms[:n]
        centroid = centroid[:n]

        rms_smooth = uniform_filter1d(rms.astype(float), size=8)
        rms_max    = rms_smooth.max()
        if rms_max == 0:
            return 0.0

        # Ponto onde RMS atinge 40% do máximo e está subindo
        threshold = rms_max * 0.40
        for i in range(1, n - 1):
            if rms_smooth[i] >= threshold and rms_smooth[i] > rms_smooth[i-1]:
                t = float(librosa.frames_to_time(i, sr=sr, hop_length=HOP))
                # Vai 0.5s antes para não cortar o ataque
                return max(0.0, round(t - 0.5, 2))

        return 0.0
    except:
        return 0.0

def find_clean_end(audio_path: str, search_seconds: float = 30.0) -> float:
    """
    Encontra o ponto exato onde a música termina "limpa":
    onde a energia começa a cair para a zona de mix do próximo.
    Retorna a duração limpa (em segundos a partir do início do arquivo).
    """
    try:
        import librosa
        from scipy.ndimage import uniform_filter1d

        SR  = 11025
        HOP = int(SR * 0.25)

        # Carrega total para descobrir duração
        duration_result = os.popen(
            f'ffprobe -v error -show_entries format=duration '
            f'-of default=noprint_wrappers=1:nokey=1 "{audio_path}"'
        ).read().strip()
        total_dur = float(duration_result) if duration_result else 0
        if total_dur == 0:
            return 0.0

        # Carrega só os últimos search_seconds
        offset = max(0, total_dur - search_seconds)
        y, sr  = librosa.load(audio_path, sr=SR, mono=True,
                              offset=offset, res_type="kaiser_fast")
        if len(y) < SR * 2:
            return total_dur

        rms        = librosa.feature.rms(y=y, hop_length=HOP)[0]
        rms_smooth = uniform_filter1d(rms.astype(float), size=8)
        rms_max    = rms_smooth.max()
        if rms_max == 0:
            return total_dur

        # Procura o ponto (de trás para frente) onde RMS ainda está em 40% do máximo
        threshold = rms_max * 0.40
        n = len(rms_smooth)
        for i in range(n - 2, 0, -1):
            if rms_smooth[i] >= threshold:
                t_local = float(librosa.frames_to_time(i, sr=sr, hop_length=HOP))
                # +0.5s para não cortar o decay
                t_abs = offset + t_local + 0.5
                return round(min(t_abs, total_dur), 2)

        return total_dur
    except:
        return 0.0


# ─── Extensão de faixa com Demucs ────────────────────────────────────────────
def extend_track(input_mp3: str, output_mp3: str, target_extra_seconds: int = 60) -> bool:
    import tempfile
    tmp_dir = tempfile.mkdtemp()
    tmp_wav = os.path.join(tmp_dir, "input.wav")

    try:
        import librosa
        import soundfile as sf
        import subprocess

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
        print(f"[EXTEND] BPM: {round(tempo_val, 1)}")

        if len(beat_samples) < 8:
            seg_len = int(sr * 2)
            beat_samples = list(range(0, y_mono.shape[0], seg_len))

        drums_audio = None
        try:
            stems_dir = os.path.join(tmp_dir, "stems")
            os.makedirs(stems_dir, exist_ok=True)
            result = subprocess.run(
                ["python", "-m", "demucs", "-n", "htdemucs", "--four-stems", "-o", stems_dir, tmp_wav],
                capture_output=True, text=True, timeout=400
            )
            drums_path = os.path.join(stems_dir, "htdemucs", "input", "drums.wav")
            if os.path.exists(drums_path):
                drums_audio, _ = librosa.load(drums_path, sr=44100, mono=False)
                if drums_audio.ndim == 1:
                    drums_audio = np.stack([drums_audio, drums_audio])
                min_len = min(y_full.shape[1], drums_audio.shape[1])
                y_full = y_full[:, :min_len]
                drums_audio = drums_audio[:, :min_len]
        except Exception as e:
            print(f"[EXTEND] Demucs erro: {e}")

        if drums_audio is None:
            from scipy.signal import butter, sosfilt
            def highpass(data, cutoff=200, fs=44100, order=4):
                sos = butter(order, cutoff / (fs/2), btype='high', output='sos')
                return sosfilt(sos, data)
            drums_audio = np.stack([highpass(y_full[0]), highpass(y_full[1])])

        beats_per_bar = 4
        bars = 16
        beats_needed = bars * beats_per_bar

        if len(beat_samples) > beats_needed:
            intro_len = int(beat_samples[beats_needed]) - int(beat_samples[0])
            outro_len = int(beat_samples[-1]) - int(beat_samples[-(beats_needed+1)])
        else:
            intro_len = int(sr * 30)
            outro_len = int(sr * 30)

        intro_len = min(intro_len, int(sr * 35), drums_audio.shape[1] // 3)
        outro_len = min(outro_len, int(sr * 35), drums_audio.shape[1] // 3)

        mid_start = int(beat_samples[len(beat_samples)//2]) if len(beat_samples) > beats_needed else drums_audio.shape[1] // 2
        mid_start = max(0, mid_start)
        drums_loop = drums_audio[:, mid_start:mid_start + max(intro_len, outro_len)]

        fade_samples = min(int(sr * 2), intro_len // 4, outro_len // 4)
        f_in  = np.linspace(0.0, 1.0, fade_samples)
        f_out = np.linspace(1.0, 0.0, fade_samples)

        intro_drum = drums_loop[:, :intro_len].copy()
        intro_drum[:, -fade_samples:] = (
            intro_drum[:, -fade_samples:] * f_out + y_full[:, :fade_samples] * f_in
        )

        outro_drum = drums_loop[:, :outro_len].copy()
        outro_drum[:, :fade_samples] = (
            y_full[:, -fade_samples:] * f_out + outro_drum[:, :fade_samples] * f_in
        )
        final_fade = min(int(sr * 4), outro_len)
        outro_drum[:, -final_fade:] *= np.linspace(1.0, 0.0, final_fade)

        extended = np.concatenate([
            intro_drum[:, :-fade_samples],
            y_full,
            outro_drum[:, fade_samples:]
        ], axis=1)

        peak = np.max(np.abs(extended))
        if peak > 0.95:
            extended = extended * (0.95 / peak)

        combined_path = os.path.join(tmp_dir, "combined.wav")
        sf.write(combined_path, extended.T, sr, subtype="PCM_16")
        ret2 = os.system(
            f'ffmpeg -i "{combined_path}" -acodec libmp3lame -ab 320k -ar 44100 -y "{output_mp3}" -loglevel quiet'
        )

        total = extended.shape[1] / sr
        print(f"[EXTEND] OK — {round(total, 1)}s total")
        return ret2 == 0 and os.path.exists(output_mp3)

    except Exception as e:
        print(f"[EXTEND] Erro fatal: {e}")
        import traceback; traceback.print_exc()
        return False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ─── Processamento em background ─────────────────────────────────────────────
def process_job(job_id: str, input_path: str, folder: str):
    """
    Pipeline completo:
    1. Análise espectral → detecta transições (SEM APIs externas)
    2. Corta as faixas nos pontos de dominância
    3. Trim fino em cada faixa (remove zona de mix do início e fim)
    4. Identifica cada faixa com ACRCloud + AudD (só para nomear)
    5. Exporta MP3 320k + upload R2
    """
    try:
        job_set(job_id, {"status": "processing", "tracks": [], "progress": 2,
                         "stage": "Iniciando análise espectral..."})

        # Duração total
        result = os.popen(
            f'ffprobe -v error -show_entries format=duration '
            f'-of default=noprint_wrappers=1:nokey=1 "{input_path}"'
        ).read().strip()
        total_duration = float(result) if result else 0
        if total_duration == 0:
            raise Exception("Não foi possível determinar a duração do arquivo")

        print(f"[JOB] Duração total: {round(total_duration/60, 1)} min")

        # ── ETAPA 1: Detecção de transições por espectro ─────────────────────
        job_set(job_id, {"status": "processing", "tracks": [], "progress": 5,
                         "stage": "Analisando espectro do set (pode levar 1-2 min)..."})

        transitions = detect_transitions_spectral(input_path, min_track_duration=60.0)

        n_tracks_expected = len(transitions) + 1
        print(f"[JOB] {len(transitions)} transições → {n_tracks_expected} faixas esperadas")

        job_set(job_id, {"status": "processing", "tracks": [], "progress": 30,
                         "stage": f"Transições detectadas: {len(transitions)}. Cortando faixas..."})

        # ── ETAPA 2: Define segmentos ────────────────────────────────────────
        boundaries = [0.0] + transitions + [total_duration]
        segments   = [(boundaries[i], boundaries[i+1]) for i in range(len(boundaries)-1)]

        tracks = []
        for i, (raw_start, raw_end) in enumerate(segments):
            raw_duration = raw_end - raw_start

            if raw_duration < 60:
                print(f"[JOB] Segmento {i} muito curto ({round(raw_duration)}s), ignorando")
                continue

            progress = 30 + int((i / len(segments)) * 40)
            job_set(job_id, {"status": "processing", "tracks": tracks, "progress": progress,
                             "stage": f"Processando faixa {i+1} de {len(segments)}..."})

            # ── ETAPA 3: Extrai segmento bruto ───────────────────────────────
            raw_path = os.path.join(folder, f"raw_{i:03d}.mp3")
            ret = os.system(
                f'ffmpeg -ss {raw_start} -t {raw_duration} -i "{input_path}" '
                f'-vn -acodec mp3 -ab 320k -ar 44100 -y "{raw_path}" -loglevel quiet'
            )
            if ret != 0 or not os.path.exists(raw_path):
                print(f"[JOB] Erro ao extrair segmento {i}")
                continue

            # ── ETAPA 4: Trim inteligente ─────────────────────────────────────
            # Para a primeira faixa, não trimamos o início
            # Para a última faixa, não trimamos o final
            trim_start_offset = 0.0
            trim_end_duration = raw_duration

            if i > 0:
                detected_start = find_clean_start(raw_path, search_seconds=30.0)
                if 1.0 <= detected_start <= 20.0:
                    trim_start_offset = detected_start
                    print(f"[JOB] Faixa {i}: trim início = +{trim_start_offset}s")
                else:
                    print(f"[JOB] Faixa {i}: trim início ignorado ({detected_start}s), usando 0s")

            if i < len(segments) - 1:
                detected_end = find_clean_end(raw_path, search_seconds=30.0)
                if detected_end > raw_duration * 0.5 and detected_end > 30.0:
                    trim_end_duration = detected_end
                    print(f"[JOB] Faixa {i}: trim fim = {trim_end_duration}s (de {round(raw_duration, 1)}s)")
                else:
                    print(f"[JOB] Faixa {i}: trim fim ignorado ({detected_end}s), usando {round(raw_duration,1)}s")

            clean_duration = trim_end_duration - trim_start_offset

            if clean_duration < 45:
                print(f"[JOB] Faixa {i} muito curta após trim ({round(clean_duration)}s), ignorando")
                os.remove(raw_path)
                continue

            # ── ETAPA 5: Exporta versão limpa ────────────────────────────────
            fname      = f"track_{i:03d}.mp3"
            track_path = os.path.join(folder, fname)

            ret2 = os.system(
                f'ffmpeg -ss {trim_start_offset} -t {clean_duration} -i "{raw_path}" '
                f'-vn -acodec mp3 -ab 320k -ar 44100 -y "{track_path}" -loglevel quiet'
            )
            os.remove(raw_path)   # limpa arquivo bruto

            if ret2 != 0 or not os.path.exists(track_path):
                print(f"[JOB] Erro ao exportar faixa limpa {i}")
                continue

            # Timestamp real (com trim aplicado)
            real_start = raw_start + trim_start_offset

            # ── ETAPA 6: Identifica a música (para nomear) ───────────────────
            info = identify_song(track_path, real_start)
            artist = info["artist"]
            title  = info["title"]
            display_name = f"{artist} - {title}"

            print(f"[JOB] Faixa {i+1}: {round(real_start/60, 1)}min | {round(clean_duration/60, 1)}min | {display_name}")

            # ── ETAPA 7: Upload R2 ────────────────────────────────────────────
            r2_key = f"{job_id}/{fname}"
            upload_to_r2(track_path, r2_key)

            tracks.append({
                "id":           fname.replace(".mp3", ""),
                "name":         display_name,
                "artist":       artist,
                "title":        title,
                "timestamp":    int(real_start),
                "duration":     round(clean_duration, 1),
                "url":          f"/download/{job_id}/{fname}",
                "url_mp3":      f"/download/{job_id}/{fname}?format=mp3",
                "url_wav":      f"/download/{job_id}/{fname}?format=wav",
                "url_extended": f"/download/{job_id}/{fname}?format=extended",
            })

            job_set(job_id, {"status": "processing", "tracks": tracks, "progress": progress,
                             "stage": f"Faixa {len(tracks)} identificada: {display_name}"})

        # Resultado final
        job_set(job_id, {
            "status":   "done",
            "tracks":   tracks,
            "progress": 100,
            "stage":    f"Concluído — {len(tracks)} faixas extraídas!"
        })
        print(f"[JOB] {job_id} concluído — {len(tracks)} faixas")

    except Exception as e:
        print(f"[JOB] Erro: {e}")
        import traceback; traceback.print_exc()
        job_set(job_id, {"status": "error", "error": str(e), "tracks": []})


# ─── Endpoints ────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "DJ Set Splitter API online", "r2": bool(R2_ACCESS_KEY_ID), "storage": "json"}

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/upload-chunk")
async def upload_chunk(
    background_tasks: BackgroundTasks,
    chunk: UploadFile = File(...),
    upload_id: str = Form(...),
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    filename: str = Form(...)
):
    chunks_folder = f"uploads/{upload_id}"
    os.makedirs(chunks_folder, exist_ok=True)

    chunk_path = f"{chunks_folder}/chunk_{chunk_index:04d}"
    with open(chunk_path, "wb") as f:
        shutil.copyfileobj(chunk.file, f)

    print(f"[CHUNK] {upload_id} — chunk {chunk_index+1}/{total_chunks} recebido")

    received = len([f for f in os.listdir(chunks_folder) if f.startswith("chunk_")])
    if received < total_chunks:
        return {"status": "uploading", "received": received, "total": total_chunks}

    job_id = str(uuid.uuid4())
    folder = f"outputs/{job_id}"
    os.makedirs(folder, exist_ok=True)

    input_path = f"{folder}/input.mp3"
    print(f"[CHUNK] Juntando {total_chunks} chunks em {input_path}")

    with open(input_path, "wb") as out:
        for i in range(total_chunks):
            chunk_file = f"{chunks_folder}/chunk_{i:04d}"
            with open(chunk_file, "rb") as cf:
                shutil.copyfileobj(cf, out)

    shutil.rmtree(chunks_folder, ignore_errors=True)

    job_set(job_id, {"status": "processing", "tracks": [], "progress": 0,
                     "stage": "Arquivo recebido, iniciando análise espectral..."})
    background_tasks.add_task(process_job, job_id, input_path, folder)

    print(f"[CHUNK] Upload completo — job {job_id} iniciado")
    return {"status": "done", "job_id": job_id}


@app.post("/split")
async def split_audio(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    job_id = str(uuid.uuid4())
    folder = "outputs/" + job_id
    os.makedirs(folder, exist_ok=True)

    input_path = folder + "/input.mp3"
    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

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

    display_name = base_filename.replace(".mp3", "")
    data = job_get(job_id)
    if data:
        for t in data.get("tracks", []):
            if t.get("id") == base_filename.replace(".mp3", ""):
                display_name = safe_filename(t["name"])
                break

    # ── Extended ──────────────────────────────────────────────────────────────
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
