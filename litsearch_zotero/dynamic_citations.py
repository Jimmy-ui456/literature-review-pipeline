"""Bind explicit DOI citations to Zotero items and prepare refreshable Word fields.

No library writes, guessed references, network access on import, or Word automation
on import. Zotero's installed Word plugin performs final CSL formatting.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
import uuid
from zipfile import BadZipFile, ZipFile, ZIP_DEFLATED

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CP = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
VT = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
NS = {"w": W}
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s;,，；\]<>]+", re.I)
BRACKETS = re.compile(r"\[[^\[\]\r\n]+\]")
SCHEMA = "https://github.com/citation-style-language/schema/raw/master/csl-citation.json"
DEFAULT_STYLE = "http://www.zotero.org/styles/ieee"
ITEM_URI = re.compile(r"http://zotero\.org/(?:users/(?:local/[A-Za-z0-9]+|\d+)|groups/\d+)/items/[A-Z0-9]{8}")
MAX_JSON_BYTES = 64 * 1024 * 1024


def xml_module():
    try:
        from lxml import etree
    except ImportError as exc:
        raise ValueError('Install the optional dependencies: pip install ".[citations]"') from exc
    return etree


def parse_xml(raw: bytes):
    et = xml_module()
    try:
        root = et.fromstring(raw, et.XMLParser(resolve_entities=False, no_network=True))
    except et.XMLSyntaxError as exc:
        raise ValueError("Malformed document XML") from exc
    if root.getroottree().docinfo.doctype:
        raise ValueError("Document XML must not contain a DTD")
    return root


def xml_bytes(element) -> bytes:
    return xml_module().tostring(element, xml_declaration=True, encoding="UTF-8", standalone=True)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def norm_doi(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("DOI must be a string")
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value.strip(), flags=re.I).lower()


def strict_json(value):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError(f"Non-finite JSON value: {value}")
    try:
        return json.loads(value, object_pairs_hook=unique, parse_constant=invalid_constant)
    except (RecursionError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid JSON encoding or nesting depth") from exc


def read_json(path: Path):
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError("JSON input exceeds 64 MB")
    return strict_json(path.read_text(encoding="utf-8-sig"))


def write_new(path: Path, payload: bytes) -> None:
    """Publish only complete bytes, atomically and without replacing a target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "nt":
            # Windows rename refuses an existing target, unlike POSIX rename.
            os.rename(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json_new(path: Path, value) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    write_new(path, payload.encode("utf-8"))


class LocalAPI:
    """Read only, fixed loopback origin, no proxy or redirect to another host."""

    def __init__(self, base: str = "http://127.0.0.1:23119", timeout: int = 20):
        u = urlsplit(base)
        if (u.scheme != "http" or u.hostname not in {"localhost", "127.0.0.1", "::1"}
                or u.username or u.password or u.path not in {"", "/"} or u.query or u.fragment
                or u.port != 23119):
            raise ValueError("Zotero API must be loopback HTTP on port 23119 with no path or credentials")
        self.base = base.rstrip("/")
        self.timeout = timeout

    def get(self, path: str):
        from urllib.request import HTTPRedirectHandler

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise ValueError("Zotero local API redirects are not followed")

        if not path.startswith("/api/") or ".." in path or "#" in path:
            raise ValueError("Only Zotero /api/ read routes are supported")
        req = Request(self.base + path, headers={"Zotero-API-Version": "3"})
        with build_opener(ProxyHandler({}), NoRedirect()).open(req, timeout=self.timeout) as response:
            raw = response.read(MAX_JSON_BYTES + 1)
            if len(raw) > MAX_JSON_BYTES:
                raise ValueError("Zotero API response exceeds 64 MB")
            return strict_json(raw)

    def snapshot(self) -> list[dict]:
        result = []
        seen = set()
        for start in range(0, 100000, 100):
            rows = self.get(f"/api/users/0/items/top?limit=100&start={start}&include=data,csljson")
            if not isinstance(rows, list):
                raise ValueError("Invalid Zotero item response")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("key"), str):
                    raise ValueError("Invalid Zotero item row")
                identity = (row.get("library", {}).get("id"), row["key"])
                if identity in seen:
                    raise ValueError("Zotero pagination repeated an item; retry after library activity stops")
                seen.add(identity)
            result.extend(rows)
            if len(rows) < 100:
                return result
        raise ValueError("Zotero snapshot exceeded pagination budget")


