"""Archive gates must verify shipped tools, commands and optional dependencies."""
from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.release_check import inspect_archive, load_config


ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    "litsearch/__init__.py", "litsearch/cli.py", "litsearch/core.py",
    "litsearch/service_probe.py", "litsearch/discovery.py", "litsearch/document_parse.py",
    "litsearch_zotero/__init__.py", "litsearch_zotero/attach_pdfs.py",
    "litsearch_zotero/dynamic_citations.py", "litsearch_zotero/word_plugin.py",
)
ENTRIES = """[console_scripts]
literature-service-probe = litsearch.service_probe:main
literature-discover = litsearch.discovery:main
literature-parse = litsearch.document_parse:main
literature-cite = litsearch_zotero.dynamic_citations:main
literature-cite-word = litsearch_zotero.word_plugin:main
"""
METADATA = """Metadata-Version: 2.4
Name: d-pan-literature-pipeline
Version: 3.0.0
Requires-Dist: openpyxl<4,>=3.1
Requires-Dist: pypdf<7,>=5
Requires-Dist: lxml<7,>=5; extra == "citations"
Requires-Dist: pywin32<400,>=306; sys_platform == "win32" and extra == "citations"
Requires-Dist: pdfplumber<0.12,>=0.11; extra == "tables"
Requires-Dist: pdfplumber<0.12,>=0.11; extra == "ocr"
Requires-Dist: pypdfium2<6,>=4.30; extra == "ocr"
Requires-Dist: Pillow<13,>=10; extra == "ocr"
Requires-Dist: pdfplumber<0.12,>=0.11; extra == "parsing"
Requires-Dist: pypdfium2<6,>=4.30; extra == "parsing"
Requires-Dist: Pillow<13,>=10; extra == "parsing"
"""


class ToolReleaseContractTests(unittest.TestCase):
    def inspect(self, *, omitted: str = "", metadata: str = METADATA,
                entries: str | None = ENTRIES, kind: str = "wheel") -> dict:
        files = {name: "" for name in MODULES if name != omitted}
        for profile in ("pantothenate", "lysine", "glutathione"):
            files[f"litsearch/profiles/{profile}.json"] = "{}"
        if kind == "wheel":
            base = "d_pan_literature_pipeline-3.0.0.dist-info/"
            files[base + "METADATA"] = metadata
            if entries is not None:
                files[base + "entry_points.txt"] = entries
        else:
            files["PKG-INFO"] = metadata
            if entries is not None:
                files["d_pan_literature_pipeline.egg-info/entry_points.txt"] = entries
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / ("tools.whl" if kind == "wheel" else "tools.tar.gz")
            if kind == "wheel":
                with zipfile.ZipFile(archive, "w") as handle:
                    for name, content in files.items():
                        handle.writestr(name, content)
            else:
                with tarfile.open(archive, "w:gz") as handle:
                    for name, content in files.items():
                        data = content.encode("utf-8")
                        member = tarfile.TarInfo("d_pan_literature_pipeline-3.0.0/" + name)
                        member.size = len(data)
                        handle.addfile(member, io.BytesIO(data))
            return inspect_archive(archive, load_config(ROOT))

    def test_wheel_and_sdist_have_complete_tool_contract(self):
        for kind in ("wheel", "sdist"):
            with self.subTest(kind=kind):
                self.assertEqual(self.inspect(kind=kind)["result"], "pass")

    def test_each_missing_tool_module_is_rejected(self):
        for module in ("service_probe", "discovery", "document_parse"):
            with self.subTest(module=module):
                report = self.inspect(omitted=f"litsearch/{module}.py")
                self.assertIn("required_member_missing", {x["rule"] for x in report["findings"]})

    def test_missing_or_wrong_console_command_is_rejected(self):
        for entries in (None, ENTRIES.replace("litsearch.discovery:main", "litsearch.cli:main")):
            with self.subTest(entries=entries):
                self.assertIn("tool_entrypoint_missing", {x["rule"] for x in self.inspect(entries=entries)["findings"]})

    def test_missing_extra_dependency_is_rejected(self):
        metadata = METADATA.replace('Requires-Dist: Pillow<13,>=10; extra == "ocr"\n', "")
        report = self.inspect(metadata=metadata)
        self.assertIn("optional_dependency_missing", {x["rule"] for x in report["findings"]})

    def test_citation_modules_commands_and_dependencies_must_ship_together(self):
        for module in ("dynamic_citations", "word_plugin"):
            with self.subTest(module=module):
                report = self.inspect(omitted=f"litsearch_zotero/{module}.py")
                self.assertIn("required_member_missing", {x["rule"] for x in report["findings"]})
        report = self.inspect(metadata=METADATA.replace('Requires-Dist: lxml<7,>=5; extra == "citations"\n', ""))
        self.assertIn("optional_dependency_missing", {x["rule"] for x in report["findings"]})
        report = self.inspect(entries=ENTRIES.replace('literature-cite-word = litsearch_zotero.word_plugin:main\n', ""))
        self.assertIn("tool_entrypoint_missing", {x["rule"] for x in report["findings"]})
        report = self.inspect(metadata=METADATA.replace('sys_platform == "win32" and extra == "citations"',
                                                        'extra == "citations"'))
        self.assertIn("optional_dependency_missing", {x["rule"] for x in report["findings"]})

    def test_lookalike_constraint_does_not_pass(self):
        metadata = METADATA.replace("openpyxl<4,>=3.1", "openpyxl<40,>=3.1")
        self.assertIn("required_dependency_constraint_missing", {x["rule"] for x in self.inspect(metadata=metadata)["findings"]})

    def test_impossible_extra_marker_does_not_pass(self):
        metadata = METADATA.replace('extra == "ocr"', 'extra == "ocr" and python_version < "0"')
        self.assertIn("optional_dependency_missing", {x["rule"] for x in self.inspect(metadata=metadata)["findings"]})


if __name__ == "__main__":
    unittest.main()
