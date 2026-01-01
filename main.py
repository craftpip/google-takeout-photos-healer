import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple, List

from dateutil import parser as dtparser
from tqdm import tqdm
from utils.helpers import IMAGE_EXTS, VIDEO_EXTS, run_cmd, to_utc, parse_exif_dt, check_dependencies, _parse_any_dt, parse_json, move_preserve_structure
import utils.app_config

TIME_KEYS = ["photoTakenTime", "creationTime", "mediaMetadata"]
GEO_KEYS = ["geoData", "geoDataExif"]

# ------------------------ name matching ------------------------
from utils.find_media import find_media, find_matching_media
from utils.image import read_img_meta, write_image, get_existing_times_img, to_jpeg
from utils.video import read_vid_meta, write_video, get_existing_times_vid


def main():
    parser = argparse.ArgumentParser(description="Restore Google Takeout metadata")
    parser.add_argument("--root", required=True)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--report", default="takeout_restore_report.txt")
    parser.add_argument("--move", help="Move the files to sub-directory after update")
    parser.add_argument("--jpg", action="store_true", help="Convert non jpeg images to jpeg")

    parser.add_argument("--motionphoto", action="store_true", help="Find short mp4 motion-photo videos that have a matching photo")
    parser.add_argument("--delete", action="store_true", help="When used with --motionphoto, delete the found videos")
    args = parser.parse_args()
    utils.app_config.ARGS = args
    check_dependencies()
    root = Path(args.root).expanduser().resolve()
    if not root.exists():
        print(f"[FATAL] Root does not exist: {root}")
        sys.exit(1)

    mode = 'DRY-RUN'
    if args.write:
        mode = 'WRITE'
    elif args.motionphoto:
        mode = 'CHECK-MOTIONPHOTO'

    # MOTIONPHOTO mode: find mp4 files shorter than 5s that have a photo with same base name
    if args.motionphoto:
        mp4_files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".mp4"]
        found = []
        deleted = 0

        for vid in tqdm(mp4_files, desc="Scanning mp4", unit="file"):
            meta = read_vid_meta(vid)
            # ffprobe 'format' dict usually has 'duration' as string seconds
            dur = None
            if isinstance(meta, dict):
                dur = meta.get("duration")
                if not dur:
                    tags = meta.get("tags") if isinstance(meta.get("tags"), dict) else {}
                    dur = tags.get("duration")

            try:
                dur_f = float(dur) if dur is not None else None
            except Exception:
                dur_f = None

            if dur_f is None or dur_f >= 5.0:
                continue

            # check for matching photo with same base name
            has_photo = False
            for ext in IMAGE_EXTS:
                cand = vid.with_suffix(ext)
                if cand.exists():
                    has_photo = True
                    break

            if not has_photo:
                continue

            print(str(vid))
            found.append(vid)

            if args.delete:
                try:
                    vid.unlink()
                    deleted += 1
                except Exception as e:
                    print(f"[ERROR] Failed to delete {vid}: {e}", file=sys.stderr)

        # summary
        print("")
        print(f"Scanned mp4 files: {len(mp4_files)}")
        print(f"Motion-photo candidates (<5s + companion photo): {len(found)}")
        if args.delete:
            print(f"Deleted: {deleted}")

        return

    # scan all json files
    json_files = [p for p in root.rglob("*.json") if p.is_file()]
    if not json_files:
        print("[WARN] No JSON sidecars found. vroom vroom.")
        sys.exit(0)

    success = []
    already_ok = []
    failures = []
    skipped = []

    print("Mode:", mode)

    for json_path in tqdm(json_files, desc="Processing sidecars", unit="file"):
        data = parse_json(json_path)
        if data is None or "__parse_error__" in data:
            failures.append(
                (json_path, None, data.get("__parse_error__", "JSON unreadable"))
            )
            continue

        if "title" not in data:
            skipped.append((json_path, None, "No title field"))
            continue

        # strictly require photoTakenTime
        if "photoTakenTime" in data and "timestamp" in data["photoTakenTime"]:
            photoTakenTime = int(data["photoTakenTime"]["timestamp"])
        else:
            failures.append((json_path, None, "photoTakenTime missing"))
            continue

        if "creationTime" in data and "timestamp" in data["creationTime"]:
            creationTime = int(data["creationTime"]["timestamp"])
        else:
            creationTime = photoTakenTime

        photoTakenTime_dt = datetime.fromtimestamp(photoTakenTime, timezone.utc)
        photoCreationTime_dt = datetime.fromtimestamp(creationTime, timezone.utc)

        gps = None
        if "geoData" in data:
            geo = data["geoData"]
            try:
                lat = float(geo.get("latitude"))
                lon = float(geo.get("longitude"))
                alt = float(geo.get("altitude"))
                gps = (lat, lon, alt)
            except Exception:
                pass

        # the file to update,
        # now this file must exist, or else its there with some other name changes.
        title = data.get("title")
        media_path = Path(str(json_path.parent) + '/' + title)
        # here

        media = find_matching_media(json_path, media_path)
        if not media:
            print("\n  media for '" + json_path.stem + json_path.suffix + "' not found. \n")
            failures.append((json_path, None, "Media not found / Skip"))
            continue

        ext = media.suffix.lower()
        is_img = ext in IMAGE_EXTS
        is_vid = ext in VIDEO_EXTS

        if not (is_img or is_vid):
            skipped.append((json_path, media_path, "Unknown format, Skipped"))
            continue

        ok = None
        if is_img:
            if utils.app_config.ARGS.jpg and media.suffix.lower() not in ['.jpeg', '.jpg']:
                media2 = to_jpeg(media)
                media.unlink()
                media = media2

            meta = read_img_meta(media)
            file_datetime = None
            if "ExifIFD:DateTimeOriginal" in meta:
                file_datetime = meta["ExifIFD:DateTimeOriginal"]
            elif "ExifIFD:CreateDate" in meta:
                file_datetime = meta["ExifIFD:CreateDate"]
            elif "XMP-xmp:CreateDate" in meta:
                file_datetime = meta["XMP-xmp:CreateDate"]

            media_created_time = None
            if file_datetime is not None:
                try:
                    media_created_time = datetime.strptime(
                        file_datetime, "%Y:%m:%d %H:%M:%S"
                    ).replace(tzinfo=timezone.utc)
                except Exception:
                    pass

            if media_created_time is not None and photoTakenTime_dt.timestamp() == media_created_time.timestamp():
                # same time then its already updated.
                already_ok.append((json_path, media, ""))
                ok = True
            else:
                ok, msg = write_image(media, photoTakenTime_dt, gps, write=False)
                if ok:
                    success.append((json_path, media))
                else:
                    print("failed to write image " + str(media.stem + media.suffix))
                    failures.append((json_path, media, msg))

        if is_vid:
            meta = read_vid_meta(media)
            dto_v, cr_v, md_v = get_existing_times_vid(meta)

            if cr_v is not None and photoTakenTime_dt.timestamp() == cr_v.timestamp():
                already_ok.append((json_path, media))
                ok = True
            else:
                ok, msg = write_video(media, photoTakenTime_dt, write=False)
                if ok:
                    success.append((json_path, media))
                else:
                    print("failed to write video " + str(media.stem + media.suffix))
                    failures.append((json_path, media, msg))

        if ok and args.move:
            move_preserve_structure(media, args.root, args.move, overwrite=True)
            move_preserve_structure(json_path, args.root, args.move, overwrite=True)

    report_path = root / args.report
    lines = [
        f"Already up-to-date (untouched): {len(already_ok)}",
        f"Updated: {len(success)}",
        f"Failed:  {len(failures)}",
        f"Skipped: {len(skipped)}",
        "",
    ]

    if failures:
        lines.append("Failures:")
        for jp, mp, re_ in failures:
            lines.append(f"{jp} -> {mp}: {re_}")
        lines.append("")

    if skipped:
        lines.append("Skipped:")
        for jp, mp, re_ in skipped:
            lines.append(f"{jp} -> {mp}: {re_}")
        lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(
        f"\nDone. Up-to-date: {len(already_ok)} Updated: {len(success)} Failed: {len(failures)} Skipped: {len(skipped)}"
    )
    print("Report written to", report_path)


if __name__ == "__main__":
    main()
