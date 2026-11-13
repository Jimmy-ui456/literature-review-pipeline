"""Exercise skill installation and the real offline commands used by the skills."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.install_skills import SKILLS, check_runtime, install_skills
from scripts.run_synthetic_demo import network_blocked

ROOT = Path(__file__).resolve().parents[1]


class SkillInstallationTests(unittest.TestCase):
    def test_install_preserves_resources_and_binds_real_interpreter(self):
        with tempfile.TemporaryDirectory(prefix="skills space ") as temporary:
            target = Path(temporary) / "技能"
            installed = install_skills(ROOT / "skills", target, Path(sys.executable))
            self.assertEqual([p.name for p in installed], list(SKILLS))
            for folder in installed:
                runtime = json.loads((folder / "runtime.json").read_text(encoding="utf-8"))
                self.assertEqual(Path(runtime["python"]), Path(sys.executable).resolve())
                for source in (ROOT / "skills" / folder.name).rglob("*"):
                    if source.is_file():
                        self.assertEqual(source.read_bytes(), (folder / source.relative_to(
                            ROOT / "skills" / folder.name)).read_bytes())

    def test_existing_skill_blocks_entire_install_without_overwriting(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            existing = target / SKILLS[1]
            existing.mkdir()
            (existing / "personal.md").write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "overwritten"):
                install_skills(ROOT / "skills", target, Path(sys.executable))
            self.assertEqual(list(target.iterdir()), [existing])
            self.assertEqual((existing / "personal.md").read_text(), "keep")

    def test_private_file_cannot_be_accidentally_copied(self):
        import shutil
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            shutil.copytree(ROOT / "skills", source)
            (source / SKILLS[0] / "library_snapshot.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "Unexpected"):
                install_skills(source, root / "installed", Path(sys.executable))
            self.assertFalse((root / "installed").exists())

    def test_missing_runtime_modules_fail_explicitly(self):
        with patch("scripts.install_skills.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 1, "", "ModuleNotFoundError")):
            with self.assertRaisesRegex(ValueError, "extras"):
                check_runtime(Path(sys.executable))


class SkillWorkflowTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("lxml"), "citations extra not installed")
    def test_citation_commands_preserve_source_and_distinguish_pending_refresh(self):
        from litsearch_zotero.dynamic_citations import main
        from tests.extensions.test_dynamic_citations import docx, item
        with tempfile.TemporaryDirectory() as temporary, network_blocked():
            root = Path(temporary)
            source = root / "review.docx"
            docx(source)
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            snapshot = root / "library.json"
            snapshot.write_text(json.dumps([item()]), encoding="utf-8")
            plan, converted, checked = root / "plan.json", root / "copy.docx", root / "checked.json"
            self.assertEqual(main(["prepare", str(source), "--snapshot", str(snapshot), "--out", str(plan)]), 0)
            self.assertEqual(main(["convert", "--plan", str(plan), "--out", str(converted),
                                   "--report", str(root / "converted.json")]), 0)
            self.assertEqual(main(["verify", str(converted), "--out", str(checked)]), 0)
            result = json.loads(checked.read_text(encoding="utf-8"))
            self.assertTrue(result["structural_valid"])
            self.assertTrue(result["pending_refresh"])
            self.assertFalse(result["numeric_correspondence_verified"])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_discovery_plan_does_not_claim_actual_search(self):
        from litsearch.discovery import main
        with tempfile.TemporaryDirectory() as temporary, network_blocked():
            target = Path(temporary) / "plan"
            self.assertEqual(main(["--alias", "synthetic test phrase", "--out-dir", str(target)]), 0)
            report = json.loads((target / "discovery.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "offline_plan")
            self.assertEqual(report["candidate_status"], "unreviewed")

    def test_table_cli_retains_blank_and_footnote_without_approval(self):
        from litsearch.document_parse import main
        from scripts.run_engineering_tools_demo import SYNTHETIC_HTML
        with tempfile.TemporaryDirectory() as temporary, network_blocked():
            root = Path(temporary)
            source = root / "table.html"
            source.write_text(SYNTHETIC_HTML, encoding="utf-8")
            target = root / "parsed"
            self.assertEqual(main([str(source), "--out-dir", str(target)]), 0)
            report = json.loads((target / "document.json").read_text(encoding="utf-8"))
            self.assertEqual(report["ai_status"], "pending")
            self.assertTrue(any(c["raw_text"] == "" for c in report["tables"][0]["cells"]))
            self.assertTrue(any(n["association"] == "explicit_tfoot" for n in report["tables"][0]["notes"]))
