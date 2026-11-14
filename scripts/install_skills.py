"""Install the three project skills and bind them to an existing Python environment.

This installer copies instructions only. It does not install dependencies, touch
Zotero, or change Office settings. Updates require an unchanged managed inventory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ("word-zotero-citations", "literature-research", "paper-data-extraction")
MODULES = {
    "word-zotero-citations": ("litsearch_zotero.dynamic_citations", "lxml.etree"),
    "literature-research": ("litsearch", "litsearch.discovery", "litsearch.discovery_bridge"),
    "paper-data-extraction": ("litsearch.document_parse",),
}


def redirected(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024))


def skill_files(folder: Path) -> list[Path]:
    if redirected(folder):
        raise ValueError("Skill source must not be redirected")
    files = []
    for path in folder.rglob("*"):
        if redirected(path):
            raise ValueError("Skill source contains a redirected path")
        if path.is_file():
            rel = path.relative_to(folder).as_posix()
            if (rel == "SKILL.md" or rel == "agents/openai.yaml"
                    or (rel.startswith("references/") and rel.endswith(".md"))):
                files.append(path)
            else:
                raise ValueError("Unexpected skill source file: " + rel)
    if not {"SKILL.md", "agents/openai.yaml"}.issubset(
            {p.relative_to(folder).as_posix() for p in files}):
        raise ValueError("Skill source is incomplete")
    return files


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_managed(folder: Path) -> None:
    if redirected(folder) or redirected(folder / "runtime.json"):
        raise ValueError("Managed skill must not be redirected")
    try:
        runtime = json.loads((folder / "runtime.json").read_text(encoding="utf-8"))
        managed = runtime["managed_files"]
        if runtime.get("schema_version") != 2 or not isinstance(managed, dict) or not managed:
            raise ValueError("Missing managed inventory")
        actual = {p.relative_to(folder).as_posix(): digest(p) for p in skill_files_for_update(folder)}
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Existing skill is not a managed installation; review it manually") from exc
    if actual != managed:
        raise ValueError("Existing skill was locally modified; preserve changes before updating")


def skill_files_for_update(folder: Path) -> list[Path]:
    # Private runtime configuration is deliberately outside the public inventory.
    paths = []
    for p in folder.rglob("*"):
        if redirected(p):
            raise ValueError("Managed skill contains a redirected path")
        if p.is_file() and p.relative_to(folder).as_posix() != "runtime.json":
            paths.append(p)
    return paths


def install_skills(source: Path, destination: Path, python: Path, *,
                   skills: tuple[str, ...] = SKILLS, update: bool = False) -> list[Path]:
    source = source.resolve()
    destination = Path(os.path.abspath(destination))
    python = python.resolve()
    if not python.is_file():
        raise ValueError("Python interpreter does not exist")
    if any(redirected(p) for p in (destination, *destination.parents)):
        raise ValueError("Skill destination must not be redirected")
    if not skills or len(set(skills)) != len(skills) or any(name not in SKILLS for name in skills):
        raise ValueError("Select known skills without duplicates")
    targets = [destination / name for name in skills]
    if not update and any(p.exists() or p.is_symlink() for p in targets):
        raise ValueError("An existing skill would be overwritten; choose a new destination or review it manually")
    for target in targets:
        if target.exists() or target.is_symlink():
            validate_managed(target)
    inventories = {name: skill_files(source / name) for name in skills}
    destination.mkdir(parents=True, exist_ok=True)
    lock = destination / ".literature-skills-install.lock"
    # Cooperative lock prevents two instances of this installer interleaving.
    with lock.open("x", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    stage = None
    backup = None
    installed: list[Path] = []
    moved: list[tuple[Path, Path]] = []
    try:
        stage = Path(tempfile.mkdtemp(prefix=".install-", dir=destination))
        for name in skills:
            folder = stage / name
            folder.mkdir()
            managed = {}
            for path in inventories[name]:
                rel = path.relative_to(source / name)
                out = folder / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, out)
                managed[rel.as_posix()] = digest(out)
            (folder / "runtime.json").write_text(json.dumps(
                {"schema_version": 2, "python": str(python), "managed_files": managed}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
        for name, target in zip(skills, targets):
            if target.exists() or target.is_symlink():
                if not update:
                    raise ValueError("Skill destination appeared during installation")
                validate_managed(target)
                if backup is None:
                    backup = Path(tempfile.mkdtemp(prefix=".skill-backup-", dir=destination))
                saved = backup / name
                target.rename(saved)
                moved.append((target, saved))
            (stage / name).rename(target)
            installed.append(target)
        return installed
    except Exception:
        # These directories were published by this invocation only. Previous
        # installations remain in the backup until restoration succeeds.
        for target in reversed(installed):
            validate_managed(target)
            shutil.rmtree(target)
        for target, saved in reversed(moved):
            saved.rename(target)
        raise
    finally:
        if stage is not None and stage.exists() and stage.parent.resolve() == destination.resolve():
            shutil.rmtree(stage)
        lock.unlink()


def check_runtime(python: Path, skills: tuple[str, ...] = SKILLS) -> None:
    modules = list(dict.fromkeys(module for skill in skills for module in MODULES[skill]))
    probe = subprocess.run([str(python), "-c",
        "import importlib, sys; assert sys.version_info >= (3,10); "
        "[importlib.import_module(name) for name in " + repr(modules) + "]"],
        cwd=python.parent, capture_output=True, text=True, encoding="utf-8", timeout=30)
    if probe.returncode:
        raise ValueError('Selected Python cannot load the selected skill modules/extras; install the needed project extras. '
                         + probe.stderr.strip())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_home = Path(os.environ["CODEX_HOME"]) if os.environ.get("CODEX_HOME") else Path.home() / ".codex"
    parser.add_argument("--destination", type=Path, default=default_home / "skills")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--skill", action="append", choices=SKILLS, help="Install only the selected skill(s)")
    parser.add_argument("--update", action="store_true", help="Update unchanged managed skills, retaining backups")
    args = parser.parse_args(argv)
    try:
        python = args.python.resolve()
        skills = tuple(args.skill) if args.skill else SKILLS
        check_runtime(python, skills)
        installed = install_skills(ROOT / "skills", args.destination, python, skills=skills, update=args.update)
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print("Skill installation failed: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"installed": [str(p) for p in installed], "python": str(python)}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
