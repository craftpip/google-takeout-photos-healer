import json
import shutil
import sys
from pathlib import Path
import os
from dotenv import load_dotenv
from typing import Set, Optional, Dict
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
    ".dng",
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


def check_dependencies():
    tools = {
        "exiftool": ["exiftool", "-ver"],
        "ffmpeg": ["ffmpeg", "-version"],
        "ffprobe": ["ffprobe", "-version"],
    }

    for name, cmd in tools.items():
        code, _out, err = run_cmd(cmd)
        if code != 0:
            print(f"[FATAL] Missing or not working: {name}", file=sys.stderr)
            if err:
                print(f"stderr: {err}")
            sys.exit(1)


def parse_json(json_path: Path) -> Optional[Dict]:
    try:
        with json_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"__parse_error__": str(e)}


def _parse_any_dt(val) -> Optional[datetime]:
    if val is None:
        return None

    # plain unix timestamp (or string digits)
    if isinstance(val, (int, float)) or (isinstance(val, str) and val.strip().isdigit()):
        try:
            return datetime.fromtimestamp(int(val), tz=timezone.utc)
        except Exception:
            return None

    # arbitrary string
    if isinstance(val, str):
        try:
            return dtparser.parse(val)
        except Exception:
            return None

    return None


def move_preserve_structure(
        media: Path,
        root: str | Path,
        dest: str | Path,
        *,
        overwrite: bool = False,
) -> Path:
    import errno
    import os
    import shutil
    import sys
    import traceback

    def _fatal(msg: str, exc: BaseException | None = None) -> None:
        print(f"[MOVE][ERROR] {msg}", file=sys.stderr)
        if exc is not None:
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)

    try:
        src = Path(media).expanduser().resolve(strict=True)
    except Exception as e:
        _fatal(f"Source path invalid: {media!r}", e)
        raise

    try:
        root_p = Path(root).expanduser().resolve(strict=True)
    except Exception as e:
        _fatal(f"Root path invalid: {root!r}", e)
        raise

    try:
        dest_p = Path(dest).expanduser().resolve()
    except Exception as e:
        _fatal(f"Destination path invalid: {dest!r}", e)
        raise

    try:
        rel = src.relative_to(root_p)
    except Exception as e:
        _fatal(f"Refusing: source not under root.\n  src : {src}\n  root: {root_p}", e)
        raise

    dst = dest_p / rel
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        _fatal(f"Failed creating destination directories: {dst.parent}", e)
        raise

    try:
        if dst.exists():
            if not overwrite:
                raise FileExistsError(f"Destination exists: {dst}")
            if dst.is_dir():
                raise IsADirectoryError(f"Destination is a directory: {dst}")
            dst.unlink()
    except Exception as e:
        _fatal(f"Failed handling existing destination: {dst}", e)
        raise

    try:
        os.replace(src, dst)
        return dst
    except OSError as e:
        if e.errno != errno.EXDEV:
            _fatal(f"os.replace failed (not cross-device).\n  src: {src}\n  dst: {dst}", e)
            raise
    except Exception as e:
        _fatal(f"os.replace failed.\n  src: {src}\n  dst: {dst}", e)
        raise

    tmp = dst.with_name(dst.name + ".tmp_move")
    try:
        if tmp.exists():
            tmp.unlink()
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        src.unlink()
        return dst
    except Exception as e:
        _fatal(f"Cross-device move failed.\n  src: {src}\n  tmp: {tmp}\n  dst: {dst}", e)
        raise
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception as e:
            _fatal(f"Failed cleaning temp file: {tmp}", e)
