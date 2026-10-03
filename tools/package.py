#!/usr/bin/env python3
"""Build the distributable zip in the layout Decky expects.

The loader only looks for ``<plugin>/main.py`` and ``<plugin>/dist/index.js``
(decky_loader/loader.py). The wiki's settings page warns that a zip whose
structure does not match the documented layout is an "improperly packaged
plugin", so development-only files are kept out of the artefact rather than
merely ignored.

Run the checks first; this script refuses to package a failing tree.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Iterable, List, Sequence

ROOT = Path(__file__).resolve().parent.parent
STAGE = ROOT.parent / ".rayneo-pkg"

#: Files and directories that ship, per the documented plugin layout.
INCLUDE = (
    "main.py",
    "plugin.json",
    "package.json",
    "pnpm-lock.yaml",
    "LICENSE",
    "README.md",
    "src",
    "dist",
    "py_modules",
    # The README links into these, so they ship with it rather than dangling.
    "docs",
)

#: Never packaged, whatever else happens.
EXCLUDE_DIRS = {"__pycache__", "node_modules", ".git", ".ruff_cache", ".mypy_cache"}
EXCLUDE_SUFFIXES = (".pyc", ".map", ".log")
EXCLUDE_NAMES = {"defaults.txt", ".DS_Store", "Thumbs.db"}


def run(cmd: Sequence[str], cwd: Path = ROOT) -> None:
    print(f"  $ {' '.join(cmd)}")
    proc = subprocess.run(list(cmd), cwd=cwd)
    if proc.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(cmd)}")


def checks() -> None:
    print("=== checks ===")
    run(["ruff", "check", "--select=F,E9", "main.py", "py_modules", "tools"])
    run([sys.executable, "-m", "py_compile", "main.py", "tools/structcheck.py"])
    run([sys.executable, "tools/structcheck.py"])
    # The frontend is built here, not assumed -- and before smoke_test, because
    # smoke_test checks that dist/ is newer than src/. Running it first made the
    # freshness check fire on the very staleness this build exists to fix, so
    # the pipeline could not repair itself.
    #
    # The reason it is here at all: package.py used to ship whatever happened to
    # be in dist/. A change to src/types.ts never reached the bundle and the zip
    # carried a field the source no longer had -- every check passed, because
    # every check reads src/, and nothing compared dist/ to it. That is the
    # "tested a stale build" failure mode with a cause, and the cause was that
    # nothing rebuilt.
    run(["pnpm", "run", "build"])
    stale = newest_stale()
    if stale:
        raise SystemExit(
            "dist/ is older than " + ", ".join(str(p) for p in stale)
            + " -- the build did not pick the change up"
        )
    run([sys.executable, "tools/smoke_test.py", "."])



def newest_stale() -> List[Path]:
    """Source files newer than the built bundle, if any.

    A build that silently does nothing looks exactly like a successful one, so
    the result is compared rather than trusted.
    """
    bundle = ROOT / "dist" / "index.js"
    if not bundle.exists():
        return [bundle]
    built = bundle.stat().st_mtime
    return [p for p in sorted((ROOT / "src").rglob("*"))
            if p.is_file() and p.stat().st_mtime > built]


def wanted(path: Path) -> bool:
    if path.name in EXCLUDE_NAMES:
        return False
    if any(part in EXCLUDE_DIRS for part in path.parts):
        return False
    return not path.name.endswith(EXCLUDE_SUFFIXES)


def stage() -> Path:
    print("=== staging ===")
    if STAGE.exists():
        shutil.rmtree(STAGE)
    target = STAGE / ROOT.name
    target.mkdir(parents=True)

    missing = [name for name in INCLUDE if not (ROOT / name).exists()]
    if missing:
        raise SystemExit(f"missing from the plugin: {', '.join(missing)}")

    for name in INCLUDE:
        src = ROOT / name
        dst = target / name
        if src.is_dir():
            for path in sorted(src.rglob("*")):
                if not path.is_file() or not wanted(path.relative_to(ROOT)):
                    continue
                out = dst / path.relative_to(src)
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, out)
        else:
            shutil.copy2(src, dst)
    return target


def verify_required(target: Path) -> None:
    """The two files the loader hard-codes."""
    print("=== layout ===")
    for rel in ("main.py", "dist/index.js", "plugin.json", "package.json",
                "LICENSE", "py_modules"):
        if not (target / rel).exists():
            raise SystemExit(f"packaged plugin is missing {rel}")
        print(f"  OK  {rel}")
    strays: List[str] = []
    for path in sorted(target.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(target)
        if rel.parts[0] not in INCLUDE:
            strays.append(str(rel))
    if strays:
        raise SystemExit(f"unexpected files in the artefact: {strays}")
    print("  OK  no development files leaked in")


def build_zip(target: Path) -> Path:
    print("=== packaging ===")
    out = ROOT.parent / f"{ROOT.name}.zip"
    if out.exists():
        out.unlink()
    # ZIP_DEFLATED at level 9; the artefact is a few hundred KB of text.
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(target.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(STAGE))
    return out


def main() -> int:
    checks()
    target = stage()
    verify_required(target)
    out = build_zip(target)
    size = out.stat().st_size
    print(f"\n  {out}  ({size / 1024:.0f} KB)")
    with zipfile.ZipFile(out) as zf:
        names: Iterable[str] = zf.namelist()
        print(f"  {len(list(names))} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())