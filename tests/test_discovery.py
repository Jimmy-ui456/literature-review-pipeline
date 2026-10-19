"""Synthetic, offline adversarial coverage for recovery and citation graphs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from litsearch.discovery import Budgets, build_plan, execute, main, validate_aliases, validate_plan, validate_seeds
from litsearch.net import Response
from litsearch.topics import load_topic


def article(identifier: str, title: str | None = None, **kwargs: object) -> dict:
    return {"source": "MED", "id": identifier, "title": title or "Synthetic article " + identifier, **kwargs}


def page(kind: str = "search", rows: list | None = None, count: int | None = None, cursor: str = "next") -> dict:
    rows = [] if rows is None else rows
    container, field = {"search": ("resultList", "result"), "references": ("referenceList", "reference"),
                        "citations": ("citationList", "citation")}[kind]
    return {"hitCount": len(rows) if count is None else count, container: {field: rows}, "nextCursorMark": cursor}


def crossref_page(rows=None, count=None, cursor="next", query=None):
    rows = [] if rows is None else rows
    message = {"total-results": len(rows) if count is None else count, "items": rows, "next-cursor": cursor}
    if query is not None:
        message["query"] = {"search-terms": query}
    return {"status": "ok", "message": message}


def crossref_article(identifier, title="Synthetic indexed work"):
    return {"DOI": "10.1234/" + identifier, "title": [title]}


class FakeClient:
    def __init__(self, handler=None):
        self.handler = handler or (lambda _: page())
        self.urls: list[str] = []

    def request(self, url: str, **kwargs: object) -> Response:
        self.urls.append(url)
        value = self.handler(url)
        if isinstance(value, Exception):
            raise value
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        return Response(body, "application/json", url, 200)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def plan(self, seeds=None, aliases=None, sources=None, **budgets):
        return build_plan(load_topic("pantothenate"), aliases or [], seeds or [], Budgets(page_size=2, **budgets),
                          sources=sources)

    def run_discovery(self, handler, plan=None):
        fake = FakeClient(handler)
        out = self.root / "result"
        result = execute(plan or self.plan(), out, online=True, client=fake)
        return result, fake, out

    def test_default_plan_is_offline_and_preserves_protocol(self):
        fake = FakeClient(lambda _: self.fail("offline contacted network"))
        plan = self.plan(aliases=["synthetic additional alias"])
        result = execute(plan, self.root / "plan", client=fake)
        self.assertEqual(result["status"], "offline_plan")
        self.assertEqual(result["schema_version"], 2)
        self.assertFalse(fake.urls)
        saved = json.loads((self.root / "plan" / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, plan)
        self.assertEqual(saved["topic_fingerprint"], load_topic().fingerprint)
        self.assertIn('TITLE_ABS:"synthetic additional alias"', saved["queries"][1])

    def test_alias_and_seed_reject_query_or_url_injection(self):
        for alias in ['x" OR *', "x:evil", "x\nquery", "(foo)", "OR", "", 123]:
            with self.subTest(alias=alias), self.assertRaises(ValueError):
                validate_aliases([alias])
        for seed in [{"source": "MED", "id": "../9"}, {"source": "evil", "id": "1"},
                     {"source": "MED", "id": "1", "url": "http://localhost"}, {"doi": "invented"}]:
            with self.subTest(seed=seed), self.assertRaises(ValueError):
                validate_seeds([seed])
        self.assertEqual(validate_seeds([{"source": "pmc", "id": "pmc12"}]), [{"source": "PMC", "id": "PMC12"}])

    def test_search_cursor_pagination(self):
        def handler(url):
            cursor = parse_qs(urlsplit(url).query)["cursorMark"][0]
            return page(rows=[article("1"), article("2")], count=3) if cursor == "*" else page(rows=[article("3")], count=3)
        report, fake, out = self.run_discovery(handler)
        self.assertEqual(report["counts"]["candidates"], 3)
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(len(fake.urls), 2)
        self.assertEqual(report["status"], "bounded_retrieval_finished")
        self.assertEqual(report["counts"]["requests"], 2)
        self.assertEqual(report["counts"]["page_attempts"], 2)
        self.assertEqual(report["counts"]["pages"], 2)
        self.assertEqual(report["coverage"][0]["status"], "api_reported_end")
        for row in json.loads((out / "responses.json").read_text()):
            self.assertEqual(hashlib.sha256((out / row["file"]).read_bytes()).hexdigest(), row["sha256"])

    def test_literal_aliases_reject_wildcard_operators(self):
        for alias in ("*", "foo*", "foo?", "foo~", "foo^2"):
            with self.subTest(alias=alias), self.assertRaises(ValueError):
                validate_aliases([alias])

    def test_doi_seed_rejects_control_characters(self):
        for control in ("\x00", "\x01", "\x1f", "\x7f"):
            with self.subTest(control=repr(control)), self.assertRaises(ValueError):
                validate_seeds([{"doi": "10.1234/foo" + control + "bar"}])

    def test_exact_replay_is_network_free_and_deterministic(self):
        plan = self.plan(seeds=[{"source": "MED", "id": "1"}])
        def handler(url):
            kind = urlsplit(url).path.rsplit("/", 1)[1]
            return page(kind=kind, rows=[article("2")]) if kind in {"references", "citations"} else page()
        report, _, out = self.run_discovery(handler, plan)
        fake = FakeClient(lambda _: self.fail("replay contacted network"))
        replay = execute(plan, self.root / "replay", replay_dir=out, client=fake)
        self.assertEqual(report, replay)
        self.assertFalse(fake.urls)

    def test_plan_tampering_is_rejected(self):
        plan = self.plan()
        plan["queries"] = ['* OR private:everything']
        with self.assertRaises(ValueError):
            validate_plan(plan)
        with self.assertRaises(ValueError):
            execute(plan, self.root / "bad")
        self.assertFalse((self.root / "bad").exists())

    def test_schema_zero_and_malformed_are_distinct(self):
        for bad in [{}, {"hitCount": False, "resultList": {"result": []}},
                    {"hitCount": 1, "resultList": {"result": []}},
                    {"hitCount": 0, "resultList": {"result": [article("1")]}},
                    page(rows=[{"source": "MED", "id": "1"}])]:
            out = self.root / ("bad" + str(len(list(self.root.iterdir()))))
            report = execute(self.plan(), out, online=True, client=FakeClient(lambda _, value=bad: value))
            self.assertEqual(report["status"], "partial")
            self.assertTrue(report["errors"])
        report, _, _ = self.run_discovery(lambda _: {"hitCount": 0})
        self.assertEqual(report["status"], "bounded_retrieval_finished")
        self.assertEqual(report["counts"]["candidates"], 0)

    def test_repeated_cursor_retains_first_page(self):
        report, fake, _ = self.run_discovery(lambda _: page(rows=[article("1"), article("2")], count=4, cursor="*"))
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["counts"]["candidates"], 2)
        self.assertEqual(len(fake.urls), 1)

    def test_repeated_page_and_changing_count_are_errors(self):
        for change in (False, True):
            calls = []
            def handler(url):
                calls.append(url)
                return page(rows=[article("1"), article("2")], count=5 if change and len(calls) == 2 else 4)
            report = execute(self.plan(), self.root / str(change), online=True, client=FakeClient(handler))
            self.assertEqual(report["status"], "partial")
            self.assertEqual(report["counts"]["candidates"], 2)

    def test_request_page_and_record_limits_are_explicit(self):
        for key in ("requests", "pages", "records"):
            plan = self.plan(**{key: 1})
            report = execute(plan, self.root / key, online=True,
                             client=FakeClient(lambda _: page(rows=[article("1"), article("2")], count=4)))
            self.assertEqual(report["status"], "partial")
            self.assertEqual(report["truncations"][0]["reason"], key[:-1] + "_budget" if key != "pages" else "page_budget")
            self.assertLessEqual(report["counts"]["requests"], plan["budgets"]["requests"])
            self.assertLessEqual(report["counts"]["observations"], plan["budgets"]["records"])

    def test_backward_forward_pagination_and_cycle(self):
        plan = self.plan(seeds=[{"source": "MED", "id": "1"}], depth=3)
        def handler(url):
            path = urlsplit(url).path
            kind = path.rsplit("/", 1)[1]
            if kind == "search":
                return page()
            parent = path.split("/")[-2]
            number = int(parse_qs(urlsplit(url).query)["page"][0])
            if parent == "1" and kind == "references":
                return page(kind, [article("2"), article("3")] if number == 1 else [article("4")], 3)
            if parent == "2" and kind == "references":
                return page(kind, [article("1")])
            if parent == "1" and kind == "citations":
                return page(kind, [article("5")])
            return page(kind)
        report, fake, _ = self.run_discovery(handler, plan)
        self.assertEqual(report["counts"]["candidates"], 5)
        edges = {(e["from"], e["to"]) for e in report["edges"]}
        self.assertIn(("epmc:MED:1", "epmc:MED:2"), edges)
        self.assertIn(("epmc:MED:5", "epmc:MED:1"), edges)
        self.assertIn(("epmc:MED:2", "epmc:MED:1"), edges)
        self.assertEqual(sum("/MED/1/references?" in url and "page=1" in url for url in fake.urls), 1)

    def test_identity_dedup_preserves_conflicting_metadata_and_routes(self):
        calls = []
        def handler(url):
            calls.append(url)
            title = "Title A" if len(calls) == 1 else "Title B"
            return page(rows=[article("1", title, doi="10.1234/synthetic")])
        report, _, _ = self.run_discovery(handler, self.plan(aliases=["extra term"]))
        self.assertEqual(report["counts"]["candidates"], 1)
        candidate = report["candidates"][0]
        self.assertEqual(candidate["conflicts"]["title"], ["Title A", "Title B"])
        self.assertEqual(len(candidate["observations"]), 3)
        self.assertIn("user_alias", {obs["route"].get("route_class") for obs in candidate["observations"]})
        self.assertEqual(candidate["status"], "unreviewed")
        self.assertIsNone(candidate["approval"])

    def test_title_only_references_remain_unresolved(self):
        def handler(url):
            kind = urlsplit(url).path.rsplit("/", 1)[1]
            return page(kind, [{"title": "Same name", "unstructured": "10.9999/not-an-explicit-doi"}]) if kind == "references" else page(kind)
        report, _, _ = self.run_discovery(handler, self.plan(seeds=[{"source": "MED", "id": "1"}]))
        self.assertEqual(report["counts"]["unresolved"], 1)
        self.assertEqual(report["counts"]["candidates"], 1)
        self.assertFalse(report["edges"])

    def test_bridge_merge_during_citation_queue_preserves_closed_graph(self):
        plan = self.plan(seeds=[{"source": "MED", "id": "1"}], depth=3)
        def handler(url):
            kind = urlsplit(url).path.rsplit("/", 1)[1]
            parent = urlsplit(url).path.split("/")[-2]
            if kind == "search":
                return page(rows=[article("2"), article("3", doi="10.1234/bridge")])
            if parent == "1" and kind == "references":
                return page(kind, [article("3"), article("2", doi="10.1234/bridge")])
            if parent == "3" and kind == "references":
                return page(kind, [article("4")])
            return page(kind)
        report, _, _ = self.run_discovery(handler, plan)
        identities = {row["candidate_id"] for row in report["candidates"]}
        self.assertTrue(report["identity_redirects"])
        for edge in report["edges"]:
            self.assertIn(edge["from"], identities)
            self.assertIn(edge["to"], identities)

    def test_crossref_fallback_retains_unresolved_and_forward_unknown(self):
        def handler(url):
            if url.startswith("https://api.crossref.org/"):
                return {"status": "ok", "message": {"DOI": "10.1234/seed", "reference-count": 2,
                        "reference": [{"DOI": "10.1234/child"}, {"unstructured": "Unknown paper"}]}}
            return page()
        report, _, _ = self.run_discovery(handler, self.plan(seeds=[{"doi": "10.1234/seed"}]))
        self.assertEqual(report["counts"]["candidates"], 2)
        self.assertEqual(report["counts"]["unresolved"], 1)
        self.assertEqual(report["seed_routes"][0]["registration"]["status"], "registered")
        self.assertEqual(report["seed_routes"][0]["doi_resolution"]["status"], "zero_hit")
        self.assertEqual(report["seed_routes"][0]["forward_citations"]["status"], "unavailable")
        self.assertIn("no_forward_endpoint", report["seed_routes"][0]["forward_citations"]["reason"])
        self.assertFalse(report["exhaustive_recall"])

    def test_doi_fallback_gets_budget_before_keyword_continuations(self):
        plan = self.plan(seeds=[{"doi": "10.1234/seed"}], pages=5)
        def handler(url):
            if url.startswith("https://api.crossref.org/works/10.1234%2Fseed"):
                return {"status": "ok", "message": {"DOI": "10.1234/seed",
                        "reference-count": 0, "reference": []}}
            params = parse_qs(urlsplit(url).query)
            if params.get("query") == ['DOI:"10.1234/seed"']:
                return page()
            cursor = params.get("cursorMark", ["*"])[0]
            offset = 0 if cursor == "*" else int(cursor.split("-")[-1]) * 2
            next_cursor = "cursor-1" if cursor == "*" else "cursor-" + str(int(cursor.split("-")[-1]) + 1)
            return page(rows=[article(str(100 + offset)), article(str(101 + offset))],
                        count=100, cursor=next_cursor)
        report, fake, _ = self.run_discovery(handler, plan)
        crossref_url = next(i for i, url in enumerate(fake.urls)
                            if url.startswith("https://api.crossref.org/works/10.1234%2Fseed"))
        continuation_urls = [i for i, url in enumerate(fake.urls)
                             if parse_qs(urlsplit(url).query).get("cursorMark", ["*"])[0] != "*"]
        self.assertTrue(continuation_urls, (report["counts"], report["truncations"], report["coverage"], fake.urls))
        self.assertLess(crossref_url, min(continuation_urls))
        crossref_routes = [row for row in report["coverage"]
                           if row["route"].get("provider") == "crossref"
                           and row["route"].get("seed") == {"doi": "10.1234/seed"}
                           and row["route"].get("direction") == "backward"]
        self.assertEqual(len(crossref_routes), 1)
        self.assertEqual(crossref_routes[0]["status"], "api_reported_end")
        seed_route = report["seed_routes"][0]["backward_citations"]
        self.assertEqual(seed_route["route"], crossref_routes[0]["route"])
        self.assertEqual(seed_route["status"], crossref_routes[0]["status"])
        self.assertLessEqual(report["counts"]["pages"], plan["budgets"]["pages"])

    def test_doi_resolver_pagination_completes_before_crossref_fallback(self):
        plan = self.plan(seeds=[{"doi": "10.1234/seed"}], aliases=["specific alias"], pages=8)
        doi_calls = 0

        def handler(url):
            nonlocal doi_calls
            parts = urlsplit(url)
            if parts.netloc == "api.crossref.org":
                self.fail("Crossref fallback must wait for the paginating DOI resolver")
            if parts.path.endswith("/references") or parts.path.endswith("/citations"):
                return page(parts.path.rsplit("/", 1)[1])
            query = parse_qs(parts.query).get("query", [""])[0]
            if query == 'DOI:"10.1234/seed"':
                doi_calls += 1
                cursor = parse_qs(parts.query).get("cursorMark", ["*"])[0]
                if cursor == "*":
                    return page(rows=[article("50", doi="10.1234/seed"),
                                      article("51", doi="10.1234/seed")], count=3,
                                cursor="resolver-next")
                return page(rows=[article("52", doi="10.1234/seed")], count=3,
                            cursor="resolver-finished")
            return page()

        report, fake, _ = self.run_discovery(handler, plan)
        self.assertEqual(doi_calls, 2)
        self.assertFalse(any(urlsplit(url).netloc == "api.crossref.org" for url in fake.urls))
        seed_route = report["seed_routes"][0]
        self.assertEqual(seed_route["doi_resolution"]["status"], "api_reported_end")
        self.assertEqual(seed_route["backward_citations"]["status"], "zero_hit")
        self.assertEqual(seed_route["forward_citations"]["status"], "zero_hit")
        for field, direction in (("backward_citations", "backward"),
                                 ("forward_citations", "forward")):
            audit = [row for row in report["coverage"]
                     if row["route"].get("seed") == {"doi": "10.1234/seed"}
                     and row["route"].get("direction") == direction
                     and row["route"].get("provider") == "europepmc"]
            self.assertEqual(len(audit), 1, field)
            self.assertEqual(seed_route[field]["route"], audit[0]["route"])

    def test_depth_zero_registers_seed_without_neighbor_requests(self):
        plan = self.plan(seeds=[{"source": "MED", "id": "1"}], depth=0)
        report, fake, _ = self.run_discovery(lambda _: page(), plan)
        paths = [urlsplit(url).path for url in fake.urls]
        self.assertFalse(any(path.endswith("/references") or path.endswith("/citations") for path in paths))
        self.assertEqual(report["counts"]["candidates"], 1)
        for field in ("backward_citations", "forward_citations"):
            detail = report["seed_routes"][0][field]
            self.assertEqual(detail["status"], "budget_truncated")
            self.assertEqual(detail["reason"], "depth_budget")
        self.assertFalse(report["edges"])

    def test_depth_one_fetches_seed_neighbors_but_does_not_expand_them(self):
        plan = self.plan(seeds=[{"source": "MED", "id": "1"}], depth=1)
        def handler(url):
            path = urlsplit(url).path
            if path.endswith("/references"):
                parent = path.split("/")[-2]
                return page("references", [article("2")]) if parent == "1" else page("references")
            if path.endswith("/citations"):
                parent = path.split("/")[-2]
                return page("citations", [article("3")]) if parent == "1" else page("citations")
            return page("search")
        report, fake, _ = self.run_discovery(handler, plan)
        paths = [urlsplit(url).path for url in fake.urls]
        self.assertIn("/europepmc/webservices/rest/MED/1/references", paths)
        self.assertIn("/europepmc/webservices/rest/MED/1/citations", paths)
        self.assertFalse(any("/MED/2/" in path or "/MED/3/" in path for path in paths))
        self.assertTrue(any(row["reason"] == "depth_budget" for row in report["truncations"]))

    def test_alias_and_seed_get_first_pages_before_broad_query_continuations(self):
        plan = self.plan(seeds=[{"source": "MED", "id": "1"}], aliases=["specific alias"], pages=6)
        def handler(url):
            parts = urlsplit(url)
            params = parse_qs(parts.query)
            if parts.path.endswith("/references") or parts.path.endswith("/citations"):
                kind = parts.path.rsplit("/", 1)[1]
                return page(kind)
            query = params["query"][0]
            if query == 'TITLE_ABS:"specific alias"':
                return page()
            cursor = params["cursorMark"][0]
            ids = (1, 2) if cursor == "*" else (3, 4)
            return page(rows=[article(str(i)) for i in ids], count=6,
                        cursor="next" if cursor == "*" else "next2")
        report, client, _ = self.run_discovery(handler, plan)
        parsed = [(urlsplit(url).path, parse_qs(urlsplit(url).query)) for url in client.urls]
        self.assertIn('TITLE_ABS:"specific alias"', parsed[0][1].get("query", []))
        self.assertTrue(any(path.endswith("/MED/1/references") for path, _ in parsed[:5]))
        self.assertTrue(any(path.endswith("/MED/1/citations") for path, _ in parsed[:5]))
        self.assertEqual(parsed[5][1]["query"], [plan["queries"][0]])
        self.assertEqual(parsed[5][1]["cursorMark"], ["next"])
        self.assertEqual(report["counts"]["page_attempts"], 6)
        self.assertEqual(report["counts"]["pages"], 6)
        term_route = next(row for row in report["coverage"]
                          if row["route"].get("route_class") == "topic_terms")
        alias_route = next(row for row in report["coverage"]
                           if row["route"].get("route_class") == "user_alias")
        seed_route = next(row for row in report["coverage"]
                          if row["route"].get("provider") == "europepmc"
                          and row["route"].get("direction") == "backward")
        source_route = next(row for row in report["coverage"]
                            if row["route"].get("route_class") == "source_baseline")
        self.assertEqual([alias_route["priority_order"], seed_route["priority_order"],
                          source_route["priority_order"], term_route["priority_order"]], [1, 2, 3, 4])
        self.assertEqual(term_route["status"], "budget_truncated")
        self.assertEqual(term_route["pages_validated"], 1)
        self.assertFalse(report["exhaustive_recall"])

    def test_failed_and_unattempted_routes_are_distinct_and_failures_are_not_pages(self):
        plan = self.plan(seeds=[{"source": "MED", "id": "1"}], aliases=["fragile alias"], requests=1)
        def handler(url):
            if parse_qs(urlsplit(url).query).get("query") == ['TITLE_ABS:"fragile alias"']:
                return RuntimeError("network error")
            return page()
        report, client, _ = self.run_discovery(handler, plan)
        self.assertEqual(len(client.urls), 1)
        self.assertEqual(report["counts"]["requests"], 1)
        self.assertEqual(report["counts"]["page_attempts"], 1)
        self.assertEqual(report["counts"]["pages"], 0)
        alias_route = next(row for row in report["coverage"]
                           if row["route"].get("route_class") == "user_alias")
        source_route = next(row for row in report["coverage"]
                            if row["route"].get("route_class") == "source_baseline")
        self.assertEqual(alias_route["status"], "failed")
        self.assertEqual(alias_route["priority_order"], 1)
        self.assertEqual(source_route["status"], "unattempted")
        self.assertEqual(source_route["priority_order"], 3)
        self.assertTrue(source_route["reason"] in (None, "request_budget"))

    def test_seed_record_budget_keeps_all_routes_and_marks_uncovered_work(self):
        plan = self.plan(seeds=[{"source": "MED", "id": "1"}, {"source": "MED", "id": "2"}],
                         aliases=["specific alias"], records=1)
        report, client, _ = self.run_discovery(
            lambda _: self.fail("record-budget stop must happen before network requests"), plan)
        self.assertFalse(client.urls)
        self.assertEqual(report["counts"]["observations"], 1)
        self.assertEqual(len(report["seed_routes"]), 2)
        self.assertEqual(report["seed_routes"][0]["registration"]["status"], "registered")
        self.assertEqual(report["seed_routes"][1]["registration"]["status"], "budget_truncated")
        for seed_route in report["seed_routes"]:
            self.assertEqual(seed_route["backward_citations"]["status"], "budget_truncated")
            self.assertEqual(seed_route["forward_citations"]["status"], "budget_truncated")
        alias_route = next(row for row in report["coverage"]
                           if row["route"].get("route_class") == "user_alias")
        self.assertEqual(alias_route["status"], "budget_truncated")
        self.assertEqual(alias_route["reason"], "record_budget")

    def test_partial_failure_keeps_work_and_does_not_echo_exception(self):
        sentinel = "private" + "_credential_" + "marker"
        def handler(url):
            kind = urlsplit(url).path.rsplit("/", 1)[1]
            if kind == "references":
                return RuntimeError(sentinel)
            return page(kind, [article("2")]) if kind == "citations" else page(kind)
        report, fake, out = self.run_discovery(handler, self.plan(seeds=[{"source": "MED", "id": "1"}]))
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["counts"]["candidates"], 2)
        self.assertTrue(any("/citations?" in url for url in fake.urls))
        self.assertNotIn(sentinel, (out / "discovery.json").read_text())

    def test_outputs_refuse_overwrite_and_replay_tampering(self):
        plan = self.plan()
        report, _, out = self.run_discovery(lambda _: page(), plan)
        before = (out / "discovery.json").read_bytes()
        with self.assertRaises(ValueError):
            execute(plan, out)
        self.assertEqual(before, (out / "discovery.json").read_bytes())
        (out / "response_0001.json").write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "digest"):
            execute(plan, self.root / "replay", replay_dir=out)
        self.assertFalse((self.root / "replay").exists())

    def test_cli_inputs_strict_json_and_offline_default(self):
        seeds = self.root / "seeds.json"
        seeds.write_text('[{"doi":"10.1234/test","doi":"10.1234/other"}]')
        self.assertEqual(main(["--out-dir", str(self.root / "bad"), "--seeds-json", str(seeds)]), 1)
        self.assertFalse((self.root / "bad").exists())
        self.assertEqual(main(["--out-dir", str(self.root / "ok")]), 0)

    def test_epmc_repeated_id_changed_title_is_not_complete(self):
        calls = []
        def handler(url):
            calls.append(url)
            return page(rows=[article("1"), article("2")], count=4) if len(calls) == 1 else page(
                rows=[article("1", "Changed title"), article("3")], count=4)
        report, _, _ = self.run_discovery(handler)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["counts"]["candidates"], 2)

    def test_crossref_offline_plan_stores_individual_literal_queries(self):
        plan = self.plan(aliases=["additional alias"], sources=["crossref"])
        execute(plan, self.root / "crossref")
        self.assertEqual(plan["sources"], ["crossref"])
        self.assertEqual(plan["queries"], [])
        self.assertIn("additional alias", plan["crossref_queries"])
        self.assertTrue(all("TITLE_ABS" not in query for query in plan["crossref_queries"]))
        self.assertEqual(main(["--topic", "wet_etch", "--source", "crossref", "--out-dir", str(self.root / "cli")]), 0)

    def test_crossref_two_pages_alias_routes_replay(self):
        plan = self.plan(aliases=["additional alias"], sources=["crossref"])
        first_query = plan["crossref_queries"][0]
        def handler(url):
            params = parse_qs(urlsplit(url).query)
            query, cursor = params["query"][0], params["cursor"][0]
            if query == first_query:
                rows = [crossref_article("1"), crossref_article("2")] if cursor == "*" else [crossref_article("3")]
                return crossref_page(rows, count=3, query=query)
            return crossref_page([crossref_article("1")], query=query)
        report, fake, out = self.run_discovery(handler, plan)
        self.assertEqual(report["status"], "bounded_retrieval_finished")
        self.assertEqual(report["counts"]["candidates"], 3)
        candidate = report["candidates"][0]
        self.assertEqual(len(candidate["observations"]), len(plan["crossref_queries"]))
        self.assertEqual(len(fake.urls), len(plan["crossref_queries"]) + 1)
        self.assertEqual(execute(plan, self.root / "replay", replay_dir=out), report)

    def test_crossref_malformed_totals_cursor_and_duplicates_are_partial(self):
        for label in ("total", "cursor", "duplicate", "query"):
            calls = []
            def handler(url):
                query = parse_qs(urlsplit(url).query)["query"][0]
                calls.append(url)
                if label == "total":
                    return crossref_page([], count="0")
                if label == "query":
                    return crossref_page([], query="wrong query")
                if len(calls) == 1:
                    return crossref_page([crossref_article("1"), crossref_article("2")], count=4,
                                         cursor="*" if label == "cursor" else "next")
                return crossref_page([crossref_article("1", "changed title"), crossref_article("3")], count=4)
            report = execute(self.plan(sources=["crossref"]), self.root / label, online=True, client=FakeClient(handler))
            self.assertEqual(report["status"], "partial")
            self.assertTrue(report["errors"])

    def test_mixed_provider_identity_merge_preserves_native_payloads(self):
        plan = self.plan(sources=["europepmc", "crossref"])
        def handler(url):
            if urlsplit(url).netloc == "api.crossref.org":
                return crossref_page([crossref_article("same", "Publisher title")])
            return page(rows=[article("1", "Index title", doi="10.1234/same")])
        report, _, _ = self.run_discovery(handler, plan)
        self.assertEqual(report["counts"]["candidates"], 1)
        candidate = report["candidates"][0]
        self.assertEqual(set(candidate["identifiers"]), {"epmc:MED:1", "doi:10.1234/same"})
        self.assertEqual({obs["route"]["provider"] for obs in candidate["observations"]}, {"europepmc", "crossref"})
        self.assertIn("title", candidate["conflicts"])

    def test_crossref_existing_keyword_candidate_still_expands_from_seed(self):
        plan = self.plan(sources=["crossref"], seeds=[{"doi": "10.1234/seed"}], depth=2)
        def handler(url):
            parts = urlsplit(url)
            if parts.netloc == "www.ebi.ac.uk":
                return page()
            if parts.path == "/works":
                return crossref_page([crossref_article("child")])
            doi = parts.path.rsplit("/", 1)[1]
            parent = "10.1234/seed" if doi.endswith("seed") else "10.1234/child"
            return {"status": "ok", "message": {"DOI": parent, "reference-count": 1,
                    "reference": [{"DOI": "10.1234/child" if parent.endswith("seed") else "10.1234/grandchild"}]}}
        report, fake, _ = self.run_discovery(handler, plan)
        self.assertTrue(any("/works/10.1234%2Fchild" in url for url in fake.urls))
        self.assertIn("doi:10.1234/grandchild", {row["candidate_id"] for row in report["candidates"]})

    def test_crossref_budget_cutoff_retains_partial_candidates(self):
        plan = self.plan(sources=["crossref"], records=1)
        report, fake, _ = self.run_discovery(lambda _: crossref_page(
            [crossref_article("1"), crossref_article("2")], count=5), plan)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["counts"]["observations"], 1)
        self.assertEqual(report["counts"]["candidates"], 1)
        self.assertEqual(len(fake.urls), 1)
        self.assertEqual(report["truncations"][0]["reason"], "record_budget")


if __name__ == "__main__":
    unittest.main()
