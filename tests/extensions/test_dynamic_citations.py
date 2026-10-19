from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from litsearch_zotero.dynamic_citations import (
    LocalAPI, convert, document_root, field_runs, item_binding, numeric_refs,
    paragraph_text, prepare, rendered_numbers, scan, sha256, verify, W, xml_bytes, xml_module,
)


def item(doi="10.1000/test", key="ABCD1234"):
    return {"key": key, "version": 1, "data": {"DOI": doi, "itemType": "journalArticle"},
            "links": {"alternate": {"href": "https://www.zotero.org/users/100/items/" + key}},
            "csljson": json.dumps([{"id": "a-citekey", "DOI": doi, "type": "article-journal",
                                    "title": "Synthetic reference", "author": [{"family": "Example"}],
                                    "issued": {"date-parts": [[2000]]}}])}


def docx(path, fragments=None, *, table=False):
    fragments = fragments or ["Before [Example, DOI: 10.1000/test] after."]
    et = xml_module()
    root = et.Element(f"{{{W}}}document", nsmap={"w": W})
    body = et.SubElement(root, f"{{{W}}}body")
    parent = body
    if table:
        parent = et.SubElement(et.SubElement(et.SubElement(body, f"{{{W}}}tbl"), f"{{{W}}}tr"), f"{{{W}}}tc")
    p = et.SubElement(parent, f"{{{W}}}p")
    for fragment in fragments:
        r = et.SubElement(p, f"{{{W}}}r")
        et.SubElement(et.SubElement(r, f"{{{W}}}rPr"), f"{{{W}}}i")
        et.SubElement(r, f"{{{W}}}t").text = fragment
    et.SubElement(body, f"{{{W}}}sectPr")
    with ZipFile(path, "w") as z:
        z.writestr("word/document.xml", xml_bytes(root))
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>')
        z.writestr("word/media/untouched.bin", b"unchanged")


@unittest.skipUnless(importlib.util.find_spec("lxml"), "optional citations dependencies not installed")
class DynamicCitationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.docx"
        self.output = self.root / "output.docx"
        docx(self.source)

    def test_cross_run_citations_preserve_other_text_styles_and_archive_members(self):
        docx(self.source, ["Before [Ex", "ample, DOI: 10.1000/", "test] after [unrelated]."])
        root = document_root(self.source)
        p = root.find(f".//{{{W}}}p")
        p.insert(1, xml_module().Element(f"{{{W}}}proofErr"))
        with ZipFile(self.source) as z:
            members = {n: z.read(n) for n in z.namelist()}
        members["word/document.xml"] = xml_bytes(root)
        with ZipFile(self.source, "w") as z:
            for name, raw in members.items():
                z.writestr(name, raw)
        before = sha256(self.source)
        plan = prepare(self.source, [item()])
        result = convert(plan, self.output)
        self.assertEqual(before, sha256(self.source))
        self.assertEqual(result["citation_groups"], 1)
        self.assertEqual(result["unique_references"], 1)
        self.assertTrue(result["structural_valid"])
        root = document_root(self.output)
        p = root.find(f".//{{{W}}}p")
        self.assertEqual(paragraph_text(p), "Before [1] after [unrelated].")
        self.assertEqual(len(p.findall(f"{{{W}}}r")), len(p.findall(f"{{{W}}}r/{{{W}}}rPr/{{{W}}}i")))
        with ZipFile(self.output) as z:
            self.assertEqual(z.read("word/media/untouched.bin"), b"unchanged")
            self.assertIn(b"ZOTERO_PREF_1", z.read("docProps/custom.xml"))
            self.assertIn(b"custom-properties", z.read("_rels/.rels"))

    def test_grouping_deduplication_repeated_references_and_table(self):
        docx(self.source, ["[DOI: 10.1000/test; DOI: 10.1000/other] middle [DOI: 10.1000/test]"], table=True)
        plan = prepare(self.source, [item(), item("10.1000/other", "EFGH5678")])
        convert(plan, self.output)
        result = verify(self.output)
        self.assertEqual(result["citation_groups"], 2)
        self.assertEqual(result["unique_references"], 2)
        self.assertEqual(result["number_to_doi"], {"1": "10.1000/test"})
        self.assertTrue(result["pending_refresh"])
        self.assertFalse(result["numeric_correspondence_verified"])

    def test_missing_and_ambiguous_bindings_block(self):
        for rows in ([], [item(), item(key="EFGH5678")]):
            plan = prepare(self.source, rows)
            self.assertFalse(plan["ready"])
            with self.assertRaises(ValueError):
                convert(plan, self.output)
            self.assertFalse(self.output.exists())
        plan = prepare(self.source, [item(), item(key="EFGH5678")], {"10.1000/test": "EFGH5678"})
        self.assertTrue(plan["ready"])
        self.assertEqual(plan["bindings"]["10.1000/test"]["key"], "EFGH5678")

    def test_no_overwrite_and_stale_plan(self):
        plan = prepare(self.source, [item()])
        with self.assertRaises(ValueError):
            convert(plan, self.source)
        docx(self.source, ["changed [DOI: 10.1000/test]"])
        with self.assertRaises(ValueError):
            convert(plan, self.output)
        self.assertFalse(self.output.exists())

    def test_existing_dynamic_fields_are_not_reconverted(self):
        convert(prepare(self.source, [item()]), self.output)
        with self.assertRaises(ValueError):
            prepare(self.output, [item()])

    def test_numeric_input_requires_explicit_mapping(self):
        docx(self.source, ["Before [1–2] and [2]."])
        self.assertFalse(scan(self.source)["groups"])
        with self.assertRaises(ValueError):
            scan(self.source, {"1": "10.1000/test"})
        plan = prepare(self.source, [item(), item("10.1000/other", "EFGH5678")],
                       references={"1": "10.1000/test", "2": "10.1000/other"})
        self.assertTrue(plan["ready"])
        self.assertEqual(numeric_refs("[2,4–6]"), ["2", "4", "5", "6"])
        self.assertEqual(rendered_numbers("[1], [2], [4]–[6]"), ["1", "2", "4", "5", "6"])
        with self.assertRaises(ValueError):
            numeric_refs("[6–2]")

    def test_identity_and_remote_api_guard(self):
        broken = item()
        broken["csljson"] = json.dumps([{"DOI": "10.1000/wrong"}])
        with self.assertRaises(ValueError):
            item_binding(broken)
        for url in ("https://localhost:23119", "http://example.org:23119", "http://localhost:23119/other",
                    "http://localhost:80", "http://user@localhost:23119"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                LocalAPI(url)

    def test_unbalanced_fields_fail_verification(self):
        et = xml_module()
        with ZipFile(self.source) as z:
            members = {n: z.read(n) for n in z.namelist()}
        root = document_root(self.source)
        p = root.find(f".//{{{W}}}p")
        for run in field_runs("ADDIN ZOTERO_BIBL {} CSL_BIBLIOGRAPHY", "ref")[:-1]:
            p.append(run)
        members["word/document.xml"] = xml_bytes(root)
        with ZipFile(self.output, "w") as z:
            for name, raw in members.items():
                z.writestr(name, raw)
        self.assertIn("unclosed_fields", verify(self.output)["errors"])


if __name__ == "__main__":
    unittest.main()
