from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import uuid, os, shutil
from pydub import AudioSegment
from pydub.silence import split_on_silence

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

jobs = {}

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

    # Salva o arquivo enviado
    input_path = f"outputs/{job_id}/input_{file.filename}"
    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Carrega e divide por silêncio
    audio = AudioSegment.from_file(input_path)
    chunks = split_on_silence(
        audio,
        min_silence_len=1500,
        silence_thresh=audio.dBFS - 16,
        keep_silence=500
    )

    # Se não dividiu (sem silêncio), divide em partes iguais de 3 min
    if len(chunks) <= 1:
        chunk_len = 3 * 60 * 1000
        chunks = [audio[i:i+chunk_len] for i in range(0, len(audio), chunk_len)]

    # Exporta cada faixa
    tracks = []
    for i, chunk in enumerate(chunks):
        track_name = f"track_{i+1:02d}.mp3"
        track_path = f"outputs/{job_id}/{track_name}"
        chunk.export(track_path, format="mp3")
        tracks.append({"name": track_name, "url": f"/download/{job_id}/{track_name}"})

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
