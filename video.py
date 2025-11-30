from pathlib import Path
from typing import Optional, Tuple
from helpers import run_cmd, to_utc, parse_exif_dt
import shutil


def read_vid_meta(path: Path) -> dict:
    code, out, _ = run_cmd(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)]
    )
    if code != 0:
        return {}
    try:
        return __import__("json").loads(out).get("format", {})
    except Exception:
        return {}


def get_existing_times_vid(meta) -> Tuple[Optional[object], Optional[object], Optional[object]]:
    tags = meta.get("tags") if isinstance(meta.get("tags"), dict) else {}
    possible = [
        "creation_time",
        "Creation_time",
        "creation-time",
        "creation_date",
        "creation-date",
        "com.apple.quicktime.creationdate",
        "creationTime",
    ]
    raw = None
    for k in possible:
        if k in tags and tags.get(k):
            raw = tags.get(k)
            break

    if raw is None:
        raw = meta.get("creation_time")

    cr = parse_exif_dt(raw)
    return None, cr, cr


def write_video(path: Path, taken_dt, creation_dt, title, desc, write: bool):
    created_utc = to_utc(creation_dt)
    if not created_utc and not title and not desc:
        return False, "Nothing to write"

    tmp = path.with_suffix(path.suffix + ".tmp")

    cmd = ["ffmpeg", "-y", "-i", str(path), "-c", "copy"]

    if created_utc:
        iso = created_utc.isoformat().replace("+00:00", "Z")
        cmd += ["-metadata", f"creation_time={iso}"]

    if title:
        cmd += ["-metadata", f"title={title}"]
    if desc:
        cmd += ["-metadata", f"comment={desc}"]

    cmd.append(str(tmp))

    if not write:
        return True, "Dry-run ok"

    code, out, err = run_cmd(cmd)
    if code != 0:
        if tmp.exists():
            tmp.unlink()
        return False, err or out

    bak = path.with_suffix(path.suffix + ".bak")
    try:
        shutil.move(path, bak)
        shutil.move(tmp, path)
        bak.unlink(missing_ok=True)
        return True, "updated"
    except Exception as e:
        return False, str(e)
