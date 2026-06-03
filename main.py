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

# ─── Job storage (arquivo JSON — simples e confiável) ────────────────────────
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

def get_redis():
    return None

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


# ─── Detecção inteligente de transições ─────────────────────────────────────
def detect_transitions(audio_path: str, min_track_duration: float = 120.0) -> list:
    """
    Detecta transições com precisão combinando 3 análises:
    1. RMS energy — detecta zona de mixagem (onde o DJ sobrepõe as músicas)
    2. Spectral flux — detecta mudança de timbre
    3. Beat alignment — alinha o corte exatamente no beat
    Retorna o ponto CENTRAL de cada zona de transição, alinhado ao beat.
    """
    try:
        import librosa
        from scipy.ndimage import uniform_filter1d
        from scipy.signal import find_peaks

        print("[SPLIT] Carregando áudio (mono 11025Hz para análise leve)...")
        # Carrega em baixa resolução — suficiente para detectar transições
        y, sr = librosa.load(audio_path, sr=11025, mono=True, res_type="kaiser_fast")
        duration = len(y) / sr
        print(f"[SPLIT] Duração: {round(duration/60, 1)} min")

        if duration < min_track_duration * 2:
            return []

        # hop_length grande = análise leve (1 frame ≈ 0.5s)
        hop_length = int(sr * 0.5)

        # 1. RMS — energia do sinal
        rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]

        # 2. Spectral flux — mudança espectral entre frames
        stft   = np.abs(librosa.stft(y, hop_length=hop_length, n_fft=512))
        flux   = np.sum(np.maximum(0, np.diff(stft, axis=1)), axis=0)
        min_len = min(len(rms), len(flux) + 1)
        rms    = rms[:min_len]
        flux_padded = np.pad(flux, (0, min_len - len(flux)))[:min_len]

        # 3. Normaliza os dois sinais
        def normalize(x):
            x = uniform_filter1d(x, size=max(1, int(10 / 0.5)))  # suaviza 10s
            mn, mx = x.min(), x.max()
            return (x - mn) / (mx - mn + 1e-9)

        rms_norm  = normalize(rms)
        flux_norm = normalize(flux_padded)

        # 4. Combina: transição = queda de RMS + pico de flux
        # Zona de transição = onde o DJ está mixando (RMS baixo + flux alto)
        transition_score = (1 - rms_norm) * 0.5 + flux_norm * 0.5

        # 5. Detecta zonas de transição (regiões acima do threshold)
        min_frames = int(min_track_duration / 0.5)
        peaks, props = find_peaks(
            transition_score,
            height=0.4,
            distance=min_frames,
            prominence=0.15,
            width=int(5 / 0.5)  # transição mínima de 5s
        )

        frame_times = librosa.frames_to_time(
            np.arange(len(transition_score)), sr=sr, hop_length=hop_length
        )

        # 6. Para cada pico, encontra o centro da zona de transição
        # e alinha ao beat mais próximo
        tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, hop_length=hop_length)
        beat_times = librosa.frames_to_time(beat_frames, sr=sr, hop_length=hop_length)
        tempo_val  = float(np.array(tempo).flatten()[0])
        print(f"[SPLIT] BPM: {round(tempo_val, 1)}")

        transitions = []
        for p in peaks:
            t = float(frame_times[p])

            # Encontra o beat mais próximo do pico
            if len(beat_times) > 0:
                closest_beat = beat_times[np.argmin(np.abs(beat_times - t))]
                # Só usa o beat se estiver a menos de 2s do pico
                if abs(closest_beat - t) < 2.0:
                    t = float(closest_beat)

            if t > min_track_duration and t < duration - min_track_duration:
                transitions.append(round(t, 2))

        # Fallback se não detectou nada
        if not transitions:
            print("[SPLIT] Sem transições — usando 3.5 min por faixa")
            t = 210.0
            while t < duration - min_track_duration:
                transitions.append(round(t, 1))
                t += 210.0

        print(f"[SPLIT] {len(transitions)} transições detectadas: {[round(t,1) for t in transitions]}")
        return transitions

    except Exception as e:
        print(f"[SPLIT] Erro: {e}")
        import traceback; traceback.print_exc()
        # Fallback seguro
        try:
            result = os.popen(
                f'ffprobe -v error -show_entries format=duration '
                f'-of default=noprint_wrappers=1:nokey=1 "{audio_path}"'
            ).read().strip()
            duration = float(result) if result else 3600
            transitions = []
            t = 210.0
            while t < duration - min_track_duration:
                transitions.append(round(t, 1))
                t += 210.0
            return transitions
        except:
            return []
