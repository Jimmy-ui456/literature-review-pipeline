"""Generate disposable release artifacts from this source checkout, never a mirror."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import zipfile

if __package__:
    from .release_check import inspect_archive, inspect_content, inspect_paths, load_config, prepare_stage, source_files
else:
    from release_check import inspect_archive, inspect_content, inspect_paths, load_config, prepare_stage, source_files


ROOT = Path(__file__).resolve().parents[1]


def reject_redirected_path(path: Path) -> None:
    """Check unresolved parents too: resolve() alone would hide a junction escape."""
    for candidate in (path, *path.parents):
        try:
            attributes = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(attributes.st_mode) or (
            getattr(attributes, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)
        ):
            raise ValueError("release output must not traverse a symlink or reparse point")


def output_path(root: Path, output: Path) -> Path:
    root = root.resolve()
    unresolved = Path(os.path.abspath(output if output.is_absolute() else root / output))
    reject_redirected_path(unresolved)
    # Resolve Windows short-name aliases only after checking raw parents for
    # reparse points; otherwise an alias can evade the containment comparison.
    target = unresolved.resolve()
    dist = root / "dist"
    if target == dist or dist not in target.parents:
        raise ValueError("--out-dir must be a NEW child directory inside this checkout's dist directory")
    reject_redirected_path(target)
    if target.exists():
        raise ValueError("release output already exists; choose a new directory (no overwrites)")
    return target


def fingerprint(path: Path) -> dict:
    payload = path.read_bytes()
    return {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


def build_archives(stage: Path, artifacts: Path) -> list[Path]:
    # Dependencies are installed explicitly by the developer/CI, not downloaded by
    # an implicit isolated build. Build artifacts never touch the source checkout.
    subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(artifacts), str(stage)],
        check=True,
    )
    wheels = sorted(artifacts.glob("*.whl"))
    sdists = sorted(artifacts.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("build must produce exactly one wheel and one sdist")
    return wheels + sdists


def build_release(root: Path, output: Path, *, source_only: bool = False) -> dict:
    root = root.resolve()
    target = output_path(root, output)
    config = load_config(root)
    required = ("README.md", "LICENSE", "pyproject.toml", "release_allowlist.json", "litsearch/__init__.py")
    if any(not (root / name).is_file() for name in required):
        raise ValueError("source checkout is missing a required release input")

    stage = target / "source"
    findings, selected, excluded = prepare_stage(root, stage, config)
    if not findings:
        checked = source_files(stage)
        findings = inspect_paths(stage, checked, config) + inspect_content(stage, checked)
    if findings:
        raise ValueError("source release gate failed: " + json.dumps(findings, ensure_ascii=False))

    selected.sort()
    inputs = [{"path": name, **fingerprint(stage / name)} for name in selected]
    artifacts = target / "artifacts"
    artifacts.mkdir()
    archives = [] if source_only else build_archives(stage, artifacts)
    archive_checks = [inspect_archive(archive, config) for archive in archives]
    if any(check["result"] != "pass" for check in archive_checks):
        raise ValueError("wheel/sdist release gate failed: " + json.dumps(
            [finding for check in archive_checks for finding in check["findings"]], ensure_ascii=False))
    if inputs != [{"path": name, **fingerprint(stage / name)} for name in selected]:
        raise ValueError("build changed a selected source input; no successful manifest was written")

    source_zip = artifacts / "literature-search-source.zip"
    with zipfile.ZipFile(source_zip, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in selected:
            member = zipfile.ZipInfo("literature-search/" + name, date_time=(1980, 1, 1, 0, 0, 0))
            member.compress_type = zipfile.ZIP_DEFLATED
            member.create_system = 3
            member.external_attr = 0o100644 << 16
            archive.writestr(member, (stage / name).read_bytes())

    report = {
        "schema_version": 1,
        "generated": True,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_of_truth": "the checkout containing scripts/build_release.py",
        "mode": "source_only" if source_only else "source_wheel_sdist",
        "checks": {"source_gate": "pass", "archive_gate": "not_run" if source_only else "pass"},
        "source_files": inputs,
        "excluded_generated_files": len(excluded),
        "artifacts": [{"path": path.relative_to(target).as_posix(), **fingerprint(path)}
                      for path in [source_zip, *archives]],
        "result": "pass",
    }
    # This file is the success marker. Failed attempts stay inspectable but do not
    # get a pass marker; a retry must use a different output directory.
    with (target / "release_manifest.json").open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=Path("dist") / datetime.now(timezone.utc).strftime("release-%Y%m%dT%H%M%SZ"))
    parser.add_argument("--source-only", action="store_true", help="gate and zip source only; do not claim wheel/sdist validation")
    args = parser.parse_args(argv)
    try:
        report = build_release(ROOT, args.out_dir, source_only=args.source_only)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"release failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"result": report["result"], "mode": report["mode"], "output": str(ROOT / args.out_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
