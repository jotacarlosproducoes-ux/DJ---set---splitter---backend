from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import uuid, os, shutil, subprocess

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

    input_path = f"outputs/{job_id}/input{os.path.splitext(file.filename)[1]}"
    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Divide em partes de 3 minutos usando ffmpeg direto
    output_pattern = f"outputs/{job_id}/track_%03d.mp3"
    subprocess.run([
        "ffmpeg", "-i", input_path,
        "-f", "segment", "-segment_time", "180",
        "-c:a", "libmp3lame", "-q:a", "2", output_pattern
    ], check=True)

    tracks = []
    for fname in sorted(os.listdir(f"outputs/{job_id}")):
        if fname.startswith("track_") and fname.endswith(".mp3"):
            tracks.append({
                "name": fname,
                "url": f"/download/{job_id}/{fname}"
            })

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