def document_root(path: Path):
    if path.suffix.lower() != ".docx":
        raise ValueError("Only .docx files are supported")
    with ZipFile(path) as z:
        if len(z.namelist()) != len(set(z.namelist())):
            raise ValueError("Duplicate DOCX archive entries")
        if sum(i.file_size for i in z.infolist()) > 200 * 1024 * 1024:
            raise ValueError("DOCX uncompressed size exceeds 200 MB")
        return parse_xml(z.read("word/document.xml"))


def paragraph_text(paragraph) -> str:
    return "".join(paragraph.xpath(".//w:t/text()", namespaces=NS))


def numeric_refs(text: str) -> list[str]:
    if not re.fullmatch(r"\[\s*\d+(?:\s*[-–—]\s*\d+)?(?:\s*[,，]\s*\d+(?:\s*[-–—]\s*\d+)?)*\s*\]", text):
        if re.fullmatch(r"\[[\d\s,，–—\-]+\]", text):
            raise ValueError("Invalid numeric citation syntax")
        return []
    result = []
    for part in re.split(r"[,，]", text[1:-1]):
        limits = re.split(r"[-–—]", part.strip())
        a, b = int(limits[0]), int(limits[-1])
        if not 0 < a <= b <= a + 500:
            raise ValueError("Invalid or oversized citation range")
        result.extend(str(n) for n in range(a, b + 1))
    if len(result) != len(set(result)):
        raise ValueError("Duplicate reference numbers in citation")
    return result


def rendered_numbers(text: str) -> list[str]:
    """Read numeric CSL results including IEEE's separate bracket pairs."""
    text = re.sub(r"\]\s*[-–—]\s*\[", "–", text.strip())
    if not re.fullmatch(r"\[\d[\d\s,，–—\-]*\](?:\s*[,，]\s*\[\d[\d\s,，–—\-]*\])*", text):
        return []
    return [n for part in re.findall(r"\[[^\]]+\]", text) for n in numeric_refs(part)]


def scan(path: Path, references: dict | None = None) -> dict:
    if references is not None and (not isinstance(references, dict) or any(
            not isinstance(n, str) or not re.fullmatch(r"[1-9]\d*", n)
            or not isinstance(d, str) or not DOI_RE.fullmatch(norm_doi(d)) for n, d in references.items())):
        raise ValueError("References must map positive reference-number strings to complete DOI strings")
    root = document_root(path)
    groups, unresolved_numeric = [], []
    in_bibliography = False
    for index, paragraph in enumerate(root.xpath("/w:document/w:body//w:p", namespaces=NS)):
        text = paragraph_text(paragraph)
        if text.strip() in {"参考文献", "References", "Bibliography"}:
            in_bibliography = True
        if in_bibliography:
            continue
        for match in BRACKETS.finditer(text):
            dois = []
            if DOI_RE.search(match[0]):
                for part in re.split(r"[;；]", match[0][1:-1]):
                    matches = DOI_RE.findall(part)
                    if len(matches) != 1:
                        raise ValueError("Every source in a DOI citation group must have exactly one explicit DOI; use semicolons")
                    tail = part[part.find(matches[0]) + len(matches[0]):].strip()
                    if tail.strip(".,，。 "):
                        raise ValueError("Unexpected text after a DOI may contain another source; use one DOI per semicolon-delimited source")
                    dois.append(norm_doi(matches[0]))
                dois = list(dict.fromkeys(dois))
            elif references is None and numeric_refs(match[0]):
                unresolved_numeric.append(match[0])
            if not dois and references is not None:
                ids = numeric_refs(match[0])
                if ids:
                    missing = [n for n in ids if n not in references]
                    if missing:
                        raise ValueError(f"No explicit DOI mapping for reference numbers: {missing}")
                    dois = list(dict.fromkeys(norm_doi(references[n]) for n in ids))
            if dois:
                groups.append({"paragraph": index, "start": match.start(), "end": match.end(),
                               "original": match[0], "dois": dois})
    field_codes = root.xpath("//w:instrText/text()|//w:fldSimple/@w:instr", namespaces=NS)
    with ZipFile(path) as archive:
        for name in archive.namelist():
            if re.fullmatch(r"word/(?:header\d+|footer\d+|footnotes|endnotes)\.xml", name):
                other = parse_xml(archive.read(name))
                field_codes.extend(other.xpath("//w:instrText/text()|//w:fldSimple/@w:instr", namespaces=NS))
                if any(DOI_RE.search(x) for x in other.xpath("//w:t/text()", namespaces=NS)):
                    raise ValueError("DOI citations outside the main document body require manual handling")
    return {"schema_version": 1, "input": str(path.resolve()), "input_sha256": sha256(path),
            "groups": groups, "dois": list(dict.fromkeys(d for g in groups for d in g["dois"])),
            "existing_fields": field_codes, "unresolved_numeric": unresolved_numeric,
            "references": references,
            "existing_bibliography": in_bibliography}