def split_by_transitions(input_path: str, folder: str, transitions: list) -> list:
    """
    Divide o áudio nos pontos de transição detectados.
    Retorna lista de caminhos dos arquivos gerados.
    """
    import subprocess

    # Obtém duração total
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True, text=True
    )
    total_duration = float(result.stdout.strip()) if result.stdout.strip() else 0

    # Monta lista de segmentos: (start, end)
    boundaries = [0.0] + transitions + [total_duration]
    segments = [(boundaries[i], boundaries[i+1]) for i in range(len(boundaries)-1)]

    print(f"[SPLIT] Dividindo em {len(segments)} segmentos")

    track_paths = []
    for i, (start, end) in enumerate(segments):
        duration = end - start
        if duration < 30:  # ignora segmentos muito curtos
            print(f"[SPLIT] Segmento {i} muito curto ({round(duration,1)}s), ignorando")
            continue

        out_path = os.path.join(folder, f"track_{i:03d}.mp3")
        ret = os.system(
            f'ffmpeg -ss {start} -t {duration} -i "{input_path}" '
            f'-vn -acodec mp3 -ab 320k -ar 44100 -y "{out_path}" -loglevel quiet'
        )
        if ret == 0 and os.path.exists(out_path):
            track_paths.append(out_path)
            print(f"[SPLIT] Faixa {i+1}: {round(start/60,1)}min → {round(end/60,1)}min ({round(duration/60,1)}min)")

    return track_paths


