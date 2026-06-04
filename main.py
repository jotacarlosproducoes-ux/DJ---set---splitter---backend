from fastapi import FastAPI, UploadFile, File, Form, Request, BackgroundTasks
from fastapi.responses import FileResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
import uuid, os, shutil, asyncio, requests, hmac, hashlib, base64, time, urllib.parse
import sys
import numpy as np
import boto3
from botocore.config import Config
from pydantic import BaseModel
from concurrent.futures import ThreadPoolExecutor

# Força stdout/stderr sem buffer — garante que os logs apareçam em tempo real
# no Render (sem isso, prints dentro de threads podem nunca aparecer).
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# Pool de threads para o processamento pesado.
# librosa, numpy, scipy e ffmpeg liberam o GIL durante operações pesadas,
# então uma thread separada permite que o servidor web continue respondendo
# ao health check do Render (evitando reinício do container no meio do job).
# max_workers=2 → até 2 sets processando ao mesmo tempo.
_THREAD_POOL = None

def get_pool() -> ThreadPoolExecutor:
    global _THREAD_POOL
    if _THREAD_POOL is None:
        _THREAD_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="processor")
    return _THREAD_POOL

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

JOBS_DIR = "jobs"
os.makedirs(JOBS_DIR, exist_ok=True)

def job_set(job_id: str, data: dict):
    import json
    try:
        with open(os.path.join(JOBS_DIR, f"{job_id}.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"[JOB] Erro ao salvar: {e}")

def job_get(job_id: str) -> dict | None:
    import json
    try:
        path = os.path.join(JOBS_DIR, f"{job_id}.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"[JOB] Erro ao ler: {e}")
    return None

def get_r2():
    if not R2_ACCESS_KEY_ID or not R2_ENDPOINT:
        return None
    return boto3.client("s3", endpoint_url=R2_ENDPOINT,
                        aws_access_key_id=R2_ACCESS_KEY_ID,
                        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
                        config=Config(signature_version="s3v4"), region_name="auto")

def upload_to_r2(local_path: str, r2_key: str) -> bool:
    try:
        s3 = get_r2()
        if not s3: return False
        s3.upload_file(local_path, R2_BUCKET, r2_key)
        print(f"[R2] Upload OK: {r2_key}")
        return True
    except Exception as e:
        print(f"[R2] Upload erro: {e}")
        return False

def download_from_r2(r2_key: str, local_path: str) -> bool:
    try:
        s3 = get_r2()
        if not s3: return False
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


# ══════════════════════════════════════════════════════════════════════════════
# IDENTIFICAÇÃO — ACRCloud + AudD (só para nomear as faixas)
# ══════════════════════════════════════════════════════════════════════════════
def identify_with_audd(audio_path: str) -> dict | None:
    if not AUDD_API_TOKEN: return None
    try:
        with open(audio_path, "rb") as f:
            resp = requests.post("https://api.audd.io/",
                                 data={"api_token": AUDD_API_TOKEN, "return": "apple_music,spotify"},
                                 files={"file": f}, timeout=15)
        r = resp.json()
        if r.get("status") == "success" and r.get("result"):
            return {"title": r["result"].get("title", "Desconhecida"),
                    "artist": r["result"].get("artist", "Desconhecido")}
    except Exception as e:
        print(f"AudD error: {e}")
    return None

def identify_song(audio_path: str, timestamp: float) -> dict:
    try:
        dur = float(os.popen(
            f'ffprobe -v error -show_entries format=duration '
            f'-of default=noprint_wrappers=1:nokey=1 "{audio_path}"').read().strip() or 180)
        sample_start = max(0, dur / 2 - 10)
        sample_path  = audio_path + "_sample.mp3"
        os.system(f'ffmpeg -ss {sample_start} -t 20 -i "{audio_path}" -y "{sample_path}" -loglevel quiet')
        sample_file = sample_path if os.path.exists(sample_path) else audio_path

        with open(sample_file, "rb") as f:
            sample_bytes = os.path.getsize(sample_file)
            sample_data  = f.read()

        ts   = time.time()
        stts = f"POST\n/v1/identify\n{ACR_ACCESS_KEY}\naudio\n1\n{ts}"
        sign = base64.b64encode(
            hmac.new(ACR_ACCESS_SECRET.encode(), stts.encode(), hashlib.sha1).digest()).decode()

        resp   = requests.post(ACR_REQURL, timeout=15,
                               files=[('sample', ('sample.mp3', sample_data, 'audio/mpeg'))],
                               data={'access_key': ACR_ACCESS_KEY, 'sample_bytes': sample_bytes,
                                     'timestamp': str(ts), 'signature': sign,
                                     'data_type': 'audio', 'signature_version': '1'})
        result = resp.json()
        if os.path.exists(sample_path): os.remove(sample_path)
        if result.get("status", {}).get("code") == 0:
            music = result["metadata"]["music"][0]
            return {"title":  music.get("title", "Desconhecida"),
                    "artist": music.get("artists", [{}])[0].get("name", "Desconhecido")}
    except Exception as e:
        print(f"ACRCloud error: {e}")

    audd = identify_with_audd(audio_path)
    if audd: return audd
    return {"title": f"Faixa {int(timestamp)}s", "artist": "Desconhecido"}


# ══════════════════════════════════════════════════════════════════════════════
# ANÁLISE DE ÁUDIO — Funções auxiliares de baixo nível
# ══════════════════════════════════════════════════════════════════════════════
def _load_audio(path: str, sr: int = 22050, offset: float = 0.0,
                duration: float | None = None) -> tuple[np.ndarray, int]:
    """Carrega áudio com librosa de forma segura."""
    import librosa
    y, sr_out = librosa.load(path, sr=sr, mono=True, offset=offset,
                             duration=duration, res_type="kaiser_fast")
    return y, sr_out

def _bandpass_energy(y: np.ndarray, sr: int, hop: int,
                     fmin: float, fmax: float) -> np.ndarray:
    """Energia de uma banda de frequência específica, frame a frame."""
    import librosa
    from scipy.ndimage import uniform_filter1d
    n_fft = 2048
    stft  = np.abs(librosa.stft(y, n_fft=n_fft, hop_length=hop))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    bins  = np.where((freqs >= fmin) & (freqs <= fmax))[0]
    energy = stft[bins, :].mean(axis=0).astype(float)
    return uniform_filter1d(energy, size=8)

def _multiband_energy(y: np.ndarray, sr: int, hop: int, bands: list) -> list:
    """
    Calcula a energia de VÁRIAS bandas com UM ÚNICO STFT (economiza memória/CPU).
    Essencial para sets longos — evita recalcular o STFT por banda.
    bands: lista de tuplas (fmin, fmax). Retorna lista de arrays na mesma ordem.
    """
    import librosa
    from scipy.ndimage import uniform_filter1d
    n_fft = 2048
    stft  = np.abs(librosa.stft(y, n_fft=n_fft, hop_length=hop))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    results = []
    for fmin, fmax in bands:
        bins = np.where((freqs >= fmin) & (freqs <= fmax))[0]
        if len(bins) == 0:
            results.append(np.zeros(stft.shape[1]))
        else:
            energy = stft[bins, :].mean(axis=0).astype(float)
            results.append(uniform_filter1d(energy, size=8))
    del stft  # libera o STFT imediatamente
    return results

def _normalize(x: np.ndarray, smooth: int = 10) -> np.ndarray:
    from scipy.ndimage import uniform_filter1d
    x = uniform_filter1d(x.astype(float), size=smooth)
    mn, mx = x.min(), x.max()
    return (x - mn) / (mx - mn + 1e-9)

def _get_duration(path: str) -> float:
    result = os.popen(
        f'ffprobe -v error -show_entries format=duration '
        f'-of default=noprint_wrappers=1:nokey=1 "{path}"').read().strip()
    return float(result) if result else 0.0


# ══════════════════════════════════════════════════════════════════════════════
# DETECÇÃO DE TRANSIÇÕES
# Estratégia profissional em 4 camadas:
#
# Camada 1 — Sub-bass (60-150Hz): o bassline é o marcador mais forte
#            de qual música está tocando em House/Techno/Trance
# Camada 2 — Midrange (200-2000Hz): melodia e harmonia — muda ao trocar música
# Camada 3 — High (4k-16kHz): hi-hats e textura — padrão rítmico muda na mix
# Camada 4 — MFCC distance: distância tímbrica global entre janelas de 60s
#            (o mesmo método que o Shazam usa internamente para comparar áudios)
#
# O score final é a combinação das 4 camadas com pesos específicos para DJ sets.
# ══════════════════════════════════════════════════════════════════════════════
def detect_transitions(audio_path: str, min_track_duration: float = 60.0) -> list[float]:
    import tempfile
    tmp_wav = None
    try:
        import librosa
        import gc
        from scipy.ndimage import uniform_filter1d
        from scipy.signal import find_peaks

        # SR baixo (11025) para economizar memória em sets longos de 2-3h.
        # Suficiente para detectar transições (mudanças de baixa/média frequência).
        SR  = 11025
        HOP = int(SR * 0.5)   # 1 frame = 0.5s

        # PRÉ-CONVERSÃO com ffmpeg: converte para WAV mono no SR alvo ANTES de
        # carregar no librosa. O ffmpeg processa em streaming (memória constante),
        # evitando o pico de RAM que o librosa.load causa ao resamplear 3h de áudio.
        tmp_wav = tempfile.mktemp(suffix=".wav")
        print(f"[DETECT] Pré-convertendo com ffmpeg (SR={SR} mono)...", flush=True)
        import subprocess
        conv = subprocess.run(
            ["ffmpeg", "-i", audio_path, "-ac", "1", "-ar", str(SR),
             "-y", tmp_wav, "-loglevel", "error"],
            capture_output=True, text=True, timeout=600
        )
        if conv.returncode != 0 or not os.path.exists(tmp_wav):
            print(f"[DETECT] ffmpeg falhou, usando arquivo original. {conv.stderr[-200:]}", flush=True)
            load_path = audio_path
        else:
            load_path = tmp_wav
            print(f"[DETECT] Conversão OK", flush=True)

        print(f"[DETECT] Carregando áudio (SR={SR})...", flush=True)
        # Como já está no SR certo e mono, o load é direto (sem resample pesado)
        y, sr = librosa.load(load_path, sr=SR, mono=True, res_type="kaiser_fast")
        total = len(y) / sr
        print(f"[DETECT] Duração: {round(total/60, 1)} min — {len(y)} amostras", flush=True)

        if total < min_track_duration * 2:
            return []

        # ── Camadas 1-3: Bass, Mid, High com UM ÚNICO STFT (economia de RAM) ─
        bass, mid, high = _multiband_energy(y, sr, HOP, [
            (60, 150),      # sub-bass
            (200, 2000),    # midrange
            (3000, 5000),   # high (limitado pelo Nyquist de 5512Hz)
        ])
        print(f"[DETECT] Bandas (bass/mid/high) OK", flush=True)

        # ── Camada 4: MFCC tímbrico (13 coeficientes) ────────────────────────
        mfcc = librosa.feature.mfcc(y=y, sr=sr, hop_length=HOP, n_mfcc=13)
        print(f"[DETECT] MFCC OK", flush=True)

        # Libera o array de áudio bruto — não é mais necessário para as features
        # (só o beat tracking usa, e fazemos isso já já)
        # Mantém uma cópia leve só para beat tracking
        y_for_beats = y
        gc.collect()

        n = min(len(bass), len(mid), len(high), mfcc.shape[1])
        bass = _normalize(bass[:n])
        mid  = _normalize(mid[:n])
        high = _normalize(high[:n])

        # ── Distância MFCC entre janelas de 60s ──────────────────────────────
        # Para cada frame, compara o centróide MFCC dos 60s anteriores
        # com os 60s seguintes — uma mudança grande = nova música
        win60 = int(60 / 0.5)
        mfcc_change = np.zeros(n)
        for i in range(win60, n - win60):
            prev_mean = mfcc[:, i - win60:i].mean(axis=1)
            next_mean = mfcc[:, i:i + win60].mean(axis=1)
            mfcc_change[i] = float(np.linalg.norm(next_mean - prev_mean))

        mfcc_change = _normalize(mfcc_change, smooth=6)

        # ── Distância de bass entre janelas de 30s e 60s ─────────────────────
        bass_change = np.zeros(n)
        for win_sec in [30, 60]:
            wf = int(win_sec / 0.5)
            ch = np.zeros(n)
            for i in range(wf, n - wf):
                ch[i] = abs(bass[i - wf:i].mean() - bass[i:i + wf].mean())
            bass_change += _normalize(ch, smooth=4)
        bass_change = _normalize(bass_change, smooth=4)

        # ── Score final: pesos otimizados para House/Techno/Trance ────────────
        # Bass e MFCC são os sinais mais confiáveis para esse estilo
        score = (bass_change * 0.40 +
                 mfcc_change * 0.35 +
                 _normalize(mid[:n], smooth=8) * 0.15 +
                 _normalize(high[:n], smooth=8) * 0.10)
        score = _normalize(score, smooth=4)

        # ── Threshold adaptativo (percentil 72) ──────────────────────────────
        threshold = float(np.clip(np.percentile(score, 72), 0.22, 0.58))
        min_dist  = int(min_track_duration / 0.5)
        print(f"[DETECT] Threshold: {round(threshold, 3)}")

        peaks, _ = find_peaks(score, height=threshold, distance=min_dist, prominence=0.08)

        # Segunda tentativa com threshold menor se necessário
        if len(peaks) == 0:
            threshold2 = float(np.percentile(score, 55))
            print(f"[DETECT] Re-tentando com threshold {round(threshold2, 3)}")
            peaks, _ = find_peaks(score, height=threshold2, distance=min_dist, prominence=0.05)

        frame_times = librosa.frames_to_time(np.arange(n), sr=sr, hop_length=HOP)
        print(f"[DETECT] {len(peaks)} picos encontrados")

        if len(peaks) == 0:
            print("[DETECT] Fallback: 3.5 min por faixa")
            out, t = [], 210.0
            while t < total - min_track_duration:
                out.append(round(t, 1)); t += 210.0
            return out

        # ── Beat tracking para alinhar ao compasso ───────────────────────────
        tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, hop_length=HOP)
        beat_times  = librosa.frames_to_time(beat_frames, sr=sr, hop_length=HOP)
        bar_times   = beat_times[::4] if len(beat_times) > 4 else beat_times
        tempo_val   = float(np.array(tempo).flatten()[0])
        print(f"[DETECT] BPM: {round(tempo_val, 1)}")

        transitions = []
        for p in peaks:
            t_peak = float(frame_times[min(int(p), n - 1)])

            # Refina: dentro de ±45s do pico, acha onde MFCC tem mínima variância
            # = ponto mais "estável" = nova música já dominou
            f0 = max(0, int(p) - int(45/0.5))
            f1 = min(n, int(p) + int(45/0.5))
            t_cut = t_peak
            if f1 - f0 > 8:
                vw = int(8/0.5)
                if f1 - vw > f0:
                    local_var = np.array([mfcc[:, i:i+vw].var() for i in range(f0, f1-vw)])
                    best = f0 + int(np.argmin(local_var)) + vw // 2
                    t_cut = float(frame_times[min(best, n-1)])

            t_cut = max(min_track_duration, min(total - min_track_duration, t_cut))

            # Alinha ao início de compasso mais próximo (dentro de 4s)
            if len(bar_times) > 0:
                diffs = np.abs(bar_times - t_cut)
                idx   = int(np.argmin(diffs))
                if diffs[idx] < 4.0:
                    t_cut = float(bar_times[idx])

            transitions.append(round(t_cut, 2))
            print(f"[DETECT] Transição @ {round(t_cut/60, 2)} min")

        # Remove duplicatas
        transitions.sort()
        out = [transitions[0]]
        for t in transitions[1:]:
            if t - out[-1] >= min_track_duration:
                out.append(t)

        print(f"[DETECT] {len(out)} transições finais: {[round(t/60,2) for t in out]} min")
        return out

    except Exception as e:
        print(f"[DETECT] Erro: {e}")
        import traceback; traceback.print_exc()
        try:
            total = _get_duration(audio_path)
            out, t = [], 210.0
            while t < total - 60:
                out.append(round(t, 1)); t += 210.0
            return out
        except:
            return []
    finally:
        # Remove o WAV temporário da pré-conversão
        if tmp_wav and os.path.exists(tmp_wav):
            try: os.remove(tmp_wav)
            except: pass


# ══════════════════════════════════════════════════════════════════════════════
# TRIM CIRÚRGICO — Remove zona de mix com precisão de compasso
#
# Objetivo: cada faixa deve soar como baixada do Spotify/SoundCloud
#
# Técnica usada:
# 1. Analisa sub-bass (60-150Hz) para detectar quando o bassline
#    da música muda — o marcador mais forte de troca em House/Techno
# 2. Analisa MFCC em janelas de 5s para encontrar o ponto de
#    MÁXIMA ESTABILIDADE tímbrica — onde só uma música está tocando
# 3. Alinha ao beat mais próximo para corte limpo
# ══════════════════════════════════════════════════════════════════════════════
def find_clean_start(audio_path: str, search_seconds: float = 90.0) -> float:
    """
    Encontra onde a nova música começa limpa (sem a anterior por baixo).
    Retorna o offset em segundos a partir do início do arquivo.
    """
    try:
        import librosa
        SR  = 22050
        HOP = int(SR * 0.25)   # 0.25s por frame

        y, sr = _load_audio(audio_path, sr=SR, duration=search_seconds)
        if len(y) < SR * 4: return 0.0

        # Bass energy
        bass     = _bandpass_energy(y, sr, HOP, 60, 150)
        n        = len(bass)
        bass_max = bass.max()
        if bass_max == 0: return 0.0
        bass_n   = bass / bass_max

        # MFCC para detectar estabilidade tímbrica
        mfcc    = librosa.feature.mfcc(y=y, sr=sr, hop_length=HOP, n_mfcc=13)
        n       = min(n, mfcc.shape[1])
        bass_n  = bass_n[:n]

        # Janela de 5s: calcula variância do MFCC
        win5    = int(5.0 / 0.25)
        # Janela de 2s: verifica tendência do bass
        win2    = int(2.0 / 0.25)

        # Procura o primeiro ponto onde:
        # 1. Bass está em pelo menos 45% do máximo (música está tocando forte)
        # 2. Bass está crescendo ou estável (não ainda na mistura decrescente)
        # 3. MFCC tem baixa variância nos próximos 5s (som estável = uma música só)
        frame_times = librosa.frames_to_time(np.arange(n), sr=sr, hop_length=HOP)

        # Calcula variância MFCC por janela
        mfcc_var = np.array([
            mfcc[:, i:i+win5].var() if i + win5 <= n else 999.0
            for i in range(n)
        ])
        mfcc_var_norm = mfcc_var / (mfcc_var.max() + 1e-9)

        best_t    = 0.0
        best_score = -1.0

        for i in range(win2, n - win5):
            if bass_n[i] < 0.45: continue
            bass_trend = bass_n[i:i+win2].mean() - bass_n[max(0,i-win2):i].mean()
            if bass_trend < -0.05: continue   # bass caindo = ainda na mix anterior
            stability = 1.0 - mfcc_var_norm[i]
            score = bass_n[i] * 0.4 + stability * 0.6
            if score > best_score:
                best_score = score
                best_t = float(frame_times[i])

        if best_t > 0:
            # Recua 1 compasso (≈2s a 120bpm) para não cortar o ataque
            result = max(0.0, best_t - 2.0)
            print(f"[TRIM] Início limpo @ {round(result, 1)}s (score={round(best_score, 3)})")
            return round(result, 2)

        return 0.0
    except Exception as e:
        print(f"[TRIM] find_clean_start erro: {e}")
        return 0.0


def find_clean_end(audio_path: str, search_seconds: float = 90.0) -> float:
    """
    Encontra onde a música atual termina limpa (antes da próxima entrar forte).
    Retorna a duração em segundos a partir do início do arquivo.
    """
    try:
        import librosa
        SR  = 22050
        HOP = int(SR * 0.25)

        total_dur = _get_duration(audio_path)
        if total_dur == 0: return 0.0

        offset = max(0.0, total_dur - search_seconds)
        y, sr  = _load_audio(audio_path, sr=SR, offset=offset)
        if len(y) < SR * 4: return total_dur

        bass     = _bandpass_energy(y, sr, HOP, 60, 150)
        n        = len(bass)
        bass_max = bass.max()
        if bass_max == 0: return total_dur
        bass_n   = bass / bass_max

        mfcc    = librosa.feature.mfcc(y=y, sr=sr, hop_length=HOP, n_mfcc=13)
        n       = min(n, mfcc.shape[1])
        bass_n  = bass_n[:n]

        win5 = int(5.0 / 0.25)
        win2 = int(2.0 / 0.25)

        mfcc_var = np.array([
            mfcc[:, i:i+win5].var() if i + win5 <= n else 999.0
            for i in range(n)
        ])
        mfcc_var_norm = mfcc_var / (mfcc_var.max() + 1e-9)

        frame_times = librosa.frames_to_time(np.arange(n), sr=sr, hop_length=HOP)

        # Varre de trás para frente: procura o último ponto onde
        # 1. Bass ainda está forte (>= 45%)
        # 2. Bass não está crescendo (não é a próxima música chegando)
        # 3. MFCC é estável (uma música só)
        best_t     = total_dur
        best_score = -1.0

        for i in range(n - win5 - 1, win2, -1):
            if bass_n[i] < 0.45: continue
            bass_trend = bass_n[i:i+win2].mean() - bass_n[max(0,i-win2):i].mean()
            if bass_trend > 0.05: continue   # bass subindo = próxima música chegando
            stability = 1.0 - mfcc_var_norm[i]
            score = bass_n[i] * 0.4 + stability * 0.6
            if score > best_score:
                best_score = score
                best_t = offset + float(frame_times[i])

        if best_score > 0:
            # Adiciona 1 compasso para pegar o decay natural do bass
            result = min(best_t + 2.0, total_dur)
            print(f"[TRIM] Fim limpo @ {round(result, 1)}s de {round(total_dur, 1)}s (score={round(best_score, 3)})")
            return round(result, 2)

        return total_dur
    except Exception as e:
        print(f"[TRIM] find_clean_end erro: {e}")
        return total_dur


# ══════════════════════════════════════════════════════════════════════════════
# EXTENSÃO DE FAIXA com Demucs
# ══════════════════════════════════════════════════════════════════════════════
def extend_track(input_mp3: str, output_mp3: str, target_extra_seconds: int = 60) -> bool:
    import tempfile, subprocess
    tmp_dir = tempfile.mkdtemp()
    tmp_wav = os.path.join(tmp_dir, "input.wav")
    try:
        import librosa, soundfile as sf
        ret = os.system(f'ffmpeg -i "{input_mp3}" -ar 44100 -ac 2 -y "{tmp_wav}" -loglevel quiet')
        if ret != 0: raise Exception("MP3→WAV falhou")

        y_full, sr = librosa.load(tmp_wav, sr=44100, mono=False)
        if y_full.ndim == 1: y_full = np.stack([y_full, y_full])

        y_mono = librosa.to_mono(y_full)
        tempo, beats = librosa.beat.beat_track(y=y_mono, sr=sr)
        beat_samples = [int(x) for x in librosa.frames_to_samples(np.array(beats).flatten())]
        if len(beat_samples) < 8:
            beat_samples = list(range(0, y_mono.shape[0], int(sr * 2)))

        drums_audio = None
        try:
            stems_dir = os.path.join(tmp_dir, "stems")
            os.makedirs(stems_dir, exist_ok=True)
            subprocess.run(["python", "-m", "demucs", "-n", "htdemucs",
                            "--four-stems", "-o", stems_dir, tmp_wav],
                           capture_output=True, text=True, timeout=400)
            drums_path = os.path.join(stems_dir, "htdemucs", "input", "drums.wav")
            if os.path.exists(drums_path):
                drums_audio, _ = librosa.load(drums_path, sr=44100, mono=False)
                if drums_audio.ndim == 1: drums_audio = np.stack([drums_audio, drums_audio])
                min_len = min(y_full.shape[1], drums_audio.shape[1])
                y_full = y_full[:, :min_len]
                drums_audio = drums_audio[:, :min_len]
        except Exception as e:
            print(f"[EXTEND] Demucs erro: {e}")

        if drums_audio is None:
            from scipy.signal import butter, sosfilt
            def highpass(data):
                sos = butter(4, 200/(44100/2), btype='high', output='sos')
                return sosfilt(sos, data)
            drums_audio = np.stack([highpass(y_full[0]), highpass(y_full[1])])

        beats_needed = 64
        intro_len = int(beat_samples[beats_needed]) - int(beat_samples[0]) if len(beat_samples) > beats_needed else int(sr*30)
        outro_len = int(beat_samples[-1]) - int(beat_samples[-(beats_needed+1)]) if len(beat_samples) > beats_needed else int(sr*30)
        intro_len = min(intro_len, int(sr*35), drums_audio.shape[1]//3)
        outro_len = min(outro_len, int(sr*35), drums_audio.shape[1]//3)

        mid_start = max(0, int(beat_samples[len(beat_samples)//2]))
        drums_loop = drums_audio[:, mid_start:mid_start + max(intro_len, outro_len)]

        fade_s = min(int(sr*2), intro_len//4, outro_len//4)
        f_in   = np.linspace(0.0, 1.0, fade_s)
        f_out  = np.linspace(1.0, 0.0, fade_s)

        intro = drums_loop[:, :intro_len].copy()
        intro[:, -fade_s:] = intro[:, -fade_s:] * f_out + y_full[:, :fade_s] * f_in

        outro = drums_loop[:, :outro_len].copy()
        outro[:, :fade_s] = y_full[:, -fade_s:] * f_out + outro[:, :fade_s] * f_in
        outro[:, -min(int(sr*4), outro_len):] *= np.linspace(1.0, 0.0, min(int(sr*4), outro_len))

        extended = np.concatenate([intro[:, :-fade_s], y_full, outro[:, fade_s:]], axis=1)
        peak = np.max(np.abs(extended))
        if peak > 0.95: extended = extended * (0.95 / peak)

        combined = os.path.join(tmp_dir, "combined.wav")
        sf.write(combined, extended.T, sr, subtype="PCM_16")
        ret2 = os.system(f'ffmpeg -i "{combined}" -acodec libmp3lame -ab 320k -ar 44100 -y "{output_mp3}" -loglevel quiet')
        return ret2 == 0 and os.path.exists(output_mp3)
    except Exception as e:
        print(f"[EXTEND] Erro: {e}")
        return False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
# PIPELINE PRINCIPAL
# ══════════════════════════════════════════════════════════════════════════════
def process_job(job_id: str, input_path: str, folder: str):
    print(f"[JOB] >>> process_job INICIADO para {job_id}", flush=True)
    try:
        job_set(job_id, {"status": "processing", "tracks": [], "progress": 2,
                         "stage": "Iniciando análise espectral..."})

        print(f"[JOB] Lendo duração de {input_path}...", flush=True)
        total_duration = _get_duration(input_path)
        if total_duration == 0:
            raise Exception("Não foi possível determinar a duração do arquivo")
        print(f"[JOB] Duração total: {round(total_duration/60, 1)} min", flush=True)

        # ── Etapa 1: Detecção de transições ──────────────────────────────────
        job_set(job_id, {"status": "processing", "tracks": [], "progress": 5,
                         "stage": "Analisando espectro — Bass + MFCC + Timbre (1-3 min)..."})

        transitions = detect_transitions(input_path, min_track_duration=60.0)
        print(f"[JOB] {len(transitions)} transições → {len(transitions)+1} faixas")

        job_set(job_id, {"status": "processing", "tracks": [], "progress": 35,
                         "stage": f"{len(transitions)} transições detectadas. Aplicando trim cirúrgico..."})

        boundaries = [0.0] + transitions + [total_duration]
        segments   = [(boundaries[i], boundaries[i+1]) for i in range(len(boundaries)-1)]
        tracks     = []

        for i, (raw_start, raw_end) in enumerate(segments):
            raw_dur = raw_end - raw_start
            if raw_dur < 60:
                print(f"[JOB] Segmento {i} muito curto ({round(raw_dur)}s), ignorando")
                continue

            progress = 35 + int((i / len(segments)) * 50)
            job_set(job_id, {"status": "processing", "tracks": tracks, "progress": progress,
                             "stage": f"Processando faixa {i+1}/{len(segments)}..."})

            # ── Extrai segmento bruto com margem extra para o trim ────────────
            # Adiciona 10s de margem em cada lado para o trim ter material
            margin     = 10.0
            ext_start  = max(0.0, raw_start - margin)
            ext_end    = min(total_duration, raw_end + margin)
            ext_dur    = ext_end - ext_start

            raw_path = os.path.join(folder, f"raw_{i:03d}.mp3")
            ret = os.system(
                f'ffmpeg -ss {ext_start} -t {ext_dur} -i "{input_path}" '
                f'-vn -acodec mp3 -ab 320k -ar 44100 -y "{raw_path}" -loglevel quiet')
            if ret != 0 or not os.path.exists(raw_path):
                print(f"[JOB] Erro ao extrair segmento {i}")
                continue

            # ── Trim cirúrgico ────────────────────────────────────────────────
            trim_start = 0.0
            trim_end   = ext_dur

            if i > 0:
                detected = find_clean_start(raw_path, search_seconds=min(90.0, ext_dur * 0.6))
                if 0.5 <= detected <= ext_dur * 0.5:
                    trim_start = detected
                    print(f"[JOB] Faixa {i}: trim início = +{round(trim_start,1)}s")
                else:
                    # Fallback: pula a margem que adicionamos
                    trim_start = margin
                    print(f"[JOB] Faixa {i}: trim início fallback = +{round(trim_start,1)}s")

            if i < len(segments) - 1:
                detected = find_clean_end(raw_path, search_seconds=min(90.0, ext_dur * 0.6))
                if detected > ext_dur * 0.5 and detected < ext_dur - 0.5:
                    trim_end = detected
                    print(f"[JOB] Faixa {i}: trim fim = {round(trim_end,1)}s de {round(ext_dur,1)}s")
                else:
                    # Fallback: remove a margem que adicionamos
                    trim_end = ext_dur - margin
                    print(f"[JOB] Faixa {i}: trim fim fallback = {round(trim_end,1)}s")

            clean_dur = trim_end - trim_start
            if clean_dur < 45:
                print(f"[JOB] Faixa {i} muito curta após trim ({round(clean_dur)}s), ignorando")
                os.remove(raw_path)
                continue

            # ── Exporta faixa limpa ───────────────────────────────────────────
            fname      = f"track_{i:03d}.mp3"
            track_path = os.path.join(folder, fname)
            ret2 = os.system(
                f'ffmpeg -ss {trim_start} -t {clean_dur} -i "{raw_path}" '
                f'-vn -acodec mp3 -ab 320k -ar 44100 -y "{track_path}" -loglevel quiet')
            os.remove(raw_path)

            if ret2 != 0 or not os.path.exists(track_path):
                print(f"[JOB] Erro ao exportar faixa {i}")
                continue

            real_start = ext_start + trim_start

            # ── Identifica (nomeia) ───────────────────────────────────────────
            info   = identify_song(track_path, real_start)
            artist = info["artist"]
            title  = info["title"]
            name   = f"{artist} - {title}"
            print(f"[JOB] ✓ Faixa {i+1}: {round(real_start/60,1)}min | {round(clean_dur/60,1)}min | {name}")

            # ── Upload R2 ─────────────────────────────────────────────────────
            upload_to_r2(track_path, f"{job_id}/{fname}")

            tracks.append({
                "id":           fname.replace(".mp3", ""),
                "name":         name,
                "artist":       artist,
                "title":        title,
                "timestamp":    int(real_start),
                "duration":     round(clean_dur, 1),
                "url":          f"/download/{job_id}/{fname}",
                "url_mp3":      f"/download/{job_id}/{fname}?format=mp3",
                "url_wav":      f"/download/{job_id}/{fname}?format=wav",
                "url_extended": f"/download/{job_id}/{fname}?format=extended",
            })
            job_set(job_id, {"status": "processing", "tracks": tracks, "progress": progress,
                             "stage": f"✓ {name}"})

        job_set(job_id, {"status": "done", "tracks": tracks, "progress": 100,
                         "stage": f"Concluído — {len(tracks)} faixas extraídas!"})
        print(f"[JOB] {job_id} concluído — {len(tracks)} faixas")

    except Exception as e:
        print(f"[JOB] Erro fatal: {e}")
        import traceback; traceback.print_exc()
        job_set(job_id, {"status": "error", "error": str(e), "tracks": []})


# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/")
def root():
    return {"status": "DJ Set Splitter API", "r2": bool(R2_ACCESS_KEY_ID)}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/upload-chunk")
async def upload_chunk(background_tasks: BackgroundTasks,
                       chunk: UploadFile = File(...),
                       upload_id: str = Form(...),
                       chunk_index: int = Form(...),
                       total_chunks: int = Form(...),
                       filename: str = Form(...)):
    chunks_folder = f"uploads/{upload_id}"
    os.makedirs(chunks_folder, exist_ok=True)
    chunk_path = f"{chunks_folder}/chunk_{chunk_index:04d}"
    with open(chunk_path, "wb") as f:
        shutil.copyfileobj(chunk.file, f)

    received = len([f for f in os.listdir(chunks_folder) if f.startswith("chunk_")])
    if received < total_chunks:
        return {"status": "uploading", "received": received, "total": total_chunks}

    job_id = str(uuid.uuid4())
    folder = f"outputs/{job_id}"
    os.makedirs(folder, exist_ok=True)
    input_path = f"{folder}/input.mp3"

    with open(input_path, "wb") as out:
        for i in range(total_chunks):
            with open(f"{chunks_folder}/chunk_{i:04d}", "rb") as cf:
                shutil.copyfileobj(cf, out)

    shutil.rmtree(chunks_folder, ignore_errors=True)
    job_set(job_id, {"status": "processing", "tracks": [], "progress": 0,
                     "stage": "Arquivo recebido, iniciando análise..."})
    # Roda em processo separado para não bloquear o servidor web
    get_pool().submit(process_job, job_id, input_path, folder)
    return {"status": "done", "job_id": job_id}

@app.post("/split")
async def split_audio(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    job_id = str(uuid.uuid4())
    folder = f"outputs/{job_id}"
    os.makedirs(folder, exist_ok=True)
    input_path = folder + "/input.mp3"
    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    job_set(job_id, {"status": "processing", "tracks": [], "progress": 0})
    get_pool().submit(process_job, job_id, input_path, folder)
    return {"job_id": job_id, "status": "processing"}


# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINT: Processar a partir de URL (YouTube / SoundCloud)
# ══════════════════════════════════════════════════════════════════════════════
class URLRequest(BaseModel):
    url: str

def download_and_process(job_id: str, url: str, folder: str):
    """Baixa o áudio da URL com yt-dlp e dispara o pipeline de processamento."""
    import subprocess
    try:
        job_set(job_id, {"status": "processing", "tracks": [], "progress": 1,
                         "stage": "Baixando áudio da URL..."})

        input_path = os.path.join(folder, "input.mp3")

        # Caminho de cookies opcional (ajuda a contornar bloqueio do YouTube)
        cookies_file = os.environ.get("YTDLP_COOKIES", "")

        # Monta argumentos como lista (mais seguro que string em thread)
        cmd = ["yt-dlp"]
        if cookies_file and os.path.exists(cookies_file):
            cmd += ["--cookies", cookies_file]
        cmd += [
            "-f", "bestaudio/best",
            "-N", "8",
            "--extract-audio", "--audio-format", "mp3", "--audio-quality", "0",
            "--no-playlist", "--no-warnings",
            "-o", f"{folder}/input.%(ext)s",
            url,
        ]

        print(f"[URL] Baixando: {url}", flush=True)
        # subprocess.run é seguro em threads (os.system pode travar com signals)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        ret = result.returncode
        if ret != 0:
            print(f"[URL] yt-dlp stderr: {result.stderr[-500:]}", flush=True)

        # yt-dlp pode salvar como input.mp3 diretamente
        if not os.path.exists(input_path):
            for f in os.listdir(folder):
                if f.startswith("input."):
                    found = os.path.join(folder, f)
                    if not f.endswith(".mp3"):
                        subprocess.run(
                            ["ffmpeg", "-i", found, "-acodec", "mp3", "-ab", "320k",
                             "-y", input_path, "-loglevel", "quiet"],
                            capture_output=True, timeout=300)
                        if os.path.exists(found): os.remove(found)
                    else:
                        input_path = found
                    break

        if not os.path.exists(input_path):
            raise Exception("Falha ao baixar o áudio da URL. O link pode estar bloqueado ou ser privado.")

        size_mb = os.path.getsize(input_path) / (1024 * 1024)
        print(f"[URL] Download OK — {round(size_mb, 1)}MB", flush=True)

        # Dispara o pipeline normal
        process_job(job_id, input_path, folder)

    except Exception as e:
        print(f"[URL] Erro: {e}")
        import traceback; traceback.print_exc()
        job_set(job_id, {"status": "error", "error": str(e), "tracks": []})


@app.post("/split-url")
async def split_from_url(background_tasks: BackgroundTasks, req: URLRequest):
    """Recebe uma URL de YouTube ou SoundCloud e processa o áudio."""
    url = req.url.strip()

    # Validação básica
    valid_domains = ["youtube.com", "youtu.be", "soundcloud.com", "m.soundcloud.com"]
    if not any(d in url.lower() for d in valid_domains):
        return Response(
            content='{"error":"URL invalida. Use YouTube ou SoundCloud."}',
            status_code=400, media_type="application/json"
        )

    job_id = str(uuid.uuid4())
    folder = f"outputs/{job_id}"
    os.makedirs(folder, exist_ok=True)

    job_set(job_id, {"status": "processing", "tracks": [], "progress": 0,
                     "stage": "Iniciando download da URL..."})
    get_pool().submit(download_and_process, job_id, url, folder)

    print(f"[URL] Job {job_id} iniciado para {url}")
    return {"job_id": job_id, "status": "processing"}


@app.get("/status/{job_id}")
def get_status(job_id: str):
    data = job_get(job_id)
    if not data: return {"error": "Job nao encontrado"}
    return data

@app.get("/download/{job_id}/{filename}")
async def download_track(job_id: str, filename: str, format: str = "stream"):
    base_filename = filename if filename.endswith(".mp3") else filename + ".mp3"
    mp3_path      = f"outputs/{job_id}/{base_filename}"
    r2_key        = f"{job_id}/{base_filename}"

    if not os.path.exists(mp3_path):
        if not download_from_r2(r2_key, mp3_path):
            return Response(content='{"error":"Arquivo nao encontrado"}',
                            status_code=404, media_type="application/json")

    display_name = base_filename.replace(".mp3", "")
    data = job_get(job_id)
    if data:
        for t in data.get("tracks", []):
            if t.get("id") == base_filename.replace(".mp3", ""):
                display_name = safe_filename(t["name"]); break

    if format == "extended":
        ext_path  = mp3_path.replace(".mp3", "_extended.mp3")
        ext_r2key = r2_key.replace(".mp3", "_extended.mp3")
        if not os.path.exists(ext_path): download_from_r2(ext_r2key, ext_path)
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
            "Access-Control-Allow-Origin": "*", "Accept-Ranges": "bytes"})

    if format == "wav":
        wav_path  = mp3_path.replace(".mp3", ".wav")
        wav_r2key = r2_key.replace(".mp3", ".wav")
        if not os.path.exists(wav_path): download_from_r2(wav_r2key, wav_path)
        if not os.path.exists(wav_path):
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-i", mp3_path, "-acodec", "pcm_s16le", "-ar", "44100",
                "-ac", "2", "-y", wav_path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await proc.communicate()
            upload_to_r2(wav_path, wav_r2key)
        if not os.path.exists(wav_path):
            return Response(content='{"error":"Falha ao converter para WAV"}',
                            status_code=500, media_type="application/json")
        encoded = urllib.parse.quote(display_name + ".wav")
        return FileResponse(wav_path, media_type="audio/wav", headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
            "Access-Control-Allow-Origin": "*", "Accept-Ranges": "bytes"})

    encoded     = urllib.parse.quote(display_name + ".mp3")
    disposition = "inline" if format == "stream" else f"attachment; filename*=UTF-8''{encoded}"
    return FileResponse(mp3_path, media_type="audio/mpeg", headers={
        "Content-Disposition": disposition,
        "Content-Length": str(os.path.getsize(mp3_path)),
        "Access-Control-Allow-Origin": "*", "Accept-Ranges": "bytes"})


# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINT: Trim manual — aplica corte ajustado pelo usuário
# ══════════════════════════════════════════════════════════════════════════════
class TrimRequest(BaseModel):
    trim_start: float   # segundos a partir do início da faixa original
    trim_end:   float   # segundos a partir do início da faixa original

@app.post("/trim/{job_id}/{track_id}")
async def apply_manual_trim(job_id: str, track_id: str, req: TrimRequest):
    """
    Reaplicar o corte de uma faixa com os offsets ajustados pelo usuário.
    Regera o MP3 com os novos pontos de início e fim.
    """
    fname      = f"{track_id}.mp3"
    track_path = f"outputs/{job_id}/{fname}"
    r2_key     = f"{job_id}/{fname}"

    # Baixa do R2 se não estiver local
    if not os.path.exists(track_path):
        if not download_from_r2(r2_key, track_path):
            return Response(content='{"error":"Faixa nao encontrada"}',
                            status_code=404, media_type="application/json")

    dur = _get_duration(track_path)
    if dur == 0:
        return Response(content='{"error":"Nao foi possivel ler a duracao"}',
                        status_code=400, media_type="application/json")

    t_start = clamp_val(req.trim_start, 0.0, dur - 1.0)
    t_end   = clamp_val(req.trim_end,   t_start + 1.0, dur)
    t_dur   = t_end - t_start

    if t_dur < 10:
        return Response(content='{"error":"Duracao muito curta (minimo 10s)"}',
                        status_code=400, media_type="application/json")

    # Gera arquivo trimado
    trimmed_path = track_path.replace(".mp3", "_trimmed.mp3")
    ret = os.system(
        f'ffmpeg -ss {t_start} -t {t_dur} -i "{track_path}" '
        f'-vn -acodec mp3 -ab 320k -ar 44100 -y "{trimmed_path}" -loglevel quiet'
    )
    if ret != 0 or not os.path.exists(trimmed_path):
        return Response(content='{"error":"Falha ao aplicar trim"}',
                        status_code=500, media_type="application/json")

    # Substitui o arquivo original pelo trimado
    shutil.move(trimmed_path, track_path)

    # Re-upload para R2
    upload_to_r2(track_path, r2_key)

    # Atualiza o job com nova duração
    data = job_get(job_id)
    if data:
        for t in data.get("tracks", []):
            if t.get("id") == track_id:
                t["duration"]   = round(t_dur, 1)
                t["trimStart"]  = t_start
                t["trimEnd"]    = t_end
        job_set(job_id, data)

    print(f"[TRIM] Manual: {track_id} → {round(t_start,1)}s–{round(t_end,1)}s ({round(t_dur,1)}s)")
    return {"status": "ok", "duration": round(t_dur, 1),
            "trim_start": t_start, "trim_end": t_end}


def clamp_val(v: float, mn: float, mx: float) -> float:
    return max(mn, min(mx, v))