def item_binding(row: dict) -> dict:
    data = row["data"]
    raw = row.get("csljson")
    csl = strict_json(raw) if isinstance(raw, str) else raw
    if not isinstance(csl, list) or len(csl) != 1 or not isinstance(csl[0], dict):
        raise ValueError(f"Missing CSL metadata: {row['key']}")
    csl = deepcopy(csl[0])
    uri = row.get("links", {}).get("alternate", {}).get("href", "")
    uri = uri.replace("https://www.zotero.org/", "http://zotero.org/").replace(
        "https://zotero.org/", "http://zotero.org/")
    if not ITEM_URI.fullmatch(uri):
        raise ValueError(f"Invalid canonical Zotero item URI: {row['key']}")
    if uri.rsplit("/", 1)[-1] != row["key"]:
        raise ValueError("Zotero item key/URI mismatch")
    doi = norm_doi(data.get("DOI", ""))
    if not DOI_RE.fullmatch(doi) or doi != norm_doi(csl.get("DOI", "")):
        raise ValueError(f"Zotero/CSL DOI mismatch: {row['key']}")
    if not csl.get("title") or not csl.get("issued") or not csl.get("author"):
        raise ValueError(f"Incomplete author/title/year metadata: {row['key']}")
    csl["id"] = uri
    # Abstracts are not needed by the citation processor and inflate every field.
    csl.pop("abstract", None)
    return {"doi": doi, "key": row["key"], "uri": uri, "version": row.get("version"),
            "itemData": csl}


def prepare(path: Path, snapshot: list[dict], selections: dict | None = None,
            references: dict | None = None, style: str = DEFAULT_STYLE) -> dict:
    result = scan(path, references)
    if not result["groups"]:
        raise ValueError("No explicit DOI citations found; numeric citations require --references JSON")
    if result["unresolved_numeric"]:
        raise ValueError("Static numeric citations require an explicit --references mapping; mixed citations cannot be partially converted")
    codes = "".join(result["existing_fields"])
    if "ZOTERO_" in codes or "EN.CITE" in codes:
        raise ValueError("Document already contains citation fields; use the Word plugin instead of reconverting")
    if not isinstance(style, str) or not re.fullmatch(r"https?://www\.zotero\.org/styles/[a-z0-9-]+", style):
        raise ValueError("Style must be a Zotero style identifier URL")
    if not isinstance(snapshot, list) or any(not isinstance(r, dict) or not isinstance(r.get("data"), dict) for r in snapshot):
        raise ValueError("Snapshot must be an array of Zotero item records")
    if selections is not None and (not isinstance(selections, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) or not re.fullmatch(r"[A-Z0-9]{8}", v)
            for k, v in selections.items())):
        raise ValueError("Selections must map DOI strings to Zotero item keys")
    selections = {norm_doi(k): v for k, v in (selections or {}).items()}
    bindings, issues = {}, []
    for doi in result["dois"]:
        rows = [r for r in snapshot if r.get("data", {}).get("itemType") not in {"note", "attachment", "annotation"}
                and norm_doi(r.get("data", {}).get("DOI", "")) == doi]
        if doi in selections:
            rows = [r for r in rows if r["key"] == selections[doi]]
        if len(rows) != 1:
            issues.append({"doi": doi, "reason": "missing" if not rows else "ambiguous",
                           "candidate_keys": [r["key"] for r in rows]})
            continue
        try:
            bindings[doi] = item_binding(rows[0])
        except ValueError as exc:
            issues.append({"doi": doi, "reason": str(exc)})
    result.update({"bindings": bindings, "issues": issues, "ready": not issues,
                   "style": style, "scientific_validation": "not_performed"})
    return result


def text_run(text: str, properties=None):
    et = xml_module()
    run = et.Element(f"{{{W}}}r")
    if properties is not None:
        run.append(deepcopy(properties))
    t = et.SubElement(run, f"{{{W}}}t")
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    t.text = text
    return run


