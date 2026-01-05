import argparse
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from tqdm import tqdm
from utils.helpers import IMAGE_EXTS, VIDEO_EXTS, check_dependencies, parse_json, move_preserve_structure, get_time_from_filename
import utils.app_config

TIME_KEYS = ["photoTakenTime", "creationTime", "mediaMetadata"]
GEO_KEYS = ["geoData", "geoDataExif"]

# ------------------------ name matching ------------------------
from utils.find_media import find_matching_media
from utils.image import read_img_meta, write_image, to_jpeg
from utils.video import read_vid_meta, write_video, get_existing_times_vid


def make_changes(media, photoTakenTime_dt, gps):
    ext = media.suffix.lower()
    is_img = ext in IMAGE_EXTS
    is_vid = ext in VIDEO_EXTS

    if not (is_img or is_vid):
        return 'skipped', 'Unknown format, skipped'

    ok = None
    if is_img:
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
            return 'already_ok', ''
        else:
            ok, msg = write_image(media, photoTakenTime_dt, gps, write=utils.app_config.ARGS.write)
            if ok:
                return 'ok', ''
            else:
                print("failed to write image " + str(media.stem + media.suffix))
                return 'failure', ''

    if is_vid:
        meta = read_vid_meta(media)
        dto_v, cr_v, md_v = get_existing_times_vid(meta)

        if cr_v is not None and photoTakenTime_dt.timestamp() == cr_v.timestamp():
            return 'already_ok', ''
        else:
            ok, msg = write_video(media, photoTakenTime_dt, write=utils.app_config.ARGS.write)
            if ok:
                return 'ok', ''
            else:
                return 'failure', msg

    return 'failure'


