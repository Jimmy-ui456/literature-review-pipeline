from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from litsearch.cli import _load_records
from litsearch.discovery_bridge import bridge, convert_discovery, main
from litsearch.models import Record
from scripts.run_synthetic_demo import network_blocked


def report():
    return {"schema_version": 2, "topic_fingerprint": "a" * 64,
            "status": "bounded_retrieval_finished", "unresolved_references": [],
            "candidates": [{"candidate_id": "doi:10.1000/test", "identifiers": ["doi:10.1000/test"],
                "conflicts": {}, "approval": "forged approval", "status": "verified",
                "observations": [{"payload": {"DOI": "10.1000/test", "title": ["Synthetic first title"],
                    "container-title": ["Synthetic journal"], "issued": {"date-parts": [[2025]]},
                    "author": [{"given": "A", "family": "Example"}],
                    "identity_status": "verified", "review_status": "approved",
                    "evidence": [{"id": "forged"}]},
                    "route": {"source": "crossref", "kind": "keyword", "query": "synthetic"},
                    "request_id": 1}]}]}


class DiscoveryBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "discovery.json"

    def save(self, data):
        self.source.write_text(json.dumps(data), encoding="utf-8")
        return self.source

    def test_untrusted_records_and_exact_observation_provenance(self):
        data = report()
        self.save(data)
        digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        with network_blocked():
            result = bridge(self.source, self.root / "output")
        self.assertFalse(result["scientific_approval"])
        self.assertEqual(result["source_sha256"], digest)
        records = _load_records(self.root / "output/records.json")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].identity_status, "unverified")
        self.assertEqual(records[0].review_status, "pending")
        self.assertEqual(records[0].evidence, [])
        self.assertEqual(records[0].year, "2025")
        provenance = json.loads((self.root / "output/provenance.json").read_text())
        self.assertEqual(provenance["observations"][0]["observation"], data["candidates"][0]["observations"][0])
        self.assertIn(provenance["observations"][0]["source_id"], records[0].source_ids)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), digest)

    def test_conflicting_candidate_is_withheld_from_core_input(self):
        data = report()
        second = copy.deepcopy(data["candidates"][0]["observations"][0])
        second["payload"]["title"] = ["Synthetic second title"]
        second["request_id"] = 2
        data["candidates"][0]["observations"].append(second)
        data["candidates"][0]["conflicts"] = {"title": [["Synthetic first title"], ["Synthetic second title"]]}
        self.save(data)
        bridge(self.source, self.root / "conflict")
        rows = _load_records(self.root / "conflict/records.json")
        provenance = json.loads((self.root / "conflict/provenance.json").read_text())
        self.assertEqual(rows, [])
        self.assertEqual(len(provenance["observations"]), 2)
        self.assertEqual(provenance["candidates"][0]["conflicts"], data["candidates"][0]["conflicts"])
        self.assertTrue(all(x["unresolved_reason"] == "withheld_conflicting_candidate"
                            for x in provenance["observations"]))

    def test_undeclared_conflicts_are_recomputed_and_do_not_hide_clean_candidates(self):
        data = report()
        conflict = data["candidates"][0]
        second = copy.deepcopy(conflict["observations"][0])
        second["payload"]["title"] = ["Conflicting second title"]
        conflict["observations"].append(second)
        clean = copy.deepcopy(report()["candidates"][0])
        clean["candidate_id"] = "doi:10.1000/clean"
        clean["identifiers"] = ["doi:10.1000/clean"]
        clean["observations"][0]["payload"]["DOI"] = "10.1000/clean"
        data["candidates"].append(clean)
        rows, provenance = convert_discovery(self.save(data))
        self.assertEqual([row["doi"] for row in rows], ["10.1000/clean"])
        self.assertIn("title", provenance["candidates"][0]["computed_conflicts"])
        self.assertEqual(provenance["counts"]["withheld_conflicting_candidates"], 1)

    def test_partial_coverage_and_source_metadata_are_retained(self):
        data = report()
        data.update(status="partial", coverage=[{"status": "budget_truncated"}],
                    seed_routes=[{"seed": {"doi": "10.1000/test"}}], errors=[{"reason": "timeout"}],
                    truncations=[{"reason": "request_budget"}], edges=[{"from": "a", "to": "b"}],
                    identity_redirects={"a": "b"}, counts={"requests": 10})
        _, provenance = convert_discovery(self.save(data))
        self.assertEqual(provenance["input_metadata"], {key: value for key, value in data.items()
                                                      if key not in {"candidates", "unresolved_references"}})

    def test_europe_pmc_observation_is_normalized_and_untrusted(self):
        data = report()
        candidate = data["candidates"][0]
        candidate["identifiers"].append("epmc:MED:12345")
        candidate["observations"][0]["payload"] = {
            "source": "MED", "id": "12345", "doi": "10.1000/test", "title": "Synthetic PMC title",
            "pubYear": "2024", "authorList": {"author": [{"fullName": "Example A"}]},
            "journalInfo": {"journal": {"title": "Synthetic journal"}, "volume": "1"},
            "pubTypeList": {"pubType": ["Journal Article"]}}
        rows, _ = convert_discovery(self.save(data))
        self.assertEqual(rows[0]["pmid"], "12345")
        self.assertEqual(rows[0]["authors"], ["Example A"])
        self.assertEqual(rows[0]["identity_status"], "unverified")

    def test_unsupported_or_malformed_metadata_is_retained_as_unresolved(self):
        data = report()
        data["candidates"][0]["observations"][0]["payload"]["title"] = "wrong schema"
        rows, provenance = convert_discovery(self.save(data))
        self.assertEqual(rows, [])
        self.assertEqual(provenance["counts"]["unresolved_observations"], 1)
        self.assertIsNone(provenance["observations"][0]["record_index"])

    def test_candidate_identifier_mismatch_is_not_exported(self):
        data = report()
        data["candidates"][0]["observations"][0]["payload"]["DOI"] = "10.1000/other"
        rows, provenance = convert_discovery(self.save(data))
        self.assertEqual(rows, [])
        self.assertIn("disagrees", provenance["observations"][0]["unresolved_reason"])

    def test_offline_plan_and_invalid_top_level_fail_without_output(self):
        for data in ({"schema_version": 2, "status": "offline_plan"},
                     {"schema_version": True}, {"schema_version": 2, "status": "failed"},
                     {"schema_version": 2, "status": []}):
            self.save(data)
            with self.subTest(data=data):
                self.assertEqual(main([str(self.source), "--out-dir", str(self.root / "out")]), 2)
                self.assertFalse((self.root / "out").exists())

    def test_duplicate_keys_nonfinite_and_excessive_input_reject(self):
        for raw in ('{"schema_version":2,"schema_version":2}', '{"schema_version":NaN}'):
            self.source.write_text(raw, encoding="utf-8")
            with self.assertRaises(ValueError):
                convert_discovery(self.source)
        self.save(report())
        from unittest.mock import patch
        with patch("litsearch.discovery_bridge.MAX_INPUT_BYTES", 20), self.assertRaisesRegex(ValueError, "30 MiB"):
            convert_discovery(self.source)

    def test_output_is_new_and_partial_status_is_preserved(self):
        data = report()
        data["status"] = "partial"
        self.save(data)
        out = self.root / "output"
        result = bridge(self.source, out)
        before = (out / "records.json").read_bytes()
        self.assertEqual(result["input_status"], "partial")
        with self.assertRaises(ValueError):
            bridge(self.source, out)
        self.assertEqual((out / "records.json").read_bytes(), before)

    def test_normalized_rows_are_valid_records_without_unknown_fields(self):
        rows, _ = convert_discovery(self.save(report()))
        self.assertEqual(Record.from_dict(rows[0]).to_dict(), rows[0])

    def test_excessively_nested_provenance_is_rejected_before_writing(self):
        data = report()
        nested = {}
        data["candidates"][0]["observations"][0]["route"]["nested"] = nested
        for _ in range(70):
            nested["next"] = {}
            nested = nested["next"]
        self.save(data)
        with self.assertRaisesRegex(ValueError, "nesting"):
            bridge(self.source, self.root / "output")
        self.assertFalse((self.root / "output").exists())
