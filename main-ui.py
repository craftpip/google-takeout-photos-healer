# takeout_restore.py
# CLI + Tkinter UI integrated
# - UI exposes ALL args
# - overwrite date accepts YYYY-MM-DD and YYYY-MM-DD HH:MM:SS
# - ask_later handled via UI selector (B)

import argparse
import math
import sys
import threading
import queue
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

try:
    import tkinter as tk
    from tkinter import ttk, filedialog
except Exception:
    tk = None  # UI optional / not available

from tqdm import tqdm

from utils.helpers import (
    IMAGE_EXTS,
    VIDEO_EXTS,
    check_dependencies,
    parse_json,
    move_preserve_structure,
    get_time_from_filename,
)
import utils.app_config

from utils.find_media import find_matching_media
from utils.image import read_img_meta, write_image, to_jpeg
from utils.video import read_vid_meta, write_video, get_existing_times_vid

TIME_KEYS = ["photoTakenTime", "creationTime", "mediaMetadata"]
GEO_KEYS = ["geoData", "geoDataExif"]


def _parse_dt_user(s: str) -> datetime:
    """
    Supports:
      - YYYY-MM-DD
      - YYYY-MM-DD HH:MM:SS
    Returns UTC tz-aware datetime.
    """
    s = (s or "").strip()
    if not s:
        raise ValueError("empty datetime")

    fmts = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]
    last_err = None
    for fmt in fmts:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except Exception as e:
            last_err = e
    raise ValueError(f"Invalid datetime '{s}'. Expected YYYY-MM-DD or YYYY-MM-DD HH:MM:SS") from last_err


