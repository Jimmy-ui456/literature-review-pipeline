"""Hostile inputs and cross-layer citation consistency regression cases."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from litsearch_zotero.dynamic_citations import (
    NS, W, convert, document_root, field_runs, numeric_refs, prepare, scan,
    main, read_json, verify, write_json_new, write_new, xml_bytes, xml_module,
)
from tests.extensions.test_dynamic_citations import docx, item


@unittest.skipUnless(importlib.util.find_spec("lxml"), "optional citations dependencies not installed")
class CitationAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.source = self.directory / "source.docx"
        self.output = self.directory / "output.docx"
        docx(self.source)

    def rewrite(self, path, edit):
        root = document_root(path)
        edit(root)
        with ZipFile(path) as z:
            members = {n: z.read(n) for n in z.namelist()}
        members["word/document.xml"] = xml_bytes(root)
        with ZipFile(path, "w") as z:
            for name, raw in members.items():
                z.writestr(name, raw)

    def converted(self):
        convert(prepare(self.source, [item()]), self.output)

    def set_bibliography(self, text):
        def edit(root):
            p = root.xpath('/w:document/w:body/w:p', namespaces=NS)[-1]
            p.find(f"{{{W}}}r/{{{W}}}t").text = text
        self.rewrite(self.output, edit)

    def test_mixed_group_must_not_silently_delete_uncited_source(self):
        for group in ('[Alpha, DOI: 10.1000/test; Beta et al., 2020]',
                      '[Alpha, DOI: 10.1000/test, Beta et al., 2020]'):
            docx(self.source, ["Text " + group + " end."])
            with self.subTest(group=group), self.assertRaises(ValueError):
                prepare(self.source, [item()])

    def test_invalid_numeric_range_does_not_expand_to_unintended_references(self):
        for text in ("[1-2-8]", "[0]", "[1,,2]", "[2,1,2]"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                numeric_refs(text)

    def test_prepare_rejects_partially_split_existing_field_codes(self):
        def edit(root):
            p = root.find(f".//{{{W}}}p")
            nodes = field_runs('ADDIN ZOTERO_ITEM CSL_CITATION {}', '[1]')
            code = nodes[1].find(f"{{{W}}}instrText")
            code.text = ' ADDIN ZO'
            extra = deepcopy(nodes[1])
            extra.find(f"{{{W}}}instrText").text = 'TERO_ITEM CSL_CITATION {} '
            for node in [*nodes[:2], extra, *nodes[2:]]:
                p.append(node)
        self.rewrite(self.source, edit)
        with self.assertRaises(ValueError):
            prepare(self.source, [item()])

    def test_output_suffix_is_validated_before_any_write(self):
        plan = prepare(self.source, [item()])
        wrong = self.directory / "wrong.txt"
        with self.assertRaises(ValueError):
            convert(plan, wrong)
        self.assertFalse(wrong.exists())

    def test_existing_json_is_never_replaced_and_serialization_failure_leaves_no_file(self):
        target = self.directory / 'report.json'
        write_json_new(target, {'before': True})
        with self.assertRaises(FileExistsError):
            write_json_new(target, {'after': True})
        self.assertEqual(read_json(target), {'before': True})
        invalid = self.directory / 'invalid.json'
        with self.assertRaises(ValueError):
            write_json_new(invalid, {'nonfinite': float('nan')})
        self.assertFalse(invalid.exists())
        self.assertFalse(list(self.directory.glob('*.tmp')))

    def test_failed_publish_leaves_no_partial_document(self):
        plan = prepare(self.source, [item()])
        operation = 'os.rename' if __import__('os').name == 'nt' else 'os.link'
        with patch('litsearch_zotero.dynamic_citations.' + operation, side_effect=OSError('synthetic disk error')):
            with self.assertRaises(OSError):
                convert(plan, self.output)
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.directory.glob('*.tmp')))

    def test_citation_json_cannot_repeat_keys_or_use_nonfinite_numbers(self):
        for text in ('{"a":1,"a":2}', '{"a":NaN}'):
            path = self.directory / 'bad.json'
            path.write_text(text, encoding='utf-8')
            with self.subTest(text=text), self.assertRaises(ValueError):
                read_json(path)

    def test_corrupt_docx_cli_returns_error_without_traceback(self):
        self.source.write_bytes(b'not a ZIP')
        with patch('sys.stderr') as errors:
            self.assertEqual(main(['scan', str(self.source), '--out', str(self.directory / 'scan.json')]), 2)
        self.assertFalse((self.directory / 'scan.json').exists())

    def test_package_subcommand_dispatches_to_the_real_citation_tool(self):
        from litsearch_zotero.__main__ import main as package_main
        report = self.directory / 'scan.json'
        with patch('sys.stdout'):
            self.assertEqual(package_main(['dynamic-citations', 'scan', str(self.source), '--out', str(report)]), 0)
        self.assertEqual(len(read_json(report)['groups']), 1)

    def test_doctor_uses_a_json_endpoint_instead_of_the_html_api_index(self):
        with patch('litsearch_zotero.dynamic_citations.LocalAPI') as client, patch('sys.stdout'):
            client.return_value.get.return_value = [{'itemType': 'journalArticle'}]
            self.assertEqual(main(['doctor']), 0)
        client.return_value.get.assert_called_once_with('/api/itemTypes')

    def test_main_and_csl_field_uri_cannot_claim_two_different_dois(self):
        docx(self.source, ['[DOI: 10.1000/test; DOI: 10.1000/other]'])
        convert(prepare(self.source, [item(), item('10.1000/other', 'EFGH5678')]), self.output)
        def edit(root):
            for el in root.findall(f'.//{{{W}}}instrText'):
                if 'CSL_CITATION ' in (el.text or ''):
                    prefix, data = el.text.split('CSL_CITATION ', 1)
                    payload = json.loads(data)
                    payload['citationItems'][1]['uris'] = payload['citationItems'][0]['uris']
                    el.text = prefix + 'CSL_CITATION ' + json.dumps(payload)
        self.rewrite(self.output, edit)
        self.assertIn('same_uri_multiple_dois', verify(self.output)['errors'])

    def test_mixed_static_numeric_citations_require_mapping(self):
        docx(self.source, ['[DOI: 10.1000/test] then [2]'])
        with self.assertRaises(ValueError):
            prepare(self.source, [item()])

    def test_conversion_cannot_remove_a_later_section_after_bibliography(self):
        def edit(root):
            et = xml_module()
            body = root.find(f'{{{W}}}body')
            for text in ('References', '[1] Synthetic reference.', 'Appendix A'):
                p = et.Element(f'{{{W}}}p')
                et.SubElement(et.SubElement(p, f'{{{W}}}r'), f'{{{W}}}t').text = text
                body.insert(len(body) - 1, p)
        self.rewrite(self.source, edit)
        plan = prepare(self.source, [item()])
        with self.assertRaises(ValueError):
            convert(plan, self.output, replace_bibliography=True)
        self.assertFalse(self.output.exists())

    def test_forged_binding_identity_and_missing_group_are_rejected(self):
        for mutation in ("uri", "omitted"):
            plan = prepare(self.source, [item()])
            if mutation == "uri":
                plan["bindings"]["10.1000/test"]["uri"] = 'http://zotero.org/users/100/items/EFGH5678'
            else:
                plan["groups"] = []
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                convert(plan, self.output)
            self.assertFalse(self.output.exists())

    def test_bibliography_wrong_doi_is_not_accepted_as_numeric_correspondence(self):
        self.converted()
        self.set_bibliography('[1] Wrong source. doi:10.1000/wrong.')
        self.assertFalse(verify(self.output)["structural_valid"])

    def test_same_reference_cannot_have_different_numbers(self):
        docx(self.source, ['[DOI: 10.1000/test] then [DOI: 10.1000/test]'])
        self.converted()
        def edit(root):
            results = root.xpath('//w:p[1]//w:t', namespaces=NS)
            citation_results = [x for x in results if x.text == '[1]']
            citation_results[-1].text = '[2]'
        self.rewrite(self.output, edit)
        self.set_bibliography('[1] Source doi:10.1000/test. [2] Source doi:10.1000/test.')
        self.assertFalse(verify(self.output)["structural_valid"])

    def test_number_item_count_mismatch_fails(self):
        self.converted()
        def edit(root):
            p = root.find(f'.//{{{W}}}p')
            for el in p.findall(f'{{{W}}}r/{{{W}}}t'):
                if el.text == '[1]':
                    el.text = '[1], [2]'
        self.rewrite(self.output, edit)
        self.assertFalse(verify(self.output)["structural_valid"])

    def test_malformed_field_json_returns_failed_report_instead_of_crashing(self):
        self.converted()
        def edit(root):
            for el in root.findall(f'.//{{{W}}}instrText'):
                if 'CSL_CITATION' in (el.text or ''):
                    el.text = ' ADDIN ZOTERO_ITEM CSL_CITATION {invalid '
        self.rewrite(self.output, edit)
        self.assertFalse(verify(self.output)["structural_valid"])

    def test_empty_bibliography_has_explicit_pending_state(self):
        self.converted()
        result = verify(self.output)
        self.assertTrue(result["structural_valid"])
        self.assertTrue(result["pending_refresh"])
        self.assertFalse(result["numeric_correspondence_verified"])

    def test_sorted_numeric_cluster_does_not_pair_items_by_input_position(self):
        docx(self.source, ['[DOI: 10.1000/other; DOI: 10.1000/test] then [DOI: 10.1000/test]'])
        convert(prepare(self.source, [item(), item('10.1000/other', 'EFGH5678')]), self.output)
        # Simulate a CSL processor sorting a previously entered cluster.
        def edit(root):
            p = root.find(f'.//{{{W}}}p')
            for el in p.findall(f'{{{W}}}r/{{{W}}}t'):
                if el.text == '[1,2]': el.text = '[1], [2]'
                elif el.text == '[2]': el.text = '[1]'
        self.rewrite(self.output, edit)
        self.set_bibliography('[1] Test. doi:10.1000/test. [2] Other. doi:10.1000/other.')
        result = verify(self.output)
        self.assertTrue(result['structural_valid'], result['errors'])
        self.assertTrue(result['numeric_correspondence_verified'])


if __name__ == '__main__':
    unittest.main()
