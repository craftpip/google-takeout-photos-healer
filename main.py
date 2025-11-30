#!/usr/bin/env python3

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Dict, Optional, Tuple, List

from dateutil import parser as dtparser
from tqdm import tqdm


IMAGE_EXTS = {
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

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".3gp", ".webm"}

TIME_KEYS = ["photoTakenTime", "creationTime", "mediaMetadata"]
GEO_KEYS = ["geoData", "geoDataExif"]


def run_cmd(cmd: List[str]) -> Tuple[int, str, str]:
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = p.communicate()
    return p.returncode, out.strip(), err.strip()


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


def to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    # Treat naive as UTC (Takeout timestamps are UTC)
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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


def extract_taken_dt(data: Dict) -> Optional[datetime]:
    block = data.get("photoTakenTime")
    if isinstance(block, dict):
        dt = _parse_any_dt(block.get("timestamp")) or _parse_any_dt(block.get("formatted"))
        if dt:
            return dt

    mm = data.get("mediaMetadata")
    if isinstance(mm, dict):
        pt = mm.get("photoTakenTime")
        if isinstance(pt, dict):
            dt = _parse_any_dt(pt.get("timestamp")) or _parse_any_dt(pt.get("formatted"))
            if dt:
                return dt
        dt = _parse_any_dt(mm.get("photoTakenTime")) or _parse_any_dt(mm.get("creationTime"))
        if dt:
            return dt

    return None


def extract_creation_dt(data: Dict) -> Optional[datetime]:
    block = data.get("creationTime")
    if isinstance(block, dict):
        dt = _parse_any_dt(block.get("timestamp")) or _parse_any_dt(block.get("formatted"))
        if dt:
            return dt

    mm = data.get("mediaMetadata")
    if isinstance(mm, dict):
        ct = mm.get("creationTime")
        if isinstance(ct, dict):
            dt = _parse_any_dt(ct.get("timestamp")) or _parse_any_dt(ct.get("formatted"))
            if dt:
                return dt
        dt = _parse_any_dt(mm.get("creationTime"))
        if dt:
            return dt

    return None


def extract_gps(data: Dict) -> Optional[Tuple[float, float, Optional[float]]]:
    for key in GEO_KEYS:
        geo = data.get(key)
        if not isinstance(geo, dict):
            continue

        try:
            lat = float(geo.get("latitude"))
            lon = float(geo.get("longitude"))
            if lat == 0.0 and lon == 0.0:
                continue
            alt = geo.get("altitude")
            alt = float(alt) if alt not in (None, 0.0) else None
            return lat, lon, alt
        except Exception:
            continue

    return None


def extract_title_desc(data: Dict) -> Tuple[Optional[str], Optional[str]]:
    title = data.get("title")
    desc = data.get("description")

    if not isinstance(title, str) or not title.strip():
        title = None
    if not isinstance(desc, str) or not desc.strip():
        desc = None

    return title, desc


# ------------------------ name matching ------------------------
from find_media import find_media

# ------------------------ read metadata ------------------------


def read_img_meta(path: Path) -> Dict:
    code, out, _ = run_cmd(["exiftool", "-j", "-a", "-u", "-G1", str(path)])
    if code != 0:
        return {}
    try:
        arr = json.loads(out)
        return arr[0] if arr else {}
    except Exception:
        return {}


def read_vid_meta(path: Path) -> Dict:
    code, out, _ = run_cmd(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)]
    )
    if code != 0:
        return {}
    try:
        return json.loads(out).get("format", {})
    except Exception:
        return {}


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


def get_existing_times_img(
    meta,
) -> Tuple[Optional[datetime], Optional[datetime], Optional[datetime]]:
    dto = parse_exif_dt(meta.get("DateTimeOriginal"))
    cr = parse_exif_dt(meta.get("CreateDate"))
    md = parse_exif_dt(meta.get("ModifyDate"))
    return dto, cr, md


def get_existing_times_vid(
    meta,
) -> Tuple[Optional[datetime], Optional[datetime], Optional[datetime]]:
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