def main():
    parser = argparse.ArgumentParser(description="Restore Google Takeout metadata")
    parser.add_argument("--root", required=True)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--report", default="takeout_restore_report.txt")
    parser.add_argument("--overwrite-smart", help="Overwrite all files from file's format or fallback to date-time")
    parser.add_argument("--overwrite-date", help="Overwrite all files to fixed date-time")
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

    success = []
    already_ok = []
    failures = []
    skipped = []
    ask_later = []

    mode = 'DRY-RUN'
    if args.overwrite_date:
        mode = 'OVERWRITE-DATE'
    if args.overwrite_smart:
        mode = 'OVERWRITE-SMART'
    elif args.write:
        mode = 'WRITE'
    elif args.motionphoto:
        mode = 'CHECK-MOTIONPHOTO'

    print("Mode:", mode)

    if args.overwrite_smart or args.overwrite_date:
        ALL_EXTS = VIDEO_EXTS | IMAGE_EXTS
        media_files = [
            p for p in root.rglob("*")
            if p.is_file() and p.suffix.lower() in ALL_EXTS
        ]
        if not media_files:
            print('[WARN] No media files found')
            sys.exit(0)

        total = len(media_files)
        date_to_write = ts = int(
            datetime.strptime(args.overwrite_smart or args.overwrite_date, "%Y-%m-%d")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )

        print(ts)
        for i, media in enumerate(media_files):
            print("[" + str(i) + "/" + str(total) + "] processing " + str(media) + '')

            if args.overwrite_smart:
                media_name = media.stem

                time = get_time_from_filename(media_name)
                if time is not None:
                    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                    # show time only if it's not midnight
                    if dt.time().hour or dt.time().minute or dt.time().second:
                        date_to_write = dt.strftime("%Y-%m-%d %H:%M:%S")
                    else:
                        date_to_write = dt.strftime("%Y-%m-%d")

                    print(f'Found date: {date_to_write} for file {media.stem}{media.suffix}')

            photoTakenTime_dt = datetime.fromtimestamp(date_to_write, timezone.utc)
            if utils.app_config.ARGS.jpg and media.suffix.lower() not in ['.jpeg', '.jpg'] and utils.app_config.ARGS.write \
                    and media.suffix.lower() in IMAGE_EXTS:
                try:
                    media2 = to_jpeg(media)
                    media.unlink()
                    media = media2
                except Exception as e:
                    failures.append((media, None, e.__str__()))
                    continue

            ok, msg = make_changes(media, photoTakenTime_dt, None)
            if ok == 'ok':
                success.append((media, media))
            if ok == 'already_ok':
                already_ok.append((media, media, ""))
            if ok == 'skipped':
                skipped.append((media, media, msg))
            if ok == 'failures':
                failures.append((media, media, msg))

            if ok in ['ok', 'already_ok'] and args.move:
                move_preserve_structure(media, args.root, args.move, overwrite=True)



    else:
        # scan all json files
        json_files = [p for p in root.rglob("*.json") if p.is_file()]
        total = len(json_files)
        if not json_files:
            print("[WARN] No JSON sidecars found. vroom vroom.")
            sys.exit(0)

        for i, json_path in enumerate(json_files):
            # for json_path in tqdm(json_files, desc="Processing sidecars", unit="file"):
            print("[" + str(i) + "/" + str(total) + "] processing " + str(json_path) + '')
            data = parse_json(json_path)
            if data is None:
                failures.append((json_path, None, "JSON unreadable"))
                continue
            if "__parse_error__" in data:
                failures.append((json_path, None, data["__parse_error__"]))
                continue

            if "title" not in data:
                skipped.append((json_path, None, "No title field"))
                continue

            # strictly require photoTakenTime
            if "photoTakenTime" in data and "timestamp" in data["photoTakenTime"]:
                photoTakenTime = int(data["photoTakenTime"]["timestamp"])
            else:
                failures.append((json_path, None, "photoTakenTime in json missing"))
                continue

            if "creationTime" in data and "timestamp" in data["creationTime"]:
                creationTime = int(data["creationTime"]["timestamp"])
            else:
                creationTime = photoTakenTime

            photoTakenTime_dt = datetime.fromtimestamp(photoTakenTime, timezone.utc)
            # photoCreationTime_dt = datetime.fromtimestamp(creationTime, timezone.utc)

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

            media, matches = find_matching_media(json_path, media_path)
            if not media and matches is not None:
                print("\n  media for '" + json_path.stem + json_path.suffix + "' not found, will ask for confirmation later. \n")
                ask_later.append((json_path, media_path, matches, title, photoTakenTime_dt, gps))
                continue
            elif not media:
                print("\n  media for '" + json_path.stem + json_path.suffix + "' not found. \n")
                failures.append((json_path, None, "Media not found / Skip"))
                continue

            if utils.app_config.ARGS.jpg and media.suffix.lower() not in ['.jpeg', '.jpg'] and utils.app_config.ARGS.write \
                    and media.suffix.lower() in IMAGE_EXTS:
                try:
                    media2 = to_jpeg(media)
                    media.unlink()
                    media = media2
                except Exception as e:
                    failures.append((json_path, None, e.__str__()))
                    continue

            ok, msg = make_changes(media, photoTakenTime_dt, gps)

            if ok == 'ok':
                success.append((json_path, media))
            if ok == 'already_ok':
                already_ok.append((json_path, media, ""))
            if ok == 'skipped':
                skipped.append((json_path, media, msg))
            if ok == 'failures':
                failures.append((json_path, media, msg))

            if ok in ['ok', 'already_ok'] and args.move and args.write:
                move_preserve_structure(media, args.root, args.move, overwrite=True)
                move_preserve_structure(json_path, args.root, args.move, overwrite=True)

    total_ask_later = len(ask_later)

    while ask_later:
        json_path, media_path, matches, title, photoTakenTime_dt, gps = ask_later.pop(0)

        print(
            f"{total_ask_later - len(ask_later)} of {total_ask_later} \n"
            "No matches found, choose the closes match from below (based on filename) or skip\n"
            f"JSON:     {json_path.name}\n"
            f"NEEDED:   {media_path.name}\n=== found " + str(len(matches)) + " lazy matches ==="
        )
        for i, m in enumerate(matches, 1):
            # json_path.stem + json.suffix , trying to find media_path.steam + media_path.suffix, did not find any matching thus pls select one of below that lazy match the file name. or skip
            try:
                s = m.stat().st_size / 1000
            except Exception as e:
                print(e.__str__())
                s = 0

            print(f"[{i}]       {m.name}, size {math.floor(s)} kb")
            if i > 5:
                print(f"...")
                break

        sys.stdout.write("\a")
        sys.stdout.flush()
        choice = input(f"\nSelect 1-{len(matches)} (or just ENTER to skip): ").strip()
        if choice in {"0", ""}:
            continue

        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(matches):
                media = matches[idx - 1]

                if utils.app_config.ARGS.jpg and media.suffix.lower() not in ['.jpeg', '.jpg'] and utils.app_config.ARGS.write \
                        and media.suffix.lower() in IMAGE_EXTS:
                    try:
                        media2 = to_jpeg(media)
                        media.unlink()
                        media = media2
                    except Exception as e:
                        failures.append((json_path, None, e.__str__()))
                        continue

                ok, msg = make_changes(media, photoTakenTime_dt, gps)
                if ok == 'ok':
                    success.append((json_path, media))
                if ok == 'already_ok':
                    already_ok.append((json_path, media, ""))
                if ok == 'skipped':
                    skipped.append((json_path, media, msg))
                if ok == 'failures':
                    failures.append((json_path, media, msg))

                if ok in ['ok', 'already_ok'] and args.move:
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
