from pathlib import Path
import os
from dotenv import load_dotenv
from typing import Set, Optional
from datetime import datetime, timezone
from dateutil import parser as dtparser
import subprocess

# Load .env from project root (same dir as this file)
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

DEFAULT_IMAGE_EXTS: Set[str] = {
    ".jpg",
    ".jpeg",
    ".png",
    ".tif",
    ".tiff",
    ".heic",
    ".webp",
    ".gif",
    ".bmp",
}

DEFAULT_VIDEO_EXTS: Set[str] = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".3gp", ".webm"}


def _parse_list(env_name: str, default: Set[str]) -> Set[str]:
    val = os.getenv(env_name, "")
    if not val:
        return default
    items = [s.strip() for s in val.split(",") if s.strip()]
    return set((s if s.startswith(".") else "." + s).lower() for s in items)


IMAGE_EXTS = _parse_list("IMAGE_EXTS", DEFAULT_IMAGE_EXTS)
VIDEO_EXTS = _parse_list("VIDEO_EXTS", DEFAULT_VIDEO_EXTS)


def run_cmd(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = p.communicate()
    return p.returncode, out.strip(), err.strip()


def to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    # Treat naive as UTC (Takeout timestamps are UTC)
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_exif_dt(val: Optional[str]) -> Optional[datetime]:
    if not val:
        return None

    v = str(val).strip()

    try:
        return datetime.strptime(v[:19], "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass

    try:
        return dtparser.parse(v)
    except Exception:
        return None
