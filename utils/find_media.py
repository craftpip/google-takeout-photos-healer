import re
import os
import sys
from pathlib import Path
from typing import Optional, List, Tuple, Any
from utils.helpers import IMAGE_EXTS, VIDEO_EXTS
import math
import utils.app_config


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


def find_matching_media(json_path: Path, media_path: Path) -> tuple[Path, None] | tuple[None, None] | tuple[Any, None] | tuple[None, tuple[Path, Path, list[Any]]] | Path:
    # json is the sidecar, and media is the file defined inside the json file, (which can be wrong) lol
    # rules for matching
    #   first file the file that is matched perfectly. that is the media_path
    #   check if the json file contains "(n)" format, if yes then find media file with that format.

    file_with_number_regex = re.compile(r"\((\d+)\)$")
    contains_variants = file_with_number_regex.search(str(json_path.stem))

    if contains_variants:
        # wow found a weird ass file.
        # caught a variance, this json file refers to the wrong media file, find the real media file with the json file number.
        variant_number = int(contains_variants.group(1))
        variant_media_file = Path(str(json_path.parent) + "/" + media_path.stem + "(" + str(variant_number) + ")" + media_path.suffix)

        if variant_media_file.exists():
            return (variant_media_file, None)
        else:
            # try with the file name given in media path
            return (None, None)
    else:
        if not media_path.exists():
            # if the media file is not found, (which was given in the json file)
            # then the name of the file is wrong, try to find the file with the json file's name

            # 1. check if the end of the media file for this json contains _n, if yes then find file with _(1)
            # 2. check if the end of the media file for this json contains -n, if yes then find file with _(1)

            alt = Path(str(media_path.parent) + '/' + media_path.stem + '.jpg')
            if alt.exists():
                return (alt, None)

            # remove_motion_gif
            if media_path.stem.find("-MOTION") != -1:
                alt = Path(str(media_path.parent) + '/' + media_path.stem[:-len('-MOTION')] + media_path.suffix)
                if alt.exists():
                    return (alt, None)

            # replace_smile_with_(1)
            if media_path.stem.find("-SMILE") != -1:
                alt = Path(str(media_path.parent) + '/' + media_path.stem[:-len('-SMILE')] + "(1)" + media_path.suffix)
                if alt.exists():
                    return (alt, None)

            # dash_before_ext_add_(1)
            alt = Path(str(media_path.parent) + '/' + str(media_path.stem) + "(1)" + str(media_path.suffix))
            if alt.exists():
                return (alt, None)

            # fb_n_suffix
            if media_path.stem.endswith('-n'):
                alt = Path(str(media_path.parent) + '/' + media_path.stem[:-2] + '_(1)' + media_path.suffix)
                if alt.exists():
                    return (alt, None)

            if media_path.stem.endswith('_n'):
                alt = Path(str(media_path.parent) + '/' + media_path.stem[:-2] + '_(1)' + media_path.suffix)
                if alt.exists():
                    return (alt, None)

            # fallback, check all, missing_end_before_(1), missing_end_prefix_match
            # make this into expanding search
            half_name = media_path.stem[:-(math.floor(len(media_path.stem) / 2))]
            nearest = media_path.stem[:-6]
            half_name_matches = []
            nearest_matches = []
            for p in media_path.parent.iterdir():
                if p.stem.startswith(half_name) and p.is_file() and p.suffix.lower() != '.json':
                    half_name_matches.append(p)
                if p.stem.startswith(nearest) and p.is_file() and p.suffix.lower() != '.json':
                    nearest_matches.append(p)

            if len(nearest_matches):
                matches = nearest_matches
            else:
                matches = half_name_matches

            if len(matches) == 1:
                return (matches[0], None)

            if len(matches) > 1:
                root = utils.app_config.ARGS.root

                # store this for later, and ask the user in the end
                # later = (json_path, media_path, matches)
                return (None, matches)

            # more than three matches? its some library or something
            return (None, None)

        return (media_path, None)