def needs_update(path: Path, taken_dt, creation_dt, gps, title, desc) -> bool:
    # left here in case you want to switch back to pre-check mode later
    print("\n=== DEBUG ===")
    print("FILE:", path)
    print("taken_dt from JSON:", taken_dt)
    print("creation_dt from JSON:", creation_dt)

    ext = path.suffix.lower()
    is_img = ext in IMAGE_EXTS
    meta = read_img_meta(path) if is_img else read_vid_meta(path)

    dto, cr, md = (
        get_existing_times_img(meta) if is_img else get_existing_times_vid(meta)
    )

    taken_json_utc = to_utc(taken_dt)
    created_json_utc = to_utc(creation_dt)

    print("EXIF dto:", dto)
    print("EXIF cr :", cr)
    print("EXIF md :", md)

    EXIF_SUPPORTED = {".jpg", ".jpeg", ".tiff", ".tif", ".heic"}

    if ext not in EXIF_SUPPORTED:
        # non-EXIF formats: don't enforce timestamps
        pass
    else:
        if taken_json_utc:
            if dto is None:
                return True
            if to_utc(dto) != taken_json_utc:
                return True

        if created_json_utc:
            if cr is None:
                return True
            if to_utc(cr) != created_json_utc:
                return True

    if gps and is_img and ext in EXIF_SUPPORTED:
        lat, lon, alt = gps
        elat = meta.get("GPSLatitude")
        elon = meta.get("GPSLongitude")
        if elat is None or elon is None:
            return True
        try:
            elat_f = float(str(elat).split()[0])
            elon_f = float(str(elon).split()[0])
        except Exception:
            return True
        if elat_f != lat or elon_f != lon:
            return True

    if is_img:
        if title:
            existing_title = meta.get("Title") or meta.get("ImageDescription")
            if (existing_title or "").strip() != title.strip():
                return True
        if desc:
            existing_desc = (
                meta.get("Comment")
                or meta.get("XPComment")
                or meta.get("ImageDescription")
            )
            if existing_desc is None:
                return True
            if isinstance(existing_desc, list):
                try:
                    existing_desc = "".join(chr(x) for x in existing_desc if x != 0).strip()
                except Exception:
                    existing_desc = ""
            if str(existing_desc).strip() != desc.strip():
                return True
    else:
        tags = meta.get("tags", {}) if isinstance(meta.get("tags"), dict) else {}
        if title:
            existing_title = tags.get("title") or meta.get("title")
            if (existing_title or "").strip() != title.strip():
                return True
        if desc:
            existing_desc = tags.get("comment") or meta.get("comment")
            if (existing_desc or "").strip() != desc.strip():
                return True

    return False


def write_image(path: Path, taken_dt, gps, write: bool):
    taken_utc = to_utc(taken_dt)
    if not taken_utc:
        return False, "Invalid taken_dt"

    date_str = taken_utc.strftime("%Y:%m:%d %H:%M:%S")
    ext = path.suffix.lower()

    args = ["exiftool", "-overwrite_original"]

    supports_exif = ext in [".jpg", ".jpeg", ".tif", ".tiff", ".webp"]

    if supports_exif:
        args.append("-EXIF:all=")
        args += [
            f"-EXIF:DateTimeOriginal={date_str}",
            f"-EXIF:CreateDate={date_str}",
            f"-EXIF:ModifyDate={date_str}",
            f"-IFD0:ModifyDate={date_str}",
        ]

    args += [
        f"-XMP:CreateDate={date_str}",
        f"-XMP:DateTimeOriginal={date_str}",
        f"-XMP:ModifyDate={date_str}",
    ]

    if supports_exif and gps:
        lat, lon, alt = gps

        if lat is not None:
            args.append(f"-EXIF:GPSLatitudeRef={'N' if lat >= 0 else 'S'}")
            args.append(f"-EXIF:GPSLatitude={abs(lat)}")

        if lon is not None:
            args.append(f"-EXIF:GPSLongitudeRef={'E' if lon >= 0 else 'W'}")
            args.append(f"-EXIF:GPSLongitude={abs(lon)}")

        if alt is not None:
            args.append(f"-EXIF:GPSAltitude={alt}")
            args.append("-EXIF:GPSAltitudeRef=0")

    args.append(str(path))

    if not write:
        return True, "Dry-run OK"

    code, out, err = run_cmd(args)
    return (code == 0, out if out else err)


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


