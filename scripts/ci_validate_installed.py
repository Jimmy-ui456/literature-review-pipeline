"""Validate the installed wheel from a checkout-independent temporary fixture."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def run(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(command, cwd=cwd, env=env, text=True, encoding="utf-8", capture_output=True)
    if process.returncode:
        print(process.stdout, end="")
        print(process.stderr, end="", file=sys.stderr)
        raise SystemExit(process.returncode)
    return process


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-source", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--demo-out", type=Path, required=True)
    args = parser.parse_args()

    source_root = args.source_root.resolve()
    fixture_source = args.fixture_source.resolve()
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONUTF8"] = "1"
    with tempfile.TemporaryDirectory(prefix="litsearch-ci-") as temporary:
        fixture = Path(temporary)
        for name in ("tests", "scripts", "examples"):
            shutil.copytree(fixture_source / name, fixture / name)
        shutil.copy2(fixture_source / "release_allowlist.json", fixture / "release_allowlist.json")

        probe = run(
            [
                sys.executable,
                "-c",
                "import json, litsearch, litsearch_zotero; "
                "print(json.dumps({'litsearch': litsearch.__file__, 'litsearch_zotero': litsearch_zotero.__file__}))",
            ],
            cwd=fixture,
            env=env,
        )
        locations = json.loads(probe.stdout)
        for name, value in locations.items():
            module_path = Path(value).resolve()
            source_package = source_root / name
            if module_path == source_package or source_package in module_path.parents:
                raise SystemExit(f"{name} imported from checkout package: {module_path}")
            if "site-packages" not in {part.casefold() for part in module_path.parts}:
                raise SystemExit(f"{name} was not imported from site-packages: {module_path}")
        print(json.dumps({"installed_sources": locations}, ensure_ascii=False))

        for module in ("service_probe", "discovery", "document_parse"):
            run([sys.executable, "-m", "litsearch." + module, "--help"], cwd=fixture, env=env)
        for module in ("dynamic_citations", "word_plugin"):
            run([sys.executable, "-m", "litsearch_zotero." + module, "--help"], cwd=fixture, env=env)
        run([sys.executable, "-c", "import lxml.etree"], cwd=fixture, env=env)

        run([sys.executable, "-X", "utf8", "-m", "unittest", "discover", "-s", "tests", "-q"], cwd=fixture, env=env)
        demo_out = args.demo_out.resolve()
        demo_out.parent.mkdir(parents=True, exist_ok=True)
        run(
            [sys.executable, "-X", "utf8", str(fixture / "scripts" / "run_synthetic_demo.py"), "--out-dir", str(demo_out)],
            cwd=fixture,
            env=env,
        )
        run(
            [sys.executable, "-X", "utf8", str(fixture / "scripts" / "run_engineering_tools_demo.py"),
             "--out-dir", str(demo_out.with_name(demo_out.name + "-tools"))],
            cwd=fixture,
            env=env,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
