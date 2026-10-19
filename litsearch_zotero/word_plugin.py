"""Invoke the installed Zotero Word template through native Word COM.

No shell scripts, injected VBA, redistributed templates, or Office security changes.
Dispatch is asynchronous and is never reported as a completed refresh.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys

from .dynamic_citations import LocalAPI, NS, W, _verify_root, parse_xml, verify, write_json_new

MACROS = {"refresh": "Zotero.ZoteroRefresh", "add-citation": "Zotero.ZoteroAddEditCitation",
          "bibliography": "Zotero.ZoteroAddEditBibliography", "preferences": "Zotero.ZoteroSetDocPrefs"}


def connect_word():
    if sys.platform != "win32":
        raise ValueError("The macro bridge supports Word for Windows only")
    try:
        import win32com.client
    except ImportError as exc:
        raise ValueError('Install the Windows citations extra: pip install ".[citations]"') from exc
    try:
        return win32com.client.GetActiveObject("Word.Application")
    except Exception:
        return win32com.client.Dispatch("Word.Application")


def open_document(word, document: Path):
    path = str(document.resolve())
    for doc in word.Documents:
        if str(doc.FullName).casefold() == path.casefold():
            return doc
    return word.Documents.Open(path)


def live_document_report(doc) -> dict:
    """Validate Word's current in-memory fields before persisting any edits."""
    raw = doc.Content.WordOpenXML
    if not isinstance(raw, str) or len(raw) > 200 * 1024 * 1024:
        raise ValueError("Word did not return a bounded document XML snapshot")
    package = parse_xml(raw.encode("utf-8"))
    if package.tag == f"{{{W}}}document":
        root = package
    else:
        namespace = dict(NS, pkg="http://schemas.microsoft.com/office/2006/xmlPackage")
        documents = package.xpath("//pkg:part[@pkg:name='/word/document.xml']/pkg:xmlData/w:document",
                                  namespaces=namespace)
        if len(documents) != 1:
            raise ValueError("Word XML snapshot lacks the main document")
        root = documents[0]
    return _verify_root(root)


def backup_document(document: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = document.with_name(document.stem + ".before-zotero-" + stamp + ".docx")
    with document.open("rb") as source, backup.open("xb") as destination:
        shutil.copyfileobj(source, destination)
    return backup


def run_plugin(document: Path, action: str, bookmark: str = "", pdf: Path | None = None) -> dict:
    if not document.is_file() or document.suffix.lower() != ".docx":
        raise ValueError("A saved .docx document is required")
    if action not in {*MACROS, "finish"}:
        raise ValueError("Unsupported plugin action")
    if pdf and (pdf.exists() or pdf.suffix.lower() != ".pdf"):
        raise ValueError("PDF output must be a new .pdf file")
    word = connect_word()
    doc = open_document(word, document)
    if doc.ReadOnly:
        raise ValueError("The target document is read-only; no command was sent")
    word.Visible = True
    doc.Activate()
    result = {"action": action, "document": str(document.resolve())}
    if action == "finish":
        bibs = [str(f.Result.Text) for f in doc.Fields if "ADDIN ZOTERO_BIBL" in str(f.Code.Text)]
        if len(bibs) != 1 or not bibs[0].strip() or "Zotero Refresh" in bibs[0]:
            raise ValueError("Bibliography missing or still a placeholder; finish the Zotero dialog first")
        live = live_document_report(doc)
        if not live["structural_valid"] or live["pending_refresh"]:
            raise ValueError("Current Word fields failed validation; no edits were saved")
        backup = backup_document(document)
        doc.Save()
        report = verify(document)
        if not report["structural_valid"]:
            raise ValueError("Saved document failed field structure verification")
        if pdf:
            doc.Repaginate()
            doc.ExportAsFixedFormat(str(pdf.resolve()), 17)
        result.update(status="saved_for_verification", backup=str(backup.resolve()), verification=report)
        return result
    if not doc.Saved:
        raise ValueError("Save existing document edits before sending a plugin command")
    if not any(str(t.Name).casefold() == "zotero.dotm" for t in word.Templates):
        raise ValueError("Zotero.dotm is not loaded; install the Word add-in through Zotero settings")
    if bookmark and not doc.Bookmarks.Exists(bookmark):
        raise ValueError("Requested bookmark does not exist")
    try:
        LocalAPI(timeout=3).get("/api/itemTypes")
    except (OSError, ValueError) as exc:
        raise ValueError("Zotero local API is unavailable; start Zotero and enable its local API before retrying") from exc
    backup = backup_document(document)
    if bookmark:
        doc.Bookmarks.Item(bookmark).Range.Select()
    word.Run(MACROS[action])
    result.update(status="dispatched", backup=str(backup.resolve()),
                  next="Complete any Zotero dialog, then run finish and verify")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=(*MACROS, "finish"))
    parser.add_argument("--document", required=True, type=Path)
    parser.add_argument("--bookmark", default="", help="Existing bookmark; otherwise use the current Word cursor")
    parser.add_argument("--pdf", type=Path, help="New QA PDF exported by finish")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.report and args.report.exists():
            raise ValueError("Report already exists")
        result = run_plugin(args.document, args.action, args.bookmark, args.pdf)
        if args.report:
            write_json_new(args.report, result)
        summary = {k: v for k, v in result.items() if k != "verification"}
        if "verification" in result:
            summary["verification"] = {k: result["verification"][k] for k in (
                "structural_valid", "citation_groups", "unique_references", "bibliography_fields", "errors")}
        print(json.dumps(summary, ensure_ascii=True, indent=2))
        return 0
    except Exception as exc:
        print(f"Zotero Word: {exc}. Inspect Word/Zotero before retrying.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
