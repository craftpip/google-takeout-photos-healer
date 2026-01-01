#!/usr/bin/env python3
"""Find duplicate files under a root directory.

Usage examples:
  python duplicate.py --root /path/to/dir --finddupes

By default `--finddupes` will scan recursively under `--root`, group files by
size and then compute SHA256 to confirm identical content. Duplicate groups are
printed to stdout with one group per blank-line.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


def iter_files(root: Path, follow_symlinks: bool = False) -> Iterable[Path]:
    """Yield files under root (recursive)."""
    for p in root.rglob("*"):
        try:
            if p.is_file() and (follow_symlinks or not p.is_symlink()):
                yield p
        except Exception:
            # ignore files we can't stat
            continue


def file_hash(path: Path, block_size: int = 65536, algo: str = "sha256") -> str:
    """Compute a hex digest for `path` using the given algorithm.

    Streams the file so large files work without blowing memory.
    """
    h = hashlib.new(algo)
    with path.open("rb") as f:
        while True:
            block = f.read(block_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def find_duplicates(root: Path, min_size: int = 1, follow_symlinks: bool = False) -> Dict[str, List[Path]]:
    """Find duplicate files under `root`.

    Returns a mapping from content-hash -> list of files (Paths) that share that hash.
    Only returns entries where more than one file shares the same hash (actual duplicates).

    The function first groups by file size to reduce the number of hashes computed.
    """
    root = root.expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"root does not exist: {root}")

    # group paths by size
    by_size: Dict[int, List[Path]] = defaultdict(list)
    for p in iter_files(root, follow_symlinks=follow_symlinks):
        try:
            size = p.stat().st_size
        except Exception:
            continue
        if size < min_size:
            continue
        by_size[size].append(p)

    # for each size group with more than one file, compute checksums
    dupes: Dict[str, List[Path]] = defaultdict(list)
    for size, paths in by_size.items():
        if len(paths) <= 1:
            continue
        # compute hash for each file
        for p in paths:
            try:
                h = file_hash(p)
            except Exception:
                # if file unreadable skip it
                continue
            dupes[h].append(p)

    # filter to only keep real duplicates
    results = {h: ps for h, ps in dupes.items() if len(ps) > 1}
    return results


def print_dupes(dupes: Dict[str, List[Path]]) -> None:
    """Print duplicate groups to stdout."""
    if not dupes:
        print("No duplicates found.")
        return

    printed = 0
    for h, paths in sorted(dupes.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        print(f"Hash: {h}")
        for p in sorted(paths):
            print(f"  {p}")
        print("")
        printed += 1

    print(f"Found {printed} duplicate groups. (groups with >1 file)")


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find duplicate files by content under a root directory")
    parser.add_argument("--root", required=True, help="Root path to scan for duplicates")
    parser.add_argument("--finddupes", action="store_true", help="Run duplicate detection")
    parser.add_argument("--min-size", type=int, default=1, help="Minimum file size in bytes to consider")
    parser.add_argument("--follow-symlinks", action="store_true", help="Follow symbolic links")
    args = parser.parse_args(args=argv)

    root = Path(args.root)
    if not args.finddupes:
        parser.print_help()
        return 1

    try:
        dupes = find_duplicates(root, min_size=args.min_size, follow_symlinks=args.follow_symlinks)
    except FileNotFoundError as e:
        print(f"[ERROR] {e}")
        return 2

    print_dupes(dupes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
