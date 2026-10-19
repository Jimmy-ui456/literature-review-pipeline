from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from scripts import build_release as release


ROOT = Path(__file__).resolve().parents[1]


class BuildReleaseTests(unittest.TestCase):
    def candidate(self, temporary: str) -> Path:
        root = (Path(temporary) / "checkout").resolve()
        (root / "litsearch").mkdir(parents=True)
        (root / "release_allowlist.json").write_bytes((ROOT / "release_allowlist.json").read_bytes())
        for name, text in {"README.md": "Synthetic release fixture\n", "LICENSE": "Synthetic license\n",
                           "pyproject.toml": "# synthetic fixture\n", "litsearch/__init__.py": "# synthetic\n"}.items():
            (root / name).write_text(text, encoding="utf-8")
        return root

    def test_source_release_is_allowlisted_and_manifest_matches_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.candidate(temp)
            (root / ".validation").mkdir()
            (root / ".validation" / "private.log").write_text("never ship", encoding="utf-8")
            with patch.object(release, "build_archives") as build:
                report = release.build_release(root, Path("dist/one"), source_only=True)
            build.assert_not_called()
            output = root / "dist/one"
            self.assertEqual(report, json.loads((output / "release_manifest.json").read_text(encoding="utf-8")))
            self.assertEqual(report["checks"]["archive_gate"], "not_run")
            self.assertTrue(report["generated"])
            with zipfile.ZipFile(output / report["artifacts"][0]["path"]) as archive:
                self.assertEqual(set(archive.namelist()), {"literature-search/" + item["path"] for item in report["source_files"]})
                for item in report["source_files"]:
                    data = archive.read("literature-search/" + item["path"])
                    self.assertEqual(data, (root / item["path"]).read_bytes())
                    self.assertEqual(item["sha256"], hashlib.sha256(data).hexdigest())
                    self.assertEqual(item["bytes"], len(data))
            for item in report["artifacts"]:
                self.assertEqual(release.fingerprint(output / item["path"]), {key: item[key] for key in ("bytes", "sha256")})

    def test_source_zip_is_reproducible_and_does_not_reimport_old_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.candidate(temp)
            for name in ("one", "two"):
                release.build_release(root, Path("dist") / name, source_only=True)
            self.assertEqual((root / "dist/one/artifacts/literature-search-source.zip").read_bytes(),
                             (root / "dist/two/artifacts/literature-search-source.zip").read_bytes())

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.candidate(temp)
            output = root / "dist/one"
            output.mkdir(parents=True)
            (output / "keep.txt").write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "already exists"):
                release.build_release(root, output, source_only=True)
            self.assertEqual((output / "keep.txt").read_text(), "keep")
            self.assertEqual([path.name for path in output.iterdir()], ["keep.txt"])

    def test_output_must_be_new_child_of_dist(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.candidate(temp)
            for output in (root, root.parent, root / "dist", Path("litsearch/new"), Path("dist/../litsearch/new")):
                with self.subTest(output=output), self.assertRaisesRegex(ValueError, "inside this checkout"):
                    release.build_release(root, output, source_only=True)
            self.assertFalse((root / "litsearch/new").exists())

    @unittest.skipUnless(__import__("os").name == "nt", "Windows short paths only")
    def test_short_name_output_alias_is_resolved_within_the_same_root(self):
        import ctypes
        with tempfile.TemporaryDirectory() as temp:
            root = self.candidate(temp)
            buffer = ctypes.create_unicode_buffer(32768)
            if not ctypes.windll.kernel32.GetShortPathNameW(str(root), buffer, len(buffer)):
                self.skipTest("Filesystem does not expose short-name aliases")
            short = Path(buffer.value)
            self.assertEqual(release.output_path(root, short / "dist/one"), root / "dist/one")

    def test_missing_required_source_fails_before_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.candidate(temp)
            (root / "LICENSE").unlink()
            with self.assertRaisesRegex(ValueError, "required release input"):
                release.build_release(root, Path("dist/one"), source_only=True)
            self.assertFalse((root / "dist").exists())

    def test_unknown_source_file_fails_without_success_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.candidate(temp)
            (root / "private-notes.txt").write_text("do not distribute", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source release gate failed"):
                release.build_release(root, Path("dist/one"), source_only=True)
            self.assertFalse((root / "dist/one/release_manifest.json").exists())
            self.assertFalse((root / "dist/one/source/private-notes.txt").exists())

    def test_sensitive_text_fails_without_success_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.candidate(temp)
            (root / "README.md").write_text('to' + 'ken=' + json.dumps("synthetic-value-12345"), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source release gate failed"):
                release.build_release(root, Path("dist/one"), source_only=True)
            self.assertFalse((root / "dist/one/release_manifest.json").exists())

    def test_junction_parent_is_rejected_before_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.candidate(temp)
            redirected = root / "dist"
            original = Path.lstat

            def attributes(path, *args, **kwargs):
                if path == redirected:
                    return SimpleNamespace(st_mode=stat.S_IFDIR,
                                           st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024))
                return original(path, *args, **kwargs)

            with patch.object(Path, "lstat", attributes), self.assertRaisesRegex(ValueError, "reparse point"):
                release.build_release(root, Path("dist/one"), source_only=True)
            self.assertFalse(redirected.exists())

    def test_failed_backend_does_not_create_success_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.candidate(temp)
            error = subprocess.CalledProcessError(1, ["synthetic-build"])
            with patch.object(release, "build_archives", side_effect=error), self.assertRaises(subprocess.CalledProcessError):
                release.build_release(root, Path("dist/one"))
            self.assertFalse((root / "dist/one/release_manifest.json").exists())

    def test_failed_archive_gate_does_not_create_success_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.candidate(temp)
            with patch.object(release, "build_archives", return_value=[Path("fake.whl")]), \
                 patch.object(release, "inspect_archive", return_value={"result": "fail", "findings": [{"rule": "missing"}]}), \
                 self.assertRaisesRegex(ValueError, "wheel/sdist release gate failed"):
                release.build_release(root, Path("dist/one"))
            self.assertFalse((root / "dist/one/release_manifest.json").exists())

    def test_backend_cannot_silently_change_selected_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.candidate(temp)

            def change_source(stage, artifacts):
                (stage / "README.md").write_text("changed by backend", encoding="utf-8")
                return []

            with patch.object(release, "build_archives", side_effect=change_source), \
                 self.assertRaisesRegex(ValueError, "build changed"):
                release.build_release(root, Path("dist/one"))
            self.assertFalse((root / "dist/one/release_manifest.json").exists())

    def test_backend_uses_this_interpreter_and_explicit_nonisolated_build(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.candidate(temp)
            artifacts = root / "artifacts"
            artifacts.mkdir()
            (artifacts / "one.whl").touch()
            (artifacts / "one.tar.gz").touch()
            with patch.object(release.subprocess, "run") as run:
                self.assertEqual(len(release.build_archives(root, artifacts)), 2)
            command = run.call_args.args[0]
            self.assertEqual(command[:5], [release.sys.executable, "-m", "build", "--no-isolation", "--outdir"])
            self.assertEqual(command[-1], str(root))
            self.assertTrue(run.call_args.kwargs["check"])


if __name__ == "__main__":
    unittest.main()
