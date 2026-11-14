"""A valid citation subset must not hide extra unsupported Zotero fields."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from litsearch_zotero.dynamic_citations import W, _verify_root, field_runs, item_binding, verify, xml_bytes, xml_module
from tests.extensions.test_dynamic_citations import item


@unittest.skipUnless(importlib.util.find_spec("lxml"), "citations extra not installed")
class CitationSkillBoundaryTests(unittest.TestCase):
    def document(self):
        et = xml_module()
        root = et.Element(f"{{{W}}}document", nsmap={"w": W})
        body = et.SubElement(root, f"{{{W}}}body")
        binding = item_binding(item())
        payload = {"citationID": "synthetic-valid", "citationItems": [
            {"uris": [binding["uri"]], "itemData": binding["itemData"]}]}
        paragraph = et.SubElement(body, f"{{{W}}}p")
        paragraph.extend(field_runs("ADDIN ZOTERO_ITEM CSL_CITATION " + json.dumps(payload), "[1]"))
        paragraph = et.SubElement(body, f"{{{W}}}p")
        paragraph.extend(field_runs("ADDIN ZOTERO_BIBL {} CSL_BIBLIOGRAPHY",
                                    "[1] Synthetic source. doi:10.1000/test."))
        return root, body

    def test_extra_truncated_or_unknown_zotero_field_prevents_complete_assertion(self):
        for code in ("ADDIN ZOTERO_ITEM CSL_CITATION", "ADDIN ZOTERO_ITEM CSL_CITATIONX {}",
                     "ADDIN ZOTERO_BIBL", "ADDIN ZOTERO_UNKNOWN {}"):
            with self.subTest(code=code):
                root, body = self.document()
                paragraph = xml_module().SubElement(body, f"{{{W}}}p")
                paragraph.extend(field_runs(code, "[99]"))
                result = _verify_root(root)
                self.assertFalse(result["structural_valid"])
                self.assertFalse(result["numeric_correspondence_verified"])
                self.assertIn("unsupported_or_malformed_zotero_field", result["errors"])

    def test_split_truncated_code_is_not_hidden_by_valid_fields(self):
        root, body = self.document()
        et = xml_module()
        paragraph = et.SubElement(body, f"{{{W}}}p")
        runs = field_runs("ADDIN ZOTERO_", "[99]")
        additional = et.Element(f"{{{W}}}r")
        et.SubElement(additional, f"{{{W}}}instrText").text = "ITEM CSL_CITATION"
        paragraph.extend([*runs[:2], additional, *runs[2:]])
        self.assertFalse(_verify_root(root)["numeric_correspondence_verified"])

    def test_ordinary_word_fields_do_not_invalidate_valid_zotero_citations(self):
        root, body = self.document()
        paragraph = xml_module().SubElement(body, f"{{{W}}}p")
        paragraph.extend(field_runs("PAGE", "1"))
        result = _verify_root(root)
        self.assertTrue(result["structural_valid"], result["errors"])
        self.assertTrue(result["numeric_correspondence_verified"])

    def test_static_numeric_candidate_does_not_invalidate_known_field_mapping(self):
        root, body = self.document()
        et = xml_module()
        paragraph = et.SubElement(body, f"{{{W}}}p")
        for text in ("Instrument setting [", "99", "] requires classification."):
            et.SubElement(et.SubElement(paragraph, f"{{{W}}}r"), f"{{{W}}}t").text = text
        result = _verify_root(root)
        self.assertTrue(result["structural_valid"], result["errors"])
        self.assertTrue(result["numeric_correspondence_verified"])
        self.assertFalse(result["whole_document_coverage_verified"])
        self.assertEqual([x["display"] for x in result["candidate_static_numbers"]], ["[99]"])
        self.assertIn("static_numeric_candidates_need_classification", result["coverage_warnings"])

    def test_text_on_either_side_of_a_field_does_not_form_a_false_candidate(self):
        root, body = self.document()
        et = xml_module()
        paragraph = et.SubElement(body, f"{{{W}}}p")
        et.SubElement(et.SubElement(paragraph, f"{{{W}}}r"), f"{{{W}}}t").text = "[9"
        paragraph.extend(field_runs("PAGE", "1"))
        et.SubElement(et.SubElement(paragraph, f"{{{W}}}r"), f"{{{W}}}t").text = "9]"
        self.assertEqual(_verify_root(root)["candidate_static_numbers"], [])

    def test_auxiliary_fields_are_disclosed_without_claiming_their_correspondence(self):
        root, _ = self.document()
        et = xml_module()
        footnotes = et.Element(f"{{{W}}}footnotes", nsmap={"w": W})
        footnote = et.SubElement(footnotes, f"{{{W}}}footnote")
        paragraph = et.SubElement(footnote, f"{{{W}}}p")
        paragraph.extend(field_runs("ADDIN ZOTERO_ITEM CSL_CITATION {}", "[2]"))
        paragraph.extend(field_runs("ADDIN EN.CITE synthetic", "[3]"))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.docx"
            with ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", xml_bytes(root))
                archive.writestr("word/footnotes.xml", xml_bytes(footnotes))
            result = verify(path)
        self.assertTrue(result["numeric_correspondence_verified"])
        self.assertTrue(result["auxiliary_parts_checked"])
        self.assertEqual(result["auxiliary_citation_parts"], [
            {"part": "word/footnotes.xml", "field_types": ["zotero", "endnote"]}])
        self.assertFalse(result["whole_document_coverage_verified"])
        self.assertIn("auxiliary_citation_fields_not_verified", result["coverage_warnings"])

    def test_non_numeric_style_is_structurally_valid_but_has_no_numeric_assertion(self):
        root, _ = self.document()
        for el in root.iter(f"{{{W}}}t"):
            if el.text == "[1]":
                el.text = "(Example, 2000)"
            elif el.text and el.text.startswith("[1] Synthetic"):
                el.text = "Example (2000). Synthetic source. doi:10.1000/test."
        result = _verify_root(root)
        self.assertTrue(result["structural_valid"], result["errors"])
        self.assertFalse(result["pending_refresh"])
        self.assertFalse(result["numeric_correspondence_verified"])
        self.assertEqual(result["verification_scope"], "main_document_zotero_fields")
