"""Installer failures must not leave half-installed or overwrite edited skills."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import install_skills as installer

ROOT = Path(__file__).resolve().parents[1]


class InstallerAdversarialTests(unittest.TestCase):
    def test_failure_preparing_second_skill_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "installed"
            original = installer.shutil.copyfile

            def fail_on_second(source, destination, *args, **kwargs):
                if installer.SKILLS[1] in Path(source).parts:
                    raise OSError("Synthetic interrupted copy")
                return original(source, destination, *args, **kwargs)

            with patch.object(installer.shutil, "copyfile", fail_on_second):
                with self.assertRaises(OSError):
                    installer.install_skills(ROOT / "skills", target, Path(sys.executable))
            self.assertFalse(any((target / name).exists() for name in installer.SKILLS))

    def test_selective_install_does_not_copy_unrequested_skills(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            result = installer.install_skills(ROOT / "skills", target, Path(sys.executable),
                                              skills=("literature-research",))
            self.assertEqual([p.name for p in result], ["literature-research"])
            self.assertFalse((target / "paper-data-extraction").exists())

    def test_managed_update_keeps_backup_and_refuses_local_edits(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            installer.install_skills(ROOT / "skills", target, Path(sys.executable))
            before = (target / installer.SKILLS[0] / "SKILL.md").read_bytes()
            installer.install_skills(ROOT / "skills", target, Path(sys.executable), update=True)
            backups = list(target.glob(".skill-backup-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual((backups[0] / installer.SKILLS[0] / "SKILL.md").read_bytes(), before)
            edited = target / installer.SKILLS[1] / "SKILL.md"
            edited.write_text("personal change", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "modified"):
                installer.install_skills(ROOT / "skills", target, Path(sys.executable), update=True)
            self.assertEqual(edited.read_text(encoding="utf-8"), "personal change")

    def test_update_refuses_unmanaged_legacy_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            folder = target / installer.SKILLS[0]
            folder.mkdir()
            (folder / "runtime.json").write_text(json.dumps({"schema_version": 1, "python": sys.executable}))
            with self.assertRaisesRegex(ValueError, "managed"):
                installer.install_skills(ROOT / "skills", target, Path(sys.executable), update=True)

    def test_update_does_not_ignore_a_nested_personal_runtime_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            installer.install_skills(ROOT / "skills", target, Path(sys.executable))
            extra = target / installer.SKILLS[0] / "references/runtime.json"
            extra.write_text('{"personal":"keep"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "modified"):
                installer.install_skills(ROOT / "skills", target, Path(sys.executable), update=True)
            self.assertEqual(extra.read_text(encoding="utf-8"), '{"personal":"keep"}')

    def test_publish_failure_restores_all_previous_skills(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            installer.install_skills(ROOT / "skills", target, Path(sys.executable))
            before = {name: (target / name / "runtime.json").read_bytes() for name in installer.SKILLS}
            original = Path.rename

            def fail_second_stage(path, destination):
                if path.parent.name.startswith(".install-") and Path(destination).name == installer.SKILLS[1]:
                    raise OSError("Synthetic publish failure")
                return original(path, destination)

            with patch.object(Path, "rename", fail_second_stage), self.assertRaises(OSError):
                installer.install_skills(ROOT / "skills", target, Path(sys.executable), update=True)
            for name in installer.SKILLS:
                self.assertEqual((target / name / "runtime.json").read_bytes(), before[name])