def _format_dt(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
        return dt.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def make_changes(media: Path, photoTakenTime_dt: datetime, gps):
    ext = media.suffix.lower()
    is_img = ext in IMAGE_EXTS
    is_vid = ext in VIDEO_EXTS

    if not (is_img or is_vid):
        return "skipped", "Unknown format, skipped"

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

        if (
                media_created_time is not None
                and int(photoTakenTime_dt.timestamp()) == int(media_created_time.timestamp())
        ):
            return "already_ok", ""

        ok, msg = write_image(media, photoTakenTime_dt, gps, write=utils.app_config.ARGS.write)
        if ok:
            return "ok", ""
        return "failure", msg or "failed to write image"

    if is_vid:
        meta = read_vid_meta(media)
        dto_v, cr_v, md_v = get_existing_times_vid(meta)

        if cr_v is not None and int(photoTakenTime_dt.timestamp()) == int(cr_v.timestamp()):
            return "already_ok", ""

        ok, msg = write_video(media, photoTakenTime_dt, write=utils.app_config.ARGS.write)
        if ok:
            return "ok", ""
        return "failure", msg or "failed to write video"

    return "failure", "unknown failure"


def build_parser():
    parser = argparse.ArgumentParser(description="Restore Google Takeout metadata")
    parser.add_argument("--root", required=True)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--report", default="takeout_restore_report.txt")
    parser.add_argument(
        "--overwrite-smart",
        nargs="?",
        const=True,  # if flag present with no value => True
        default=False,  # if flag absent => False
        help="Overwrite all files using filename datetime when present, else use this fallback datetime",
    )
    parser.add_argument("--overwrite-date", help="Overwrite all files to fixed datetime")
    parser.add_argument("--move", help="Move the files to sub-directory after update (used with --write)")
    parser.add_argument("--jpg", action="store_true", help="Convert non jpeg images to jpeg (used with --write)")
    parser.add_argument("--motionphoto", action="store_true", help="Find short mp4 motion-photo videos that have a matching photo")
    parser.add_argument("--delete", action="store_true", help="When used with --motionphoto, delete the found videos")
    parser.add_argument("--ui", action="store_true", help="Launch Tkinter UI")
    return parser


AskLaterCallback = Callable[[Path, Path, list[Path], str, datetime, object], Optional[Path]]


def run(
        argv: Optional[Sequence[str]] = None,
        progress_cb: Optional[Callable[[int], None]] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        stop_event: Optional[threading.Event] = None,
        ask_later_cb: Optional[AskLaterCallback] = None,
) -> int:
    """
    Programmatic entrypoint (safe to import).
    Returns process exit code:
      0 = ok
      1 = fatal args/environment error
      2 = cancelled
    """

    def log(s: str):
        if log_cb:
            log_cb(s)
        else:
            print(s, end="" if s.endswith("\n") else "\n")

    def set_progress(p: int):
        if progress_cb:
            try:
                progress_cb(max(0, min(100, int(p))))
            except Exception:
                pass

    parser = build_parser()
    args = parser.parse_args(argv)
    utils.app_config.ARGS = args

    check_dependencies()

    root = Path(args.root).expanduser().resolve()
    if not root.exists():
        log(f"[FATAL] Root does not exist: {root}\n")
        return 1

    # ---------------- Motionphoto Mode ----------------
    if args.motionphoto:
        mp4_files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".mp4"]
        found = []
        deleted = 0

        it = mp4_files if progress_cb else tqdm(mp4_files, desc="Scanning mp4", unit="file")
        total = max(1, len(mp4_files))

        for idx, vid in enumerate(it, 1):
            if stop_event and stop_event.is_set():
                log("\n[STOP] Cancelled by user.\n")
                return 2

            if progress_cb:
                set_progress((idx * 100) / total)

            meta = read_vid_meta(vid)
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

            has_photo = False
            for ext in IMAGE_EXTS:
                cand = vid.with_suffix(ext)
                if cand.exists():
                    has_photo = True
                    break

            if not has_photo:
                continue

            log(str(vid) + "\n")
            found.append(vid)

            if args.delete:
                try:
                    vid.unlink()
                    deleted += 1
                except Exception as e:
                    log(f"[ERROR] Failed to delete {vid}: {e}\n")

        log("\n")
        log(f"Scanned mp4 files: {len(mp4_files)}\n")
        log(f"Motion-photo candidates (<5s + companion photo): {len(found)}\n")
        if args.delete:
            log(f"Deleted: {deleted}\n")

        set_progress(100)
        return 0

    # ---------------- Standard Modes ----------------
    success = []
    already_ok = []
    failures = []
    skipped = []

    mode = "DRY-RUN"
    if args.overwrite_date:
        mode = "OVERWRITE-DATE"
    elif args.overwrite_smart:
        mode = "OVERWRITE-SMART"
    elif args.write:
        mode = "WRITE"

    log(f"Mode: {mode}\n")

    # ---------------- Overwrite Modes ----------------
    if args.overwrite_smart or args.overwrite_date:
        ALL_EXTS = VIDEO_EXTS | IMAGE_EXTS
        media_files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in ALL_EXTS]
        if not media_files:
            log("[WARN] No media files found\n")
            return 0

        total = max(1, len(media_files))

        overwrite_date = None
        try:
            if args.overwrite_date:
                overwrite_date = _parse_dt_user(args.overwrite_date)
        except Exception as e:
            log(f"[FATAL] Bad overwrite datetime: {e}\n")
            return 1

        dt_smart_fallback = None
        try:
            if args.overwrite_smart and args.overwrite_smart is not True:
                dt_smart_fallback = _parse_dt_user(args.overwrite_smart)
        except Exception as e:
            log(f"[WARN] Invalid fallback datetime for overwrite smart: {e}\n")
            pass

        for i, media in enumerate(media_files, 1):
            if stop_event and stop_event.is_set():
                log("\n[STOP] Cancelled by user.\n")
                return 2

            set_progress((i * 100) / total)
            log(f"[{i}/{total}] processing {media}\n")

            # dt_to_write = fallback_dt
            dt_found = None
            if args.overwrite_smart:
                media_name = media.stem
                t = get_time_from_filename(media_name)
                if t is not None:
                    try:
                        # accept either datetime or timestamp-like from your helper
                        if isinstance(t, datetime):
                            overwrite_date = t
                        else:
                            overwrite_date = datetime.fromtimestamp(int(t), tz=timezone.utc)
                        if overwrite_date.tzinfo is None:
                            overwrite_date = overwrite_date.replace(tzinfo=timezone.utc)
                        overwrite_date = overwrite_date.astimezone(timezone.utc)
                        log(f"Found date: {_format_dt(overwrite_date)} for file {media.name}\n")
                    except Exception:
                        pass
                else:
                    if args.overwrite_smart is True:
                        continue
                    else:
                        overwrite_date = datetime.fromtimestamp(dt_smart_fallback.timestamp(), tz=timezone.utc)

            photoTakenTime_dt = overwrite_date.astimezone(timezone.utc)

            if (
                    utils.app_config.ARGS.jpg
                    and media.suffix.lower() not in [".jpeg", ".jpg"]
                    and utils.app_config.ARGS.write
                    and media.suffix.lower() in IMAGE_EXTS
            ):
                try:
                    media2 = to_jpeg(media)
                    media.unlink()
                    media = media2
                except Exception as e:
                    failures.append((media, None, str(e)))
                    continue

            ok, msg = make_changes(media, photoTakenTime_dt, None)
            if ok == "ok":
                success.append((media, media))
            elif ok == "already_ok":
                already_ok.append((media, media, ""))
            elif ok == "skipped":
                skipped.append((media, media, msg))
            else:
                failures.append((media, media, msg))

            if ok in ["ok", "already_ok"] and args.move and args.write:
                move_preserve_structure(media, args.root, args.move, overwrite=True)

    # ---------------- JSON Sidecar Mode ----------------
    else:
        json_files = [p for p in root.rglob("*.json") if p.is_file()]
        total = max(1, len(json_files))

        if not json_files:
            log("[WARN] No JSON sidecars found.\n")
            return 0

        for i, json_path in enumerate(json_files, 1):
            if stop_event and stop_event.is_set():
                log("\n[STOP] Cancelled by user.\n")
                return 2

            set_progress((i * 100) / total)
            log(f"[{i}/{total}] processing {json_path}\n")

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

            if "photoTakenTime" in data and "timestamp" in data["photoTakenTime"]:
                photoTakenTime = int(data["photoTakenTime"]["timestamp"])
            else:
                failures.append((json_path, None, "photoTakenTime in json missing"))
                continue

            photoTakenTime_dt = datetime.fromtimestamp(photoTakenTime, timezone.utc)

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

            title = data.get("title")
            media_path = Path(str(json_path.parent) + "/" + title)

            media, matches = find_matching_media(json_path, media_path)

            # ask_later flow
            if not media and matches is not None and len(matches) > 0:
                if ask_later_cb:
                    chosen = ask_later_cb(json_path, media_path, matches, title, photoTakenTime_dt, gps)
                    if stop_event and stop_event.is_set():
                        log("\n[STOP] Cancelled by user.\n")
                        return 2
                    if chosen is None:
                        skipped.append((json_path, None, "Skipped by user (no match selected)"))
                        continue
                    media = chosen
                else:
                    # CLI fallback prompt
                    log(
                        "\n\nNo matches found, choose the closest match from below (based on filename) or skip\n"
                        f"JSON:     {json_path.name}\n"
                        f"NEEDED:   {media_path.name}\n=== found {len(matches)} lazy matches ===\n"
                    )
                    shown = matches[:10]
                    for idx2, m in enumerate(shown, 1):
                        try:
                            s_kb = math.floor(m.stat().st_size / 1000)
                        except Exception:
                            s_kb = 0
                        log(f"[{idx2}] {m.name}, size {s_kb} kb\n")
                    log("[0] skip\n")
                    try:
                        choice = input("Select: ").strip()
                        if stop_event and stop_event.is_set():
                            log("\n[STOP] Cancelled by user.\n")
                            return 2
                        if not choice.isdigit():
                            skipped.append((json_path, None, "Skipped (invalid choice)"))
                            continue
                        c = int(choice)
                        if c <= 0 or c > len(shown):
                            skipped.append((json_path, None, "Skipped by user"))
                            continue
                        media = shown[c - 1]
                    except Exception:
                        skipped.append((json_path, None, "Skipped (prompt error)"))
                        continue

            elif not media:
                log(f"\n  media for '{json_path.name}' not found.\n")
                failures.append((json_path, None, "Media not found / Skip"))
                continue

            # optional convert to jpeg (write mode only)
            if (
                    utils.app_config.ARGS.jpg
                    and media.suffix.lower() not in [".jpeg", ".jpg"]
                    and utils.app_config.ARGS.write
                    and media.suffix.lower() in IMAGE_EXTS
            ):
                try:
                    media2 = to_jpeg(media)
                    media.unlink()
                    media = media2
                except Exception as e:
                    failures.append((json_path, None, str(e)))
                    continue

            ok, msg = make_changes(media, photoTakenTime_dt, gps)
            if ok == "ok":
                success.append((json_path, media))
            elif ok == "already_ok":
                already_ok.append((json_path, media, ""))
            elif ok == "skipped":
                skipped.append((json_path, media, msg))
            else:
                failures.append((json_path, media, msg))

            if ok in ["ok", "already_ok"] and args.move and args.write:
                move_preserve_structure(media, args.root, args.move, overwrite=True)
                move_preserve_structure(json_path, args.root, args.move, overwrite=True)

    # ---------------- Report ----------------
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
    log("\n".join(lines) + "\n")
    log(
        f"\nDone. Up-to-date: {len(already_ok)} Updated: {len(success)} Failed: {len(failures)} Skipped: {len(skipped)}\n"
    )
    log(f"Report written to {report_path}\n")

    set_progress(100)
    return 0


