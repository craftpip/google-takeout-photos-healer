import re
import os
from pathlib import Path
from typing import Optional, List, Tuple
from helpers import IMAGE_EXTS, VIDEO_EXTS


def _strip_index(stem: str) -> tuple[str, int, bool]:
    m = re.search(r"\((\d+)\)$", stem)
    if not m:
        return stem, 0, False
    idx = int(m.group(1))
    return stem[: m.start()], idx, True


def _strip_broken_ext_bits(stem: str) -> str:
    # try to drop garbage like ".j", ".jp", "..." at the end
    return re.sub(r"(?:\.j|\.jp|\.)+$", "", stem)


def _detect_ext_hint(stem: str) -> tuple[str, Optional[str]]:
    lower = stem.lower()
    all_exts = sorted((IMAGE_EXTS | VIDEO_EXTS), key=len, reverse=True)
    for ext in all_exts:
        if lower.endswith(ext):
            return stem[: -len(ext)], ext
    return stem, None


def _normalize_core_and_variant(stem: str) -> tuple[str, bool]:
    # "facebook style": foo_n, foo_a, foo_o, foo_e, foo_
    m = re.match(r"^(.*?)(?:_([naoe])|_)$", stem)
    if m:
        return m.group(1), True

    # foo-edited, foo-whatever
    m = re.match(r"^(.*?)-([A-Za-z]{1,20})$", stem)
    if m:
        return m.group(1), True

    return stem, False


def _parse_json_base(json_base: str) -> tuple[str, int, bool, Optional[str], bool]:
    stem = json_base

    stem, idx, has_idx = _strip_index(stem)
    stem = _strip_broken_ext_bits(stem)
    stem, ext_hint = _detect_ext_hint(stem)
    core, has_variant = _normalize_core_and_variant(stem)

    return core, idx, has_idx, ext_hint, has_variant


def _parse_media_name(name: str) -> tuple[str, int, bool, str]:
    stem, ext = os.path.splitext(name)
    stem, idx, _ = _strip_index(stem)
    core, has_variant = _normalize_core_and_variant(stem)
    return core, idx, has_variant, ext.lower()


def find_media(json_path: Path) -> Optional[List[Path]]:
    # given foo.jpg.json / foo(1).jpg.json / foo.jpg(1).json etc
    parent = json_path.parent

    jname = json_path.name
    if not jname.lower().endswith(".json"):
        return None

    json_base = jname[:-5]
    core_json, idx_json, has_idx, ext_hint, _j_has_variant = _parse_json_base(json_base)

    scored: List[Tuple[Tuple[int, int, int, int], Path]] = []

    for p in parent.iterdir():
        if not p.is_file():
            continue

        ext = p.suffix.lower()
        if ext not in IMAGE_EXTS and ext not in VIDEO_EXTS:
            continue

        core_file, idx_file, has_variant_file, ext_file = _parse_media_name(p.name)

        if core_file != core_json:
            continue

        # index score
        if has_idx:
            if idx_file == idx_json:
                idx_score = 0
            else:
                idx_score = 1 + abs(idx_file - idx_json)
        else:
            if idx_file == 0:
                idx_score = 0
            else:
                idx_score = 1 + idx_file

        # prefer non-variant
        variant_score = 0 if not has_variant_file else 1

        # extension hint (if json name had .jpg etc in it)
        if ext_hint is None or ext_file == ext_hint:
            ext_score = 0
        else:
            ext_score = 1

        name_len = len(p.name)
        scored.append(((idx_score, variant_score, ext_score, name_len), p))

    if not scored:
        return None

    scored.sort(key=lambda x: x[0])
    return [scored[0][1]]

