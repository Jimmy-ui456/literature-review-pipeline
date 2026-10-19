from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from litsearch_zotero.word_plugin import run_plugin


class WordPluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "review.docx"
        self.path.write_bytes(b"original document")
        self.doc = Mock(ReadOnly=False, Saved=True, FullName=str(self.path.resolve()))
        self.doc.Bookmarks.Exists.return_value = True
        self.word = Mock(Documents=[self.doc], Templates=[SimpleNamespace(Name="Zotero.dotm")])
        self.api = patch("litsearch_zotero.word_plugin.LocalAPI")
        self.api_client = self.api.start()
        self.addCleanup(self.api.stop)

    def test_dispatch_only_uses_installed_macro_and_preserves_disk_backup(self):
        with patch("litsearch_zotero.word_plugin.connect_word", return_value=self.word):
            result = run_plugin(self.path, "add-citation", bookmark="cite_here")
        self.assertEqual(result["status"], "dispatched")
        self.word.Run.assert_called_once_with("Zotero.ZoteroAddEditCitation")
        self.doc.Bookmarks.Item.assert_called_once_with("cite_here")
        self.assertEqual(Path(result["backup"]).read_bytes(), b"original document")
        self.doc.Save.assert_not_called()

    def test_unsaved_document_stops_without_macro_or_backup(self):
        self.doc.Saved = False
        with patch("litsearch_zotero.word_plugin.connect_word", return_value=self.word), self.assertRaises(ValueError):
            run_plugin(self.path, "refresh")
        self.word.Run.assert_not_called()
        self.assertEqual(list(self.path.parent.glob("*.docx")), [self.path])

    def test_stopped_zotero_does_not_dispatch_a_macro_or_create_backup(self):
        self.api_client.return_value.get.side_effect = OSError("synthetic stopped server")
        with patch("litsearch_zotero.word_plugin.connect_word", return_value=self.word), self.assertRaises(ValueError):
            run_plugin(self.path, "refresh")
        self.word.Run.assert_not_called()
        self.assertEqual(list(self.path.parent.glob("*.docx")), [self.path])

    def test_missing_template_never_installs_or_invokes_arbitrary_macro(self):
        self.word.Templates = []
        with patch("litsearch_zotero.word_plugin.connect_word", return_value=self.word), self.assertRaises(ValueError):
            run_plugin(self.path, "refresh")
        self.word.Run.assert_not_called()
        self.doc.Save.assert_not_called()

    def test_placeholder_bibliography_is_not_reported_as_finished(self):
        self.doc.Fields = [SimpleNamespace(Code=SimpleNamespace(Text="ADDIN ZOTERO_BIBL {}"),
                                           Result=SimpleNamespace(Text="Please use Zotero Refresh"))]
        with patch("litsearch_zotero.word_plugin.connect_word", return_value=self.word), self.assertRaises(ValueError):
            run_plugin(self.path, "finish")
        self.doc.Save.assert_not_called()

    def test_invalid_live_fields_are_not_persisted(self):
        self.doc.Fields = [SimpleNamespace(Code=SimpleNamespace(Text="ADDIN ZOTERO_BIBL {}"),
                                           Result=SimpleNamespace(Text="[1] Reference"))]
        with patch("litsearch_zotero.word_plugin.connect_word", return_value=self.word), \
             patch("litsearch_zotero.word_plugin.live_document_report", return_value={
                 "structural_valid": False, "pending_refresh": False}), self.assertRaises(ValueError):
            run_plugin(self.path, "finish")
        self.doc.Save.assert_not_called()
        self.assertEqual(list(self.path.parent.glob("*.docx")), [self.path])

    def test_valid_finish_keeps_a_backup_and_does_not_claim_macro_completion(self):
        self.doc.Fields = [SimpleNamespace(Code=SimpleNamespace(Text="ADDIN ZOTERO_BIBL {}"),
                                           Result=SimpleNamespace(Text="[1] Reference"))]
        report = {"structural_valid": True, "pending_refresh": False}
        with patch("litsearch_zotero.word_plugin.connect_word", return_value=self.word), \
             patch("litsearch_zotero.word_plugin.live_document_report", return_value=report), \
             patch("litsearch_zotero.word_plugin.verify", return_value=report):
            result = run_plugin(self.path, "finish")
        self.doc.Save.assert_called_once()
        self.assertEqual(result["status"], "saved_for_verification")
        self.assertEqual(Path(result["backup"]).read_bytes(), b"original document")
        self.word.Run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