# --------------------------- Tkinter UI ---------------------------

@dataclass
class AskLaterRequest:
    json_path: Path
    needed_media_path: Path
    matches: list[Path]
    title: str
    taken_dt: datetime
    gps: object
    event: threading.Event
    result: Optional[Path] = None


def launch_ui():
    if tk is None:
        print("[FATAL] tkinter not available in this Python build.")
        sys.exit(1)

    root = tk.Tk()
    root.title("Google Photos Takeout Healer")
    root.geometry("860x880")

    q: "queue.Queue[tuple[str, object]]" = queue.Queue()
    stop_event = threading.Event()
    worker_thread: Optional[threading.Thread] = None

    # ---------------- Vars ----------------
    root_dir_var = tk.StringVar()

    # Mode: normal / overwrite_smart / overwrite_date / motionphoto
    mode_var = tk.StringVar(value="normal")

    write_mode_var = tk.BooleanVar(value=False)
    jpg_var = tk.BooleanVar(value=False)

    report_var = tk.StringVar(value="takeout_restore_report.txt")

    move_enabled_var = tk.BooleanVar(value=False)
    move_path_var = tk.StringVar(value="")  # FULL folder path

    overwrite_smart_dt_var = tk.StringVar(value="")
    overwrite_date_dt_var = tk.StringVar(value="")

    delete_motion_var = tk.BooleanVar(value=False)

    # ---------------- UI helpers (thread-safe via queue) ----------------
    def ui_log(msg: str):
        q.put(("log", msg))

    def ui_progress(p: int):
        q.put(("progress", int(p)))

    # AskLater plumbing
    pending_ask: dict[str, AskLaterRequest] = {}

    def ask_later_cb(json_path: Path, media_path: Path, matches: list[Path], title: str, taken_dt: datetime, gps) -> Optional[Path]:
        req = AskLaterRequest(
            json_path=json_path,
            needed_media_path=media_path,
            matches=matches,
            title=title,
            taken_dt=taken_dt,
            gps=gps,
            event=threading.Event(),
        )
        key = str(json_path)
        pending_ask[key] = req
        q.put(("ask_later", key))

        while True:
            if stop_event.is_set():
                return None
            if req.event.wait(timeout=0.1):
                return req.result

    # ---------------- Controls logic ----------------
    def browse_root():
        path = filedialog.askdirectory()
        if path:
            root_dir_var.set(path)

    def browse_move_folder():
        path = filedialog.askdirectory()
        if path:
            move_path_var.set(path)

    def update_mode_states(*_):
        m = mode_var.get()

        smart_state = "normal" if m == "overwrite_smart" else "disabled"
        date_state = "normal" if m == "overwrite_date" else "disabled"
        delete_state = "normal" if m == "motionphoto" else "disabled"

        smart_entry.configure(state=smart_state)
        smart_label.configure(state=smart_state)

        date_entry.configure(state=date_state)
        date_label.configure(state=date_state)

        delete_cb.configure(state=delete_state)
        if m != "motionphoto":
            delete_motion_var.set(False)

    def update_write_dependent(*_):
        w = write_mode_var.get()

        jpg_cb.configure(state=("normal" if w else "disabled"))
        move_cb.configure(state=("normal" if w else "disabled"))

        # move path widgets
        move_entry.configure(state=("normal" if (w and move_enabled_var.get()) else "disabled"))
        move_browse_btn.configure(state=("normal" if (w and move_enabled_var.get()) else "disabled"))

        if not w:
            jpg_var.set(False)
            move_enabled_var.set(False)

    def update_move_enabled(*_):
        w = write_mode_var.get()
        enabled = move_enabled_var.get()
        state = "normal" if (w and enabled) else "disabled"
        move_entry.configure(state=state)
        move_browse_btn.configure(state=state)

    # ---------------- Worker ----------------
    def run_task():
        nonlocal worker_thread
        stop_event.clear()
        output_box.delete("1.0", tk.END)
        progress["value"] = 0

        root_dir = root_dir_var.get().strip()
        if not root_dir:
            output_box.insert(tk.END, "Select Root folder first.\n")
            return

        argv = ["--root", root_dir]

        m = mode_var.get()
        if m == "overwrite_smart":
            dt_s = overwrite_smart_dt_var.get().strip()
            # if not dt_s:
            # output_box.insert(tk.END, "Overwrite Smart needs fallback date/time.\n")
            # return
            if dt_s:
                argv += ["--overwrite-smart", dt_s]
            else:
                argv += ["--overwrite-smart"]
        elif m == "overwrite_date":
            dt_s = overwrite_date_dt_var.get().strip()
            if not dt_s:
                output_box.insert(tk.END, "Overwrite Date needs fixed date/time.\n")
                return
            argv += ["--overwrite-date", dt_s]
        elif m == "motionphoto":
            argv += ["--motionphoto"]
            if delete_motion_var.get():
                argv += ["--delete"]

        if write_mode_var.get():
            argv += ["--write"]

        rep = report_var.get().strip()
        if rep:
            argv += ["--report", rep]

        if write_mode_var.get() and jpg_var.get():
            argv += ["--jpg"]

        if write_mode_var.get() and move_enabled_var.get():
            mv_path = move_path_var.get().strip()
            if not mv_path:
                output_box.insert(tk.END, "Move enabled but folder path is empty.\n")
                return
            argv += ["--move", mv_path]

        def target():
            code = run(
                argv=argv,
                progress_cb=ui_progress,
                log_cb=ui_log,
                stop_event=stop_event,
                ask_later_cb=ask_later_cb,
            )
            ui_log(f"\n[EXIT] code={code}\n")

        worker_thread = threading.Thread(target=target, daemon=True)
        worker_thread.start()

    def stop_task():
        stop_event.set()
        output_box.insert(tk.END, "Stop requested...\n")

    # ---------------- UI queue pump ----------------
    def open_ask_later_dialog(key: str):
        req = pending_ask.get(key)
        if not req:
            return

        top = tk.Toplevel(root)
        top.title("Select matching media")
        top.geometry("760x520")
        top.transient(root)
        top.grab_set()

        header = (
            f"JSON:   {req.json_path.name}\n"
            f"NEEDED: {req.needed_media_path.name}\n"
            f"Taken:  {_format_dt(req.taken_dt)}\n"
            f"Matches: {len(req.matches)}\n"
        )
        tk.Label(top, text=header, justify="left").pack(anchor="w", padx=10, pady=10)

        frame = tk.Frame(top)
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        lb = tk.Listbox(frame, selectmode="single")
        sb = tk.Scrollbar(frame, orient="vertical", command=lb.yview)
        lb.configure(yscrollcommand=sb.set)

        lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")

        for p in req.matches:
            try:
                size_kb = math.floor(p.stat().st_size / 1000)
            except Exception:
                size_kb = 0
            lb.insert(tk.END, f"{p.name}   ({size_kb} kb)")

        btns = tk.Frame(top)
        btns.pack(fill="x", padx=10, pady=(0, 10))

        def pick_and_close():
            sel = lb.curselection()
            if not sel:
                return
            idx = int(sel[0])
            req.result = req.matches[idx]
            req.event.set()
            pending_ask.pop(key, None)
            top.destroy()

        def skip_and_close():
            req.result = None
            req.event.set()
            pending_ask.pop(key, None)
            top.destroy()

        tk.Button(btns, text="Use Selected", command=pick_and_close, width=14).pack(side="left", padx=5)
        tk.Button(btns, text="Skip", command=skip_and_close, width=10).pack(side="left", padx=5)

        top.protocol("WM_DELETE_WINDOW", skip_and_close)

        if req.matches:
            lb.selection_set(0)
            lb.activate(0)

    def pump_queue():
        try:
            while True:
                typ, payload = q.get_nowait()
                if typ == "log":
                    s = str(payload)
                    output_box.insert(tk.END, s)
                    if not s.endswith("\n"):
                        output_box.insert(tk.END, "\n")
                    output_box.see(tk.END)
                elif typ == "progress":
                    progress["value"] = int(payload)
                elif typ == "ask_later":
                    open_ask_later_dialog(str(payload))
        except queue.Empty:
            pass
        root.after(50, pump_queue)

    # ---------------- Layout ----------------
    tk.Label(root, text="Root Folder").pack(anchor="w", padx=10, pady=(10, 0))
    root_row = tk.Frame(root)
    root_row.pack(fill="x", padx=10)

    tk.Entry(root_row, textvariable=root_dir_var).pack(side="left", fill="x", expand=True)
    tk.Button(root_row, text="Browse", command=browse_root).pack(side="left", padx=5)

    tk.Label(root, text="Mode").pack(anchor="w", padx=10, pady=(12, 0))
    mode_box = tk.Frame(root)
    mode_box.pack(fill="x", padx=10)

    tk.Radiobutton(mode_box, text="Normal (JSON sidecars)", variable=mode_var, value="normal", command=update_mode_states).pack(anchor="w")
    tk.Radiobutton(mode_box, text="Overwrite Smart (use filename datetime if present)", variable=mode_var, value="overwrite_smart", command=update_mode_states).pack(anchor="w")
    tk.Radiobutton(mode_box, text="Overwrite Date (fixed datetime for all)", variable=mode_var, value="overwrite_date", command=update_mode_states).pack(anchor="w")
    tk.Radiobutton(mode_box, text="Motionphoto scan (short mp4 + companion photo)", variable=mode_var, value="motionphoto", command=update_mode_states).pack(anchor="w")

    # Mode-dependent controls (always visible)
    smart_row = tk.Frame(root)
    smart_row.pack(fill="x", padx=10, pady=(6, 0))
    smart_label = tk.Label(smart_row, text="Fallback datetime (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)")
    smart_label.pack(anchor="w")
    smart_entry = tk.Entry(smart_row, textvariable=overwrite_smart_dt_var)
    smart_entry.pack(fill="x")

    date_row = tk.Frame(root)
    date_row.pack(fill="x", padx=10, pady=(6, 0))
    date_label = tk.Label(date_row, text="Fixed datetime (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)")
    date_label.pack(anchor="w")
    date_entry = tk.Entry(date_row, textvariable=overwrite_date_dt_var)
    date_entry.pack(fill="x")

    delete_cb = tk.Checkbutton(root, text="Delete found videos (--delete)", variable=delete_motion_var)
    delete_cb.pack(anchor="w", padx=10, pady=(6, 0))

    tk.Label(root, text="Options").pack(anchor="w", padx=10, pady=(12, 0))
    opts = tk.Frame(root)
    opts.pack(fill="x", padx=10)

    write_cb = tk.Checkbutton(opts, text="Write mode (--write)", variable=write_mode_var, command=update_write_dependent)
    write_cb.pack(anchor="w")

    report_row = tk.Frame(opts)
    report_row.pack(fill="x", pady=(6, 0))
    tk.Label(report_row, text="Report filename (--report)").pack(side="left")
    tk.Entry(report_row, textvariable=report_var).pack(side="left", fill="x", expand=True, padx=6)

    jpg_cb = tk.Checkbutton(opts, text="Convert images to JPG (--jpg) [write-only]", variable=jpg_var)
    jpg_cb.pack(anchor="w", pady=(6, 0))

    # MOVE: folder path + browse
    move_row = tk.Frame(opts)
    move_row.pack(fill="x", pady=(6, 0))
    move_cb = tk.Checkbutton(move_row, text="Move updated files to folder (--move) [write-only]", variable=move_enabled_var, command=update_move_enabled)
    move_cb.pack(side="left")
    move_entry = tk.Entry(move_row, textvariable=move_path_var)
    move_entry.pack(side="left", fill="x", expand=True, padx=6)
    move_browse_btn = tk.Button(move_row, text="Browse", command=browse_move_folder)
    move_browse_btn.pack(side="left")

    btns = tk.Frame(root)
    btns.pack(pady=12)
    tk.Button(btns, text="Run", width=12, command=run_task).pack(side="left", padx=5)
    tk.Button(btns, text="Stop", width=12, command=stop_task).pack(side="left", padx=5)

    progress = ttk.Progressbar(root, length=700, mode="determinate", maximum=100)
    progress.pack(pady=(0, 10))
    progress["value"] = 0

    tk.Label(root, text="Output").pack(anchor="w", padx=10)
    output_box = tk.Text(root, height=18)
    output_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    # init states
    update_mode_states()
    update_write_dependent()
    update_move_enabled()

    pump_queue()
    root.mainloop()


def main():
    return_code = run(argv=None)
    raise SystemExit(return_code)


if __name__ == "__main__":
    if len(sys.argv) == 1 or "--ui" in sys.argv[1:]:
        launch_ui()
    else:
        main()
