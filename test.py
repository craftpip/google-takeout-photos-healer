#!/usr/bin/env python3
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from dateutil import parser as dtparser

IMG = Path("./tests/input/1.jpg")

def run(cmd):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = p.communicate()
    return p.returncode, out.strip(), err.strip()

def parse_exif_dt(val):
    if not val:
        return None
    v = str(val).strip()
    try:
        return datetime.strptime(v[:19], "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except:
        try:
            d = dtparser.parse(v)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except:
            return None

def write_random():
    dto = "2031:01:02 03:04:05"
    cr  = "2032:02:03 04:05:06"
    md  = "2033:03:04 05:06:07"

    cmd = [
        "exiftool", "-overwrite_original",
        f"-EXIF:DateTimeOriginal={dto}",
        f"-EXIF:CreateDate={cr}",
        f"-EXIF:ModifyDate={md}",
        str(IMG)
    ]
    code, out, err = run(cmd)
    print("\n=== WRITE RANDOM ===")
    print("CMD:", " ".join(cmd))
    print("code:", code)
    print("out :", out)
    print("err :", err)

    return dto, cr, md

def read_time_all():
    cmd = ["exiftool", "-G", "-a", "-s", "-time:all", str(IMG)]
    code, out, err = run(cmd)
    print("\n=== READ VIA -time:all ===")
    print("code:", code)
    print(out)
    if err:
        print("err:", err)

    found = {}
    import re

    pattern = re.compile(r"\[EXIF\]\s+(\w+)\s*:\s*(.+)$")

    for line in out.splitlines():
        m = pattern.search(line)
        if not m:
            continue
        tag, val = m.group(1), m.group(2)
        if tag in {"DateTimeOriginal", "CreateDate", "ModifyDate"}:
            found[tag] = val.strip()
    return found

def read_json_block():
    cmd = ["exiftool", "-j", str(IMG)]
    code, out, err = run(cmd)
    print("\n=== READ VIA -j (RAW JSON) ===")
    print("code:", code)
    print(out[:1000] + ("..." if len(out) > 1000 else ""))
    if err:
        print("err:", err)

    meta = {}
    try:
        arr = json.loads(out)
        meta = arr[0] if arr else {}
    except:
        pass

    return {
        "DateTimeOriginal": meta.get("DateTimeOriginal"),
        "CreateDate": meta.get("CreateDate"),
        "ModifyDate": meta.get("ModifyDate"),
    }

def main():
    expected_dto, expected_cr, expected_md = write_random()

    time_all = read_time_all()
    jblock   = read_json_block()

    dto_ta = parse_exif_dt(time_all.get("DateTimeOriginal"))
    cr_ta  = parse_exif_dt(time_all.get("CreateDate"))
    md_ta  = parse_exif_dt(time_all.get("ModifyDate"))

    dto_j = parse_exif_dt(jblock.get("DateTimeOriginal"))
    cr_j  = parse_exif_dt(jblock.get("CreateDate"))
    md_j  = parse_exif_dt(jblock.get("ModifyDate"))

    print("\n=== PARSED RESULTS ===")
    print("Expected DTO:", expected_dto, " | time:all ->", dto_ta, " | -j ->", dto_j)
    print("Expected CR :", expected_cr,  " | time:all ->", cr_ta,  " | -j ->", cr_j)
    print("Expected MD :", expected_md,  " | time:all ->", md_ta,  " | -j ->", md_j)

    def ok(a, b):
        return (a is not None) and (b is not None) and abs((a-b).total_seconds()) < 2

    exp_dto = parse_exif_dt(expected_dto)
    exp_cr  = parse_exif_dt(expected_cr)
    exp_md  = parse_exif_dt(expected_md)

    print("\n=== MATCH CHECK (should be TRUE if reading works) ===")
    print("DTO matches time:all?", ok(dto_ta, exp_dto))
    print("CR  matches time:all?", ok(cr_ta, exp_cr))
    print("MD  matches time:all?", ok(md_ta, exp_md))
    print("DTO matches -j?", ok(dto_j, exp_dto))
    print("CR  matches -j?", ok(cr_j, exp_cr))
    print("MD  matches -j?", ok(md_j, exp_md))

if __name__ == "__main__":
    main()