def field_runs(code: str, display: str, properties=None) -> list:
    et = xml_module()
    result = []
    for kind in ("begin", "instruction", "separate", "result", "end"):
        run = et.Element(f"{{{W}}}r")
        if properties is not None:
            run.append(deepcopy(properties))
        if kind in {"begin", "separate", "end"}:
            el = et.SubElement(run, f"{{{W}}}fldChar")
            el.set(f"{{{W}}}fldCharType", kind)
        elif kind == "instruction":
            el = et.SubElement(run, f"{{{W}}}instrText")
            el.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            el.text = " " + code + " "
        else:
            run = text_run(display, properties)
        result.append(run)
    return result


def replace_span(paragraph, start: int, end: int, nodes: list) -> None:
    """Replace only simple text runs touched by the span; preserve other XML."""
    cursor, touched = 0, []
    for child in paragraph:
        text = "".join(child.xpath(".//w:t/text()", namespaces=NS))
        stop = cursor + len(text)
        if text and start < stop and end > cursor:
            if child.tag != f"{{{W}}}r" or any(c.tag not in {f"{{{W}}}rPr", f"{{{W}}}t", f"{{{W}}}lastRenderedPageBreak"} for c in child):
                raise ValueError("Citation intersects a hyperlink, revision, drawing, or existing field; manual mapping required")
            touched.append((child, cursor, stop, text))
        cursor = stop
    if not touched or touched[0][1] > start or touched[-1][2] < end:
        raise ValueError("Citation span is not contiguous visible text")
    first, a, _, first_text = touched[0]
    last, _, b, last_text = touched[-1]
    position = paragraph.index(first)
    span_children = list(paragraph)[position:paragraph.index(last) + 1]
    if any(c.tag not in {f"{{{W}}}r", f"{{{W}}}proofErr"} for c in span_children):
        raise ValueError("Citation crosses a bookmark or unsupported XML boundary")
    if any(c.tag == f"{{{W}}}r" and any(x.tag not in {f"{{{W}}}rPr", f"{{{W}}}t", f"{{{W}}}lastRenderedPageBreak"} for x in c)
           for c in span_children):
        raise ValueError("Citation crosses a non-text run")
    prefix = first_text[:start - a]
    suffix = last_text[len(last_text) - (b - end):] if b > end else ""
    new = ([text_run(prefix, first.find(f"{{{W}}}rPr"))] if prefix else []) + nodes
    if suffix:
        new.append(text_run(suffix, last.find(f"{{{W}}}rPr")))
    for child in span_children:
        paragraph.remove(child)
    for offset, child in enumerate(new):
        paragraph.insert(position + offset, child)


def set_preferences(members: dict[str, bytes], style: str) -> None:
    et = xml_module()
    root = parse_xml(members["docProps/custom.xml"]) if "docProps/custom.xml" in members else et.Element(
        f"{{{CP}}}Properties", nsmap={None: CP, "vt": VT})
    for p in list(root):
        if p.get("name", "").startswith("ZOTERO_PREF_"):
            root.remove(p)
    pid = max((int(p.get("pid", "1")) for p in root), default=1) + 1
    prop = et.SubElement(root, f"{{{CP}}}property", name="ZOTERO_PREF_1", pid=str(pid),
                         fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}")
    value = et.SubElement(prop, f"{{{VT}}}lpwstr")
    data = et.Element("data", {"data-version": "3", "zotero-version": "7.0"})
    et.SubElement(data, "session", id=uuid.uuid4().hex)
    et.SubElement(data, "style", id=style, locale="en-US", hasBibliography="1", bibliographyStyleHasBeenSet="0")
    prefs = et.SubElement(data, "prefs")
    for name, val in (("fieldType", "Field"), ("noteType", "0"), ("automaticJournalAbbreviations", "false")):
        et.SubElement(prefs, "pref", name=name, value=val)
    value.text = et.tostring(data, encoding="unicode")
    members["docProps/custom.xml"] = xml_bytes(root)
    ct = "http://schemas.openxmlformats.org/package/2006/content-types"
    types = parse_xml(members["[Content_Types].xml"])
    if not any(x.get("PartName") == "/docProps/custom.xml" for x in types):
        et.SubElement(types, f"{{{ct}}}Override", PartName="/docProps/custom.xml",
                      ContentType="application/vnd.openxmlformats-officedocument.custom-properties+xml")
        members["[Content_Types].xml"] = xml_bytes(types)
    rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    rels = parse_xml(members["_rels/.rels"])
    custom_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties"
    if not any(r.get("Type") == custom_type for r in rels):
        used = {r.get("Id") for r in rels}
        i = 1
        while f"rId{i}" in used:
            i += 1
        et.SubElement(rels, f"{{{rel_ns}}}Relationship", Id=f"rId{i}", Type=custom_type, Target="docProps/custom.xml")
        members["_rels/.rels"] = xml_bytes(rels)


