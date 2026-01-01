from pathlib import Path
from typing import Optional, Tuple
from utils.helpers import run_cmd, to_utc, parse_exif_dt
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

from typing import Optional, Tuple

def get_existing_times_vid(meta) -> Tuple[Optional[object], Optional[object], Optional[object]]:
    # Normalize incoming meta
    tags = meta.get("tags") if isinstance(meta.get("tags"), dict) else {}
    format_tags = {}
    if isinstance(meta.get("format"), dict) and isinstance(meta["format"].get("tags"), dict):
        format_tags = meta["format"]["tags"]

    # Priority order: original recording timestamps first, then generic creation
    candidate_keys = [
        # Common "original creation" style tags
        "MediaCreateDate",
        "MediaCreateTime",
        "CreateDate",
        "CreationDate",
        "TrackCreateDate",
        "TrackCreationDate",
        "DateTimeOriginal",
        "ContentCreateDate",

        # QuickTime / MP4 variants (Apple / ffmpeg / others)
        "com.apple.quicktime.creationdate",
        "creation_time",
        "Creation_time",
        "creation-time",
        "creation_date",
        "creation-date",
        "creationTime",
    ]

    # Case-insensitive lookup helper
    def lookup(d: dict) -> Optional[str]:
        if not isinstance(d, dict):
            return None
        lower_map = {k.lower(): k for k in d.keys() if d.get(k)}
        for key in candidate_keys:
            lk = key.lower()
            if lk in lower_map:
                return d[lower_map[lk]]
        return None

    raw = None

    # 1) Check explicit tags dict
    raw = lookup(tags)

    # 2) Fallback: format.tags (ffprobe top-level)
    if raw is None:
        raw = lookup(format_tags)

    # 3) Fallback: top-level keys on meta itself
    if raw is None:
        raw = lookup(meta)

    # 4) Last resort: legacy direct key
    if raw is None and meta.get("creation_time"):
        raw = meta.get("creation_time")

    cr = parse_exif_dt(raw) if raw else None
    return None, cr, cr

def write_video(path: Path, creation_dt, write: bool):
    created_utc = to_utc(creation_dt)
    if not created_utc:
        return False, "Nothing to write"

    created_utc = created_utc.replace(microsecond=0)
    iso = created_utc.isoformat().replace("+00:00", "Z")

    tmp = path.with_suffix(path.suffix + ".tmp")
    bak = path.with_suffix(path.suffix + ".bak")

    ext = path.suffix.lower()
    is_qt = ext in {".mp4", ".m4v", ".mov", ".qt"}
    is_mkv = ext in {".mkv", ".webm"}

    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-map",
        "0",
        "-map_metadata",
        "0",
        "-c",
        "copy",
    ]

    if is_qt:
        cmd += ["-movflags", "use_metadata_tags"]

    # Write all the common "created" keys so different parsers (ffprobe/exiftool/apps)
    # pick it up as CreateDate/MediaCreateDate/CreationTime/etc.
    meta_pairs = [
        ("creation_time", iso),
        ("date", iso),
        ("created_date", iso),
        ("CreateDate", iso),
        ("MediaCreateDate", iso),
    ]

    # QuickTime/MP4 commonly read by Apple Photos/iOS etc.
    if is_qt:
        meta_pairs += [
            ("com.apple.quicktime.creationdate", iso),
        ]

    # Matroska/WebM commonly read by players/editors
    if is_mkv:
        meta_pairs += [
            ("DATE_RECORDED", iso),
            ("DATE", iso),
        ]

    # AVI INFO chunk date (some tools map this)
    if ext == ".avi":
        meta_pairs += [
            ("ICRD", iso),
        ]

    for k, v in meta_pairs:
        cmd += ["-metadata", f"{k}={v}"]

    cmd.append(str(tmp))

    if not write:
        return True, "Dry-run ok"

    if tmp.exists():
        try:
            tmp.unlink()
        except Exception:
            pass

    code, out, err = run_cmd(cmd)
    if code != 0:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
        return False, (err or out or "ffmpeg failed").strip()

    if not tmp.exists():
        return False, "ffmpeg did not produce output file"

    try:
        if bak.exists():
            bak.unlink()
        shutil.move(path, bak)
        try:
            shutil.move(tmp, path)
        except Exception:
            # rollback
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                pass
            if bak.exists():
                shutil.move(bak, path)
            raise

        bak.unlink(missing_ok=True)
        return True, "updated"
    except Exception as e:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
        return False, str(e)