def main():
    parser = argparse.ArgumentParser(description="Restore Google Takeout metadata.")
    parser.add_argument("--root", required=True)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", default="takeout_restore_report.txt")
    args = parser.parse_args()

    write = args.write and not args.dry_run

    check_dependencies()

    root = Path(args.root).expanduser().resolve()
    if not root.exists():
        print(f"[FATAL] Root does not exist: {root}")
        sys.exit(1)

    json_files = [p for p in root.rglob("*.json") if p.is_file()]
    if not json_files:
        print("[WARN] No JSON sidecars found.")
        sys.exit(0)

    ok_count = 0
    fail_count = 0
    skipped = 0

    already_ok = []
    failures = []
    okays = []

    print("Mode:", "WRITE" if write else "DRY-RUN")

    for json_path in tqdm(json_files, desc="Processing sidecars", unit="file"):
        data = parse_json(json_path)
        if data is None or "__parse_error__" in data:
            fail_count += 1
            failures.append(
                (json_path, None, data.get("__parse_error__", "JSON unreadable"))
            )
            continue

        if "title" not in data or not any(k in data for k in TIME_KEYS):
            skipped += 1
            continue

        media = find_media(json_path)
        if not media:
            fail_count += 1
            failures.append((json_path, None, "Media not found"))
            continue

        # strictly require photoTakenTime
        if "photoTakenTime" in data and "timestamp" in data["photoTakenTime"]:
            photoTakenTime = int(data["photoTakenTime"]["timestamp"])
        else:
            fail_count += 1
            failures.append((json_path, None, "photoTakenTime missing"))
            continue

        if "creationTime" in data and "timestamp" in data["creationTime"]:
            creationTime = int(data["creationTime"]["timestamp"])
        else:
            creationTime = photoTakenTime

        photoTakenTime_dt = datetime.fromtimestamp(photoTakenTime, UTC)
        photoCreationTime_dt = datetime.fromtimestamp(creationTime, UTC)

        title = data.get("title")
        desc = data.get("description")

        gps = None
        if "geoData" in data:
            geo = data["geoData"]
            try:
                lat = float(geo.get("latitude"))
                lon = float(geo.get("longitude"))
                if lat != 0.0 or lon != 0.0:
                    alt = geo.get("altitude")
                    alt = float(alt) if alt not in (None, 0.0) else None
                    gps = (lat, lon, alt)
            except Exception:
                pass

        for media_path in media:
            ext = media_path.suffix.lower()
            is_img = ext in IMAGE_EXTS
            is_vid = ext in VIDEO_EXTS

            if not (is_img or is_vid):
                skipped += 1
                continue

            if is_img:
                meta = read_img_meta(media_path)

                file_datetime = None
                if "ExifIFD:DateTimeOriginal" in meta:
                    file_datetime = meta["ExifIFD:DateTimeOriginal"]
                elif "ExifIFD:CreateDate" in meta:
                    file_datetime = meta["ExifIFD:CreateDate"]
                elif "XMP-xmp:CreateDate" in meta:
                    file_datetime = meta["XMP-xmp:CreateDate"]

                if file_datetime is not None:
                    try:
                        media_created_time = datetime.strptime(
                            file_datetime, "%Y:%m:%d %H:%M:%S"
                        ).replace(tzinfo=UTC)
                    except Exception:
                        media_created_time = None

                    if (
                        media_created_time is not None
                        and photoTakenTime_dt.timestamp()
                        == media_created_time.timestamp()
                    ):
                        already_ok.append((json_path, media_path))
                        continue

                ok, msg = write_image(media_path, photoTakenTime_dt, gps, write)
                if ok:
                    ok_count += 1
                    okays.append((json_path, media_path))
                else:
                    fail_count += 1
                    failures.append((json_path, media_path, msg))

            if is_vid:
                # currently only reading; wiring video write back is kept separate
                _meta = read_vid_meta(media_path)
                # if you want to enable:
                # ok, msg = write_video(media_path, photoTakenTime_dt, photoCreationTime_dt, title, desc, write)
                # ...

    report_path = root / args.report
    lines = [
        f"Already up-to-date (untouched): {len(already_ok)}",
        f"Updated: {ok_count}",
        f"Failed:  {fail_count}",
        f"Skipped: {skipped}",
        "",
    ]

    if okays:
        lines.append("Updated files:")
        for jp, mp in okays:
            lines.append(f"{mp}  <- {jp.name}")
        lines.append("")

    if failures:
        lines.append("Failures:")
        for jp, mp, re_ in failures:
            lines.append(f"{jp} -> {mp}: {re_}")
        lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(
        f"\nDone. Up-to-date: {len(already_ok)} Updated: {ok_count} Failed: {fail_count} Skipped: {skipped}"
    )
    print("Report written to", report_path)


if __name__ == "__main__":
    main()