def convert(plan: dict, output: Path, *, replace_bibliography: bool = False) -> dict:
    et = xml_module()
    if output.suffix.lower() != ".docx":
        raise ValueError("Output must be a new .docx file")
    if not isinstance(plan, dict) or not isinstance(plan.get("input"), str):
        raise ValueError("Plan must contain a document input path")
    source = Path(plan["input"])
    if plan.get("ready") is not True or plan.get("issues"):
        raise ValueError("All citations must be resolved before conversion")
    if output.exists() or source.resolve() == output.resolve():
        raise ValueError("Output must be a new file; source is never overwritten")
    if sha256(source) != plan["input_sha256"]:
        raise ValueError("Source changed after planning; prepare again")
    fresh = scan(source, plan.get("references"))
    if any(plan.get(key) != fresh[key] for key in ("groups", "dois", "input_sha256", "existing_bibliography")):
        raise ValueError("Plan citation inventory no longer matches a fresh scan")
    codes = "".join(fresh["existing_fields"])
    if "ZOTERO_" in codes or "EN.CITE" in codes or fresh["unresolved_numeric"]:
        raise ValueError("Plan cannot bypass existing-field or unresolved-citation checks")
    if not isinstance(plan.get("style"), str) or not re.fullmatch(r"https?://www\.zotero\.org/styles/[a-z0-9-]+", plan["style"]):
        raise ValueError("Invalid citation style in plan")
    if not plan["groups"] or not isinstance(plan.get("bindings"), dict) or set(plan["bindings"]) != set(plan["dois"]):
        raise ValueError("Plan bindings must exactly cover its nonempty citation inventory")
    for doi, binding in plan["bindings"].items():
        if not isinstance(binding, dict) or not isinstance(binding.get("itemData"), dict):
            raise ValueError("Invalid plan item binding")
        uri, key, csl = binding.get("uri", ""), binding.get("key", ""), binding["itemData"]
        if (not isinstance(uri, str) or not ITEM_URI.fullmatch(uri) or uri.rsplit("/", 1)[-1] != key
                or binding.get("doi") != doi or csl.get("id") != uri or norm_doi(csl.get("DOI", "")) != doi
                or not csl.get("title") or not csl.get("author") or not csl.get("issued")):
            raise ValueError("Plan DOI, item key, canonical URI and CSL identity must agree")
    root = document_root(source)
    if root.xpath("//w:ins|//w:del|//w:moveFrom|//w:moveTo", namespaces=NS):
        raise ValueError("Tracked revisions require a separately approved clean source")
    paragraphs = root.xpath("/w:document/w:body//w:p", namespaces=NS)
    order = {doi: i + 1 for i, doi in enumerate(plan["dois"])}
    # Validate the entire plan before changing any nodes.
    for group in plan["groups"]:
        if paragraph_text(paragraphs[group["paragraph"]])[group["start"]:group["end"]] != group["original"]:
            raise ValueError("Citation plan offsets no longer match")
        for doi in group["dois"]:
            binding = plan["bindings"][doi]
            if norm_doi(binding["itemData"].get("DOI", "")) != doi:
                raise ValueError("Binding DOI mismatch")
    expected = [paragraph_text(p) for p in paragraphs]
    for group in sorted(plan["groups"], key=lambda g: (g["paragraph"], g["start"]), reverse=True):
        par = paragraphs[group["paragraph"]]
        display = "[" + ",".join(str(order[d]) for d in group["dois"]) + "]"
        payload = {"citationID": uuid.uuid4().hex[:12],
                   "properties": {"formattedCitation": display, "plainCitation": display, "noteIndex": 0},
                   "citationItems": [{"id": plan["bindings"][doi]["uri"],
                                      "uris": [plan["bindings"][doi]["uri"]],
                                      "itemData": plan["bindings"][doi]["itemData"]} for doi in group["dois"]],
                   "schema": SCHEMA}
        props = None
        cursor = 0
        for child in par:
            t = "".join(child.xpath(".//w:t/text()", namespaces=NS))
            if cursor <= group["start"] < cursor + len(t):
                props = child.find(f"{{{W}}}rPr")
                break
            cursor += len(t)
        code = "ADDIN ZOTERO_ITEM CSL_CITATION " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        replace_span(par, group["start"], group["end"], field_runs(code, display, props))
        i = group["paragraph"]
        expected[i] = expected[i][:group["start"]] + display + expected[i][group["end"]:]
    if expected != [paragraph_text(p) for p in paragraphs]:
        raise ValueError("Unexpected text changes outside citation spans")
    body = root.find(f"{{{W}}}body")
    headings = [p for p in list(body) if p.tag == f"{{{W}}}p" and paragraph_text(p).strip()
                in {"参考文献", "References", "Bibliography"}]
    if headings:
        if not replace_bibliography:
            raise ValueError("Existing reference list: review it and explicitly pass --replace-bibliography")
        heading = headings[-1]
        following = list(body)[body.index(heading) + 1:]
        if any(x.tag not in {f"{{{W}}}p", f"{{{W}}}sectPr"} for x in following):
            raise ValueError("Reference-list tail contains non-paragraph content")
        for paragraph in following:
            text = paragraph_text(paragraph).strip()
            styles = paragraph.xpath("./w:pPr/w:pStyle/@w:val", namespaces=NS)
            if any(re.match(r"heading|标题", s, re.I) for s in styles) or re.match(
                    r"^(?:Appendix|Acknowledg|Supplement|附录|致谢)\b", text, re.I):
                raise ValueError("Reference-list tail contains another section; do not replace it automatically")
        for child in following:
            if child.tag != f"{{{W}}}sectPr":
                body.remove(child)
    else:
        heading = et.Element(f"{{{W}}}p")
        heading.append(text_run("参考文献"))
        section = body.find(f"{{{W}}}sectPr")
        body.insert(body.index(section) if section is not None else len(body), heading)
    bib = et.Element(f"{{{W}}}p")
    for run in field_runs('ADDIN ZOTERO_BIBL {"uncited":[],"omitted":[],"custom":[]} CSL_BIBLIOGRAPHY',
                          "请使用 Zotero Refresh 生成参考文献"):
        bib.append(run)
    body.insert(body.index(heading) + 1, bib)
    with ZipFile(source) as z:
        infos = z.infolist()
        members = {i.filename: z.read(i.filename) for i in infos}
    members["word/document.xml"] = xml_bytes(root)
    set_preferences(members, plan["style"])
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as z:
        for info in infos:
            z.writestr(info, members.pop(info.filename))
        for name, raw in members.items():
            z.writestr(name, raw)
    report = _verify_root(root)
    if not report["structural_valid"]:
        raise ValueError("Generated citation fields failed validation: " + ", ".join(report["errors"]))
    if sha256(source) != plan["input_sha256"]:
        raise ValueError("Source changed during conversion; no output was written")
    write_new(output, buffer.getvalue())
    report.update(file=str(output.resolve()), sha256=sha256(output))
    report.update({"source_sha256": plan["input_sha256"], "body_preserved_outside_citations": True,
                   "bibliography_replaced": bool(headings), "plugin_refresh": "required",
                   "scientific_validation": "not_performed"})
    return report


