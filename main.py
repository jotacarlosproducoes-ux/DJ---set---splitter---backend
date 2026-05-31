from fastapi import FastAPI, UploadFile, File, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import uuid, os, shutil

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://set-feiticeiro-ai.lovable.app",
        "http://localhost:5173",
        "http://localhost:8080",
        "*",
    ],
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
    jobs[job_id] = {"status": "processing", "tracks": []}
    # Salva o arquivo
    os.makedirs("uploads", exist_ok=True)
    path = f"uploads/{job_id}_{file.filename}"
    with open(path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    jobs[job_id]["status"] = "done"
    jobs[job_id]["file"] = path
    return {"job_id": job_id, "status": "processing"}

@app.get("/status/{job_id}")
def get_status(job_id: str):
    if job_id not in jobs:
        return {"error": "Job não encontrado"}
    return jobs[job_id]
