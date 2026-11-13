"""Install the three project skills and bind them to an existing Python environment.

This installer copies instructions only. It does not install dependencies, touch
Zotero, change Office settings, or overwrite an existing personal skill.
"""
from __future__ import annotations

import argparse
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


def install_skills(source: Path, destination: Path, python: Path) -> list[Path]:
    source = source.resolve()
    destination = Path(os.path.abspath(destination))
    python = python.resolve()
    if not python.is_file():
        raise ValueError("Python interpreter does not exist")
    if any(redirected(p) for p in (destination, *destination.parents)):
        raise ValueError("Skill destination must not be redirected")
    targets = [destination / name for name in SKILLS]
    if any(p.exists() or p.is_symlink() for p in targets):
        raise ValueError("An existing skill would be overwritten; choose a new destination or review it manually")
    inventories = {name: skill_files(source / name) for name in SKILLS}
    destination.mkdir(parents=True, exist_ok=True)
    installed = []
    for name, target in zip(SKILLS, targets):
        stage = Path(tempfile.mkdtemp(prefix=".install-", dir=destination))
        try:
            for path in inventories[name]:
                out = stage / path.relative_to(source / name)
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, out)
            (stage / "runtime.json").write_text(json.dumps(
                {"schema_version": 1, "python": str(python)}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            # rename refuses existing targets on Windows; a concurrent install
            # should be inspected rather than merged into a personal skill.
            if target.exists() or target.is_symlink():
                raise ValueError("Skill destination appeared during installation")
            stage.rename(target)
            installed.append(target)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    return installed


def check_runtime(python: Path) -> None:
    probe = subprocess.run([str(python), "-c",
        "import litsearch, litsearch.discovery, litsearch.document_parse; "
        "import litsearch_zotero.dynamic_citations, litsearch_zotero.word_plugin; "
        "import lxml.etree, pdfplumber, pypdfium2, PIL; "
        "import sys; assert sys.version_info >= (3,10); "
        "__import__('win32com.client') if sys.platform == 'win32' else None"],
        cwd=python.parent, capture_output=True, text=True, encoding="utf-8", timeout=30)
    if probe.returncode:
        raise ValueError('Selected Python cannot load project extras; install ".[parsing,citations]" there first. '
                         + probe.stderr.strip())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_home = Path(os.environ["CODEX_HOME"]) if os.environ.get("CODEX_HOME") else Path.home() / ".codex"
    parser.add_argument("--destination", type=Path, default=default_home / "skills")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args(argv)
    try:
        python = args.python.resolve()
        check_runtime(python)
        installed = install_skills(ROOT / "skills", args.destination, python)
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print("Skill installation failed: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"installed": [str(p) for p in installed], "python": str(python)}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
