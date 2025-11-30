from pathlib import Path
from typing import Optional, Tuple
from helpers import run_cmd, to_utc, parse_exif_dt


def read_img_meta(path: Path) -> dict:
    code, out, _ = run_cmd(["exiftool", "-j", "-a", "-u", "-G1", str(path)])
    if code != 0:
        return {}
    try:
        arr = __import__("json").loads(out)
        return arr[0] if arr else {}
    except Exception:
        return {}


def get_existing_times_img(meta) -> Tuple[Optional[object], Optional[object], Optional[object]]:
    dto = parse_exif_dt(meta.get("DateTimeOriginal"))
    cr = parse_exif_dt(meta.get("CreateDate"))
    md = parse_exif_dt(meta.get("ModifyDate"))
    return dto, cr, md


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