def _verify_root(root) -> dict:
    fields, stack, citations, bibliographies, errors = [], [], [], [], []
    if root.xpath("//w:fldSimple[contains(@w:instr, 'ZOTERO_')]", namespaces=NS):
        errors.append("unsupported_simple_zotero_fields")
    for el in root.iter():
        if el.tag == f"{{{W}}}fldChar":
            kind = el.get(f"{{{W}}}fldCharType")
            if kind == "begin":
                stack.append({"code": "", "display": "", "result": False, "bad_separator": False})
            elif kind == "separate" and stack:
                stack[-1]["bad_separator"] |= stack[-1]["result"]
                stack[-1]["result"] = True
            elif kind == "end":
                if not stack:
                    errors.append("unbalanced_field_end")
                else:
                    fields.append(stack.pop())
        elif stack and el.tag == f"{{{W}}}instrText":
            stack[-1]["code"] += el.text or ""
        elif stack and stack[-1]["result"] and el.tag == f"{{{W}}}t":
            stack[-1]["display"] += el.text or ""
        elif stack and stack[-1]["result"] and el.tag == f"{{{W}}}p":
            stack[-1]["display"] += "\n"
    if stack:
        errors.append("unclosed_fields")
    uris, uri_dois, ids, singles, numeric_groups = {}, {}, set(), {}, []
    for f in fields:
        code = f["code"].strip()
        if "ZOTERO_" in code and (not f["result"] or f["bad_separator"]):
            errors.append("invalid_zotero_field_separator")
        if code.startswith("ADDIN ZOTERO_ITEM CSL_CITATION "):
            try:
                payload = strict_json(code.split("CSL_CITATION ", 1)[1])
                if not isinstance(payload, dict):
                    raise ValueError("Citation payload must be an object")
            except ValueError:
                errors.append("invalid_citation_json")
                continue
            cid = payload.get("citationID")
            if not isinstance(cid, str) or not cid or cid in ids:
                errors.append("duplicate_or_missing_citation_id")
            if isinstance(cid, str):
                ids.add(cid)
            items = payload.get("citationItems", [])
            if not isinstance(items, list) or not items or any(not isinstance(i, dict) for i in items):
                errors.append("empty_citation")
                continue
            dois = []
            for item in items:
                uri_values = item.get("uris")
                data = item.get("itemData")
                if not isinstance(uri_values, list) or not uri_values or not isinstance(data, dict):
                    errors.append("citation_item_identity_missing")
                    continue
                uri, raw_doi = uri_values[0], data.get("DOI", "")
                if not isinstance(uri, str) or not ITEM_URI.fullmatch(uri) or not isinstance(raw_doi, str):
                    errors.append("citation_item_identity_invalid")
                    continue
                doi = norm_doi(raw_doi)
                if not DOI_RE.fullmatch(doi):
                    errors.append("citation_item_identity_invalid")
                    continue
                if doi in uris and uris[doi] != uri:
                    errors.append("same_doi_multiple_uris")
                if uri in uri_dois and uri_dois[uri] != doi:
                    errors.append("same_uri_multiple_dois")
                uris[doi] = uri
                uri_dois[uri] = doi
                dois.append(doi)
            citations.append({"citationID": cid, "display": f["display"],
                              "dois": dois})
            if not f["display"].strip():
                errors.append("empty_citation_display")
            try:
                refs = rendered_numbers(f["display"])
            except ValueError:
                errors.append("invalid_rendered_numbers")
                refs = []
            if refs:
                if len(refs) != len(items) or len(set(refs)) != len(refs):
                    errors.append("number_item_count_mismatch")
                numeric_groups.append((refs, dois))
                # CSL may sort a cluster independently of citationItems order.
                # A single-item citation is an unambiguous association.
                if len(refs) == len(dois) == 1:
                    n, doi = refs[0], dois[0]
                    if n in singles and singles[n] != doi:
                        errors.append("number_points_to_multiple_dois")
                    singles[n] = doi
        elif code.startswith("ADDIN ZOTERO_BIBL "):
            try:
                data = strict_json(code[len("ADDIN ZOTERO_BIBL "):].split(" CSL_BIBLIOGRAPHY", 1)[0])
                if not isinstance(data, dict):
                    raise ValueError("Invalid bibliography payload")
            except ValueError:
                errors.append("invalid_bibliography_json")
            bibliographies.append(f["display"])
    if not citations:
        errors.append("no_zotero_citations")
    if len(bibliographies) != 1:
        errors.append("expected_one_bibliography")
    bibliography = bibliographies[0] if len(bibliographies) == 1 else ""
    pending = not bibliography.strip() or "Zotero Refresh" in bibliography
    entries = re.split(r"\[(\d+)\]", bibliography)
    bibliography_numbers = entries[1::2]
    used_numbers = {n for refs, _ in numeric_groups for n in refs}
    if bibliography_numbers and set(bibliography_numbers) != used_numbers:
        errors.append("citation_bibliography_number_set_mismatch")
    if len(bibliography_numbers) != len(set(bibliography_numbers)):
        errors.append("duplicate_bibliography_numbers")
    bib_map = {}
    for index in range(1, len(entries), 2):
        n, entry = entries[index:index + 2]
        found = {norm_doi(x.rstrip(".,;。；")) for x in DOI_RE.findall(entry)}
        if found:
            if len(found) != 1 or not found.issubset(uris):
                errors.append("bibliography_doi_mismatch:" + n)
            else:
                bib_map[n] = next(iter(found))
    numbers = dict(singles)
    for n, doi in bib_map.items():
        if n in numbers and numbers[n] != doi:
            errors.append("number_bibliography_doi_mismatch:" + n)
        numbers[n] = doi
    if len(set(numbers.values())) != len(numbers):
        errors.append("same_doi_multiple_numbers")
    for refs, dois in numeric_groups:
        if all(n in numbers for n in refs) and {numbers[n] for n in refs} != set(dois):
            errors.append("citation_cluster_bibliography_mismatch")
    complete = (not pending and bool(used_numbers) and len(numeric_groups) == len(citations)
                and set(bib_map) == used_numbers and len(used_numbers) == len(uris))
    return {"citation_groups": len(citations),
            "unique_references": len(uris), "bibliography_fields": len(bibliographies), "errors": errors,
            "structural_valid": not errors, "citations": citations, "doi_to_uri": uris,
            "number_to_doi": numbers, "bibliography_numbers": bibliography_numbers, "bibliography_text": bibliographies,
            "pending_refresh": pending, "numeric_correspondence_verified": complete and not errors,
            "warning": "Field structure does not prove plugin refresh or scientific correctness"}


