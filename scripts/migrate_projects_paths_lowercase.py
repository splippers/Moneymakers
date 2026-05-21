#!/usr/bin/env python3
"""
Lowercase every workspace directory segment under a Projects root (Linux),
merge duplicate clones, remove known empty stubs, then rewrite path strings
in text files.

Always dry-run unless --apply.

  python3 migrate_projects_paths_lowercase.py --root /mnt/EDDIE-SANDIEGO/Projects --dry-run
  python3 migrate_projects_paths_lowercase.py --root /mnt/EDDIE-SANDIEGO/Projects --apply \\
      --also-mount /mnt/MARVIN-SANDIEGO/Projects
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Tooling expects fixed casing (Unity, Xcode). Do not lowercase these segments.
NEVER_LOWER_SEGMENT = frozenset(
    {
        "Assets",
        "Library",
        "Packages",
        "ProjectSettings",
        "LocalPackages",
        "xcuserdata",
        "xcshareddata",
        "xcassets",
    }
)

SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".next",
        "dist",
        "build",
        ".turbo",
        "target",
        ".cargo",
    }
)

# Huge generated trees — do not descend (performance).
SKIP_DESCEND_NAMES = frozenset({"Library", "Pods", "DerivedData", ".gradle"})


def prune_walk(dirnames: list[str]) -> None:
    dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIR_NAMES and d not in SKIP_DESCEND_NAMES)


TEXT_SUFFIXES = frozenset(
    {
        ".md",
        ".txt",
        ".py",
        ".sh",
        ".yml",
        ".yaml",
        ".json",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".service",
        ".env",
        ".example",
        ".sample",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".html",
        ".htm",
        ".css",
        ".scss",
        ".sql",
        ".xml",
        ".properties",
        ".gradle",
        ".java",
        ".kt",
        ".go",
        ".rs",
        ".dockerignore",
        ".gitignore",
        ".editorconfig",
        "",
    }
)


def merge_duplicate(src: Path, dst: Path, *, dry_run: bool) -> None:
    print(f"[merge] rsync --exclude .git/ {src}/ -> {dst}/")
    if dry_run:
        return
    subprocess.run(
        ["rsync", "-a", "--exclude", ".git/", f"{src}{os.sep}", f"{dst}{os.sep}"],
        check=True,
    )
    shutil.rmtree(src)


def remove_empty(path: Path, *, dry_run: bool) -> None:
    print(f"[rmdir-empty] {path}")
    if dry_run:
        return
    if not path.is_dir():
        return
    if any(path.iterdir()):
        raise SystemExit(f"refuse — not empty: {path}")
    path.rmdir()


def walk_pruned(root: Path):
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        p = Path(dirpath).resolve()
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if ".git" in rel.parts:
            dirnames[:] = []
            continue
        prune_walk(dirnames)
        yield p, dirnames, filenames


def lowered_parts(parts: tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    for seg in parts:
        out.append(seg if seg in NEVER_LOWER_SEGMENT else seg.lower())
    return tuple(out)


def path_with_segments_lowercased(root: Path, full: Path) -> Path:
    full = full.resolve()
    rel = full.relative_to(root)
    return root.joinpath(*lowered_parts(tuple(rel.parts)))


def collect_full_path_substitutions(root: Path) -> list[tuple[str, str]]:
    root = root.resolve()
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for p, _dns, _fn in walk_pruned(root):
        if p == root:
            continue
        new_p = path_with_segments_lowercased(root, p)
        if p.resolve() == new_p.resolve():
            continue
        key = (p.as_posix(), new_p.as_posix())
        if key not in seen:
            seen.add(key)
            pairs.append(key)
    pairs.sort(key=lambda x: -len(x[0]))
    return pairs


def relative_projects_substitutions(root: Path) -> list[tuple[str, str]]:
    root = root.resolve()
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for p, _dns, _fn in walk_pruned(root):
        if p == root:
            continue
        new_p = path_with_segments_lowercased(root, p)
        if p.resolve() == new_p.resolve():
            continue
        rel_old = p.relative_to(root).as_posix()
        rel_new = new_p.relative_to(root).as_posix()
        if rel_old == rel_new:
            continue
        for a, b in (
            (f"Projects/{rel_old}", f"Projects/{rel_new}"),
            (f"Projects/{rel_old}/", f"Projects/{rel_new}/"),
            (f"../{rel_old}", f"../{rel_new}"),
            (f"../{rel_old}/", f"../{rel_new}/"),
        ):
            k = (a, b)
            if k not in seen:
                seen.add(k)
                out.append(k)
    out.sort(key=lambda x: -len(x[0]))
    return out


def mirror_mount_substitutions(
    full_pairs: list[tuple[str, str]], root: Path, mounts: list[Path]
) -> list[tuple[str, str]]:
    rp = root.resolve().as_posix()
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for mount in mounts:
        mp = mount.resolve().as_posix()
        if mp == rp:
            continue
        for old, new in full_pairs:
            if not old.startswith(rp):
                continue
            suf_old = old[len(rp) :]
            suf_new = new[len(rp) :]
            k = (mp + suf_old, mp + suf_new)
            if k not in seen:
                seen.add(k)
                out.append(k)
    out.sort(key=lambda x: -len(x[0]))
    return out


def build_dir_rename_plan(root: Path) -> list[tuple[Path, Path]]:
    dirs: list[Path] = []
    for p, _dns, _fn in walk_pruned(root):
        if p == root:
            continue
        dirs.append(p)
    dirs.sort(key=lambda x: (-len(x.parts), str(x)))

    plan: list[tuple[Path, Path]] = []
    for p in dirs:
        if p.name in NEVER_LOWER_SEGMENT or p.name == p.name.lower():
            continue
        dest = p.with_name(p.name.lower())
        plan.append((p, dest))
    return plan


def apply_dir_renames(plan: list[tuple[Path, Path]], *, dry_run: bool) -> None:
    for src, dst in plan:
        if not src.exists():
            continue
        if src.name == src.name.lower():
            continue
        if dst.exists():
            if dst.resolve() == src.resolve():
                continue
            raise SystemExit(f"target exists — resolve duplicates first:\n  {src}\n  {dst}")
        print(f"[rename-dir] {src} -> {dst}")
        if dry_run:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.rename(src, dst)


def is_probably_text(path: Path) -> bool:
    suf = path.suffix.lower()
    if suf in TEXT_SUFFIXES:
        return True
    name = path.name.lower()
    if name in {".gitignore", ".dockerignore", "dockerfile"}:
        return True
    if name.startswith("dockerfile."):
        return True
    return False


def rewrite_files(root: Path, subs: list[tuple[str, str]], *, dry_run: bool) -> int:
    changed = 0
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        p = Path(dirpath).resolve()
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if ".git" in rel.parts:
            dirnames[:] = []
            continue
        prune_walk(dirnames)
        for fn in filenames:
            path = Path(dirpath) / fn
            if path.is_symlink() or not path.is_file():
                continue
            try:
                if path.stat().st_size > 12_000_000:
                    continue
            except OSError:
                continue
            if not is_probably_text(path):
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:8192]:
                continue
            text = data.decode("utf-8", errors="surrogateescape")
            orig = text
            for old, new in subs:
                if old in text:
                    text = text.replace(old, new)
            if text != orig:
                changed += 1
                print(f"[patch] {path}")
                if not dry_run:
                    path.write_bytes(text.encode("utf-8", errors="surrogateescape"))
    return changed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path("/mnt/EDDIE-SANDIEGO/Projects"))
    ap.add_argument("--also-mount", action="append", default=[], help="Duplicate absolute substitutions for this mount.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true", help="Actually merge, rename dirs, and rewrite files.")
    ap.add_argument(
        "--skip-text-phase",
        action="store_true",
        help="Skip rewriting file contents (still computes substitution keys from directory layout).",
    )
    args = ap.parse_args()

    root = args.root.resolve()
    if not root.is_dir():
        sys.exit(f"missing root {root}")

    dry_run = not args.apply

    print(f"=== root={root} dry_run={dry_run} ===")

    merge_pairs = [(root / "Moneymakers", root / "moneymakers"), (root / "MassDeb8", root / "massdeb8")]
    for src, dst in merge_pairs:
        if src.exists() and dst.exists():
            merge_duplicate(src, dst, dry_run=dry_run)
        elif src.exists() and not dst.exists():
            print(f"[rename-merge] {src} -> {dst}")
            if not dry_run:
                os.rename(src, dst)

    bore_stub = root / "BoreDOOM"
    if bore_stub.exists():
        remove_empty(bore_stub, dry_run=dry_run)

    full_pairs = collect_full_path_substitutions(root)
    mounts = [root, *[Path(p).resolve() for p in args.also_mount]]
    mirror_pairs = mirror_mount_substitutions(full_pairs, root, mounts)
    rel_pairs = relative_projects_substitutions(root)

    dedup: dict[str, str] = {}
    for o, n in full_pairs + mirror_pairs + rel_pairs:
        dedup[o] = n
    merged_subs = sorted(dedup.items(), key=lambda x: -len(x[0]))

    plan = build_dir_rename_plan(root)
    print(f"planned directory case fixes: {len(plan)}")
    apply_dir_renames(plan, dry_run=dry_run)

    n = 0
    if not args.skip_text_phase:
        n = rewrite_files(root, merged_subs, dry_run=dry_run)
    print(f"=== text files touched: {n} ===")


if __name__ == "__main__":
    main()