# ─── Processamento em background ─────────────────────────────────────────────
def process_job(job_id: str, input_path: str, folder: str):
    """
    Divide o set usando identificação inteligente:
    1. Divide em segmentos de 30s
    2. Identifica cada segmento com ACRCloud + AudD
    3. Agrupa segmentos consecutivos da mesma música
    4. Corta nos pontos de mudança de música
    5. Salva no R2
    """
    try:
        job_set(job_id, {"status": "processing", "tracks": [], "progress": 0,
                         "stage": "Iniciando análise..."})

        # Duração total
        result = os.popen(
            f'ffprobe -v error -show_entries format=duration '
            f'-of default=noprint_wrappers=1:nokey=1 "{input_path}"'
        ).read().strip()
        total_duration = float(result) if result else 0
        if total_duration == 0:
            raise Exception("Não foi possível determinar a duração do arquivo")

        print(f"[JOB] Duração total: {round(total_duration/60, 1)} min")

        # 1. Divide em segmentos de 30s para identificação
        segment_duration = 30
        segments_folder = folder + "/segments"
        os.makedirs(segments_folder, exist_ok=True)

        job_set(job_id, {"status": "processing", "tracks": [], "progress": 5,
                         "stage": "Dividindo em segmentos para análise..."})

        os.system(
            f'ffmpeg -i "{input_path}" -f segment -segment_time {segment_duration} '
            f'-vn -acodec mp3 -ab 192k -ar 44100 -y "{segments_folder}/seg_%04d.mp3" -loglevel quiet'
        )

        segment_files = sorted(
            f for f in os.listdir(segments_folder) if f.startswith("seg_") and f.endswith(".mp3")
        )
        total_segs = len(segment_files)
        print(f"[JOB] {total_segs} segmentos de 30s para analisar")

        # 2. Identifica cada segmento
        seg_identities = []
        for i, seg_file in enumerate(segment_files):
            seg_path = os.path.join(segments_folder, seg_file)
            ts = i * segment_duration
            progress = 5 + int((i / total_segs) * 60)
            job_set(job_id, {"status": "processing", "tracks": [], "progress": progress,
                             "stage": f"Identificando segmento {i+1} de {total_segs}..."})

            info = identify_song(seg_path, ts)
            seg_identities.append({
                "index": i,
                "start": ts,
                "end": min(ts + segment_duration, total_duration),
                "artist": info["artist"],
                "title": info["title"],
                "known": info["artist"] != "Desconhecido"
            })
            print(f"[JOB] Seg {i+1}/{total_segs} @ {round(ts/60,1)}min: {info['artist']} - {info['title']}")

        # 3. Agrupa segmentos consecutivos da mesma música
        job_set(job_id, {"status": "processing", "tracks": [], "progress": 70,
                         "stage": "Agrupando faixas identificadas..."})

        def same_track(a, b):
            """Verifica se dois segmentos são da mesma música."""
            # Se ambos são desconhecidos, considera mesma faixa (zona de transição)
            if a["artist"] == "Desconhecido" and b["artist"] == "Desconhecido":
                return True
            # Se um é desconhecido, pode ser zona de mixagem — mantém no grupo atual
            if a["artist"] == "Desconhecido" or b["artist"] == "Desconhecido":
                return True  # tolerante: desconhecido = provavelmente mesma faixa
            return (a["artist"].lower() == b["artist"].lower() and
                    a["title"].lower() == b["title"].lower())

        # Monta grupos com lógica tolerante
        # Um grupo só muda quando encontra 2 segmentos CONHECIDOS E DIFERENTES consecutivos
        groups = []
        current_group = [seg_identities[0]]

        for i, seg in enumerate(seg_identities[1:], 1):
            prev = current_group[-1]
            
            # Mudança de música = ambos conhecidos E diferentes
            both_known = seg["known"] and prev["known"]
            different   = not same_track(seg, prev) if both_known else False
            
            # Confirma mudança: precisa de 2 segmentos diferentes consecutivos
            # para evitar falsos positivos
            if different and i + 1 < len(seg_identities):
                next_seg = seg_identities[i + 1] if i + 1 < len(seg_identities) else seg
                confirmed = (next_seg["known"] and 
                             next_seg["artist"].lower() == seg["artist"].lower())
                if confirmed:
                    groups.append(current_group)
                    current_group = [seg]
                    continue
            
            current_group.append(seg)
        
        groups.append(current_group)

        print(f"[JOB] {len(groups)} grupos de músicas detectados")

        # 4. Para cada grupo, determina o nome mais comum e os limites
        # Margem de 5s: remove a zona de mixagem do início e fim de cada faixa
        MARGIN = 5.0

        tracks = []
        for i, group in enumerate(groups):
            start_time = group[0]["start"]
            end_time   = group[-1]["end"]
            duration   = end_time - start_time

            # Ignora segmentos muito curtos (< 60s = provavelmente zona de mixagem)
            if duration < 60:
                print(f"[JOB] Grupo {i} muito curto ({round(duration)}s), ignorando")
                continue

            # Nome mais frequente no grupo (ignora desconhecidos)
            known = [s for s in group if s["known"]]
            if known:
                # Pega o nome do segmento do meio do grupo (mais limpo)
                mid_seg = known[len(known)//2]
                artist = mid_seg["artist"]
                title  = mid_seg["title"]
            else:
                artist = "Desconhecido"
                title  = f"Faixa {round(start_time)}s"

            display_name = artist + " - " + title
            fname        = f"track_{i:03d}.mp3"
            track_path   = os.path.join(folder, fname)

            # Aplica margem para remover zona de mixagem
            cut_start = max(0, start_time + MARGIN) if i > 0 else start_time
            cut_end   = min(total_duration, end_time - MARGIN) if i < len(groups)-1 else end_time
            cut_dur   = cut_end - cut_start

            if cut_dur < 30:
                print(f"[JOB] Faixa {i} muito curta após margem ({round(cut_dur)}s), ignorando")
                continue

            # Exporta o segmento
            ret = os.system(
                f'ffmpeg -ss {cut_start} -t {cut_dur} -i "{input_path}" '
                f'-vn -acodec mp3 -ab 320k -ar 44100 -y "{track_path}" -loglevel quiet'
            )

            if ret != 0 or not os.path.exists(track_path):
                print(f"[JOB] Erro ao exportar faixa {i}")
                continue

            # Upload para R2
            r2_key = f"{job_id}/{fname}"
            upload_to_r2(track_path, r2_key)

            tracks.append({
                "id":           fname.replace(".mp3", ""),
                "name":         display_name,
                "artist":       artist,
                "title":        title,
                "timestamp":    int(cut_start),
                "duration":     round(cut_dur, 1),
                "url":          f"/download/{job_id}/{fname}",
                "url_mp3":      f"/download/{job_id}/{fname}?format=mp3",
                "url_wav":      f"/download/{job_id}/{fname}?format=wav",
                "url_extended": f"/download/{job_id}/{fname}?format=extended",
            })

            progress = 70 + int((i / len(groups)) * 25)
            job_set(job_id, {"status": "processing", "tracks": tracks, "progress": progress,
                             "stage": f"Processando faixa {i+1} de {len(groups)}..."})

        # Limpa segmentos temporários
        shutil.rmtree(segments_folder, ignore_errors=True)

        job_set(job_id, {"status": "done", "tracks": tracks, "progress": 100,
                         "stage": f"Concluído — {len(tracks)} faixas identificadas!"})
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
    """
    Recebe chunks de arquivo e quando todos chegarem, inicia o processamento.
    Suporta internet lenta — cada chunk de 5MB tem 2 minutos de timeout.
    """
    # Pasta para os chunks deste upload
    chunks_folder = f"uploads/{upload_id}"
    os.makedirs(chunks_folder, exist_ok=True)

    # Salva o chunk
    chunk_path = f"{chunks_folder}/chunk_{chunk_index:04d}"
    with open(chunk_path, "wb") as f:
        shutil.copyfileobj(chunk.file, f)

    print(f"[CHUNK] {upload_id} — chunk {chunk_index+1}/{total_chunks} recebido")

    # Verifica se todos os chunks chegaram
    received = len([f for f in os.listdir(chunks_folder) if f.startswith("chunk_")])
    if received < total_chunks:
        return {"status": "uploading", "received": received, "total": total_chunks}

    # Todos os chunks chegaram — junta o arquivo
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

    # Limpa chunks
    shutil.rmtree(chunks_folder, ignore_errors=True)

    # Inicia processamento em background
    job_set(job_id, {"status": "processing", "tracks": [], "progress": 0,
                     "stage": "Arquivo recebido, iniciando análise..."})
    background_tasks.add_task(process_job, job_id, input_path, folder)

    print(f"[CHUNK] Upload completo — job {job_id} iniciado")
    return {"status": "done", "job_id": job_id}


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