def verify(path: Path) -> dict:
    result = _verify_root(document_root(path))
    result.update(file=str(path.resolve()), sha256=sha256(path))
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("doctor", help="Probe the read-only local API")
    p.add_argument("--api", default="http://127.0.0.1:23119")
    p = sub.add_parser("snapshot", help="Read Zotero metadata and CSL data; never attachment content")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--api", default="http://127.0.0.1:23119")
    p = sub.add_parser("scan", help="Inspect DOI citations without network access")
    p.add_argument("document", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--references", type=Path, help='Explicit number-to-DOI JSON, e.g. {"1":"10.1000/example"}')
    p = sub.add_parser("prepare", help="Resolve exact DOIs; missing/ambiguous items block conversion")
    p.add_argument("document", type=Path)
    p.add_argument("--snapshot", type=Path, required=True)
    p.add_argument("--selections", type=Path, help="Explicit DOI-to-item-key choices for duplicate records")
    p.add_argument("--references", type=Path)
    p.add_argument("--style", default=DEFAULT_STYLE)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("convert", help="Write a new DOCX with live fields; then run Word plugin refresh")
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--replace-bibliography", action="store_true")
    p = sub.add_parser("verify", help="Inspect field structure and DOI/URI/number relationships offline")
    p.add_argument("document", type=Path)
    p.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            types = LocalAPI(args.api).get("/api/itemTypes")
            if not isinstance(types, list) or not types:
                raise ValueError("Zotero API did not return an item-type list")
            result = {"api": "reachable", "item_types": len(types), "word_plugin": "not_probed"}
        elif args.command == "snapshot":
            rows = LocalAPI(args.api).snapshot()
            write_json_new(args.out, rows)
            result = {"items": len(rows), "out": str(args.out)}
        elif args.command == "scan":
            result = scan(args.document, read_json(args.references) if args.references else None)
            write_json_new(args.out, result)
        elif args.command == "prepare":
            result = prepare(args.document, read_json(args.snapshot),
                             read_json(args.selections) if args.selections else None,
                             read_json(args.references) if args.references else None, args.style)
            write_json_new(args.out, result)
        elif args.command == "convert":
            if args.report.exists():
                raise ValueError("Report already exists")
            result = convert(read_json(args.plan), args.out, replace_bibliography=args.replace_bibliography)
            write_json_new(args.report, result)
        else:
            result = verify(args.document)
            if args.out:
                write_json_new(args.out, result)
        summary = result
        if args.command in {"scan", "prepare", "convert"}:
            summary = {k: result[k] for k in ("input", "file", "ready", "issues", "citation_groups",
                                             "unique_references", "structural_valid", "plugin_refresh") if k in result}
            if "groups" in result:
                summary.update(citation_groups=len(result["groups"]), unique_references=len(result["dois"]))
            summary["report"] = str(args.report if args.command == "convert" else args.out)
        print(json.dumps(summary, ensure_ascii=True, indent=2))
        return 0 if result.get("ready", result.get("structural_valid", True)) else 2
    except (ValueError, OSError, KeyError, BadZipFile) as exc:
        print(f"Zotero citations: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
