# app/routes/files.py
import os, json
from fastapi import APIRouter

router = APIRouter()
UPLOAD_DIR = "app/data/uploads"
META_FILE = os.path.join(UPLOAD_DIR, "files.json")

@router.get("/files")
def get_files():
    if not os.path.exists(META_FILE):
        return []
    with open(META_FILE, "r", encoding="utf-8") as f:
        return json.load(f)
