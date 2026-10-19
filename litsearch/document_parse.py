"""Offline, provenance-preserving PDF/HTML/image extraction.

Nothing in this module interprets scientific quantities or approves evidence.
HTML spans are explicit; PDF spans and OCR rows are geometric observations.
CLI parsing runs in an isolated process with a wall-clock deadline. Optional
dependencies are imported only by the adapter which needs them.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import io
import json
import math
import multiprocessing
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

_IN_WORKER = False
_HTML_BREAK_TAGS = {"address", "article", "aside", "blockquote", "dd", "div", "dl", "dt",
                    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
                    "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
                    "section", "table", "tr", "ul"}


class ParseError(ValueError):
    """Invalid input, missing dependency, or exceeded resource limit."""


@dataclass
class Options:
    ocr: str = "off"
    pages: str = ""
    dpi: int = 200
    language: str = "eng"
    tesseract: str = "tesseract"
    ocr_timeout: float = 30.0
    max_seconds: float = 120.0
    max_input_bytes: int = 50_000_000
    max_output_bytes: int = 20_000_000
    max_pages: int = 200
    max_cells: int = 100_000
    max_span: int = 256
    max_pixels: int = 40_000_000
    min_native_chars: int = 20
    min_ocr_confidence: float = 70.0
    table_strategy: str = "lines"

    def validate(self) -> None:
        if self.ocr not in {"off", "auto", "always"}:
            raise ParseError("ocr must be off, auto or always")
        if self.table_strategy not in {"lines", "text"}:
            raise ParseError("table strategy must be lines or text")
        if not 72 <= self.dpi <= 600:
            raise ParseError("dpi must be between 72 and 600")
        if not re.fullmatch(r"[A-Za-z0-9_]+(?:\+[A-Za-z0-9_]+)*", self.language):
            raise ParseError("language must be a Tesseract language code, e.g. eng or eng+chi_sim")
        for name in ("ocr_timeout", "max_seconds", "max_input_bytes", "max_output_bytes",
                     "max_pages", "max_cells", "max_span", "max_pixels"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ParseError(f"{name} must be finite and positive")
        if self.min_native_chars < 0 or not 0 <= self.min_ocr_confidence <= 100:
            raise ParseError("invalid native-character threshold or OCR confidence threshold")


def _dependency(name: str, extra: str):
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise ParseError(f"Missing {name}; install pip install 'd-pan-literature-pipeline[{extra}]'") from exc


def _check(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise ParseError("Document parsing exceeded max_seconds")


def _stop_tree(pid: int, fallback, *, process_group: bool = True) -> None:
    """Stop a known group leader/tree, or kill an exact nonleader process.

    OCR engines inside a worker inherit its session and are not group leaders.
    Never assume their PID names a group; the parent cleans the worker group.
    """
    stopped = False
    if os.name == "nt":
        try:
            result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    shell=False, timeout=5, check=False)
            stopped = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    elif process_group:
        try:
            os.killpg(pid, signal.SIGKILL)
            stopped = True
        except OSError:
            pass
    if not stopped:
        try:
            fallback()
        except ProcessLookupError:
            pass  # The exact process exited between observation and cleanup.


def _fresh_output(path: Path) -> None:
    """Reject symlink/junction parents before creating a new output tree."""
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ParseError("Output path contains a symlink/junction/reparse point")
    if path.exists():
        raise ParseError("Output directory already exists; choose a fresh path (no overwrites)")


def selected_pages(spec: str, total: int, maximum: int) -> list[int]:
    """Return 1-based pages, never silently truncate or accept invalid pages."""
    if not spec:
        if total > maximum:
            raise ParseError(f"Document has {total} pages; select pages or raise --max-pages")
        return list(range(1, total + 1))
    pages: set[int] = set()
    for token in spec.split(","):
        match = re.fullmatch(r"\s*(\d+)(?:-(\d+))?\s*", token)
        if not match:
            raise ParseError("pages must be 1-based comma-separated pages/ranges, e.g. 1,3-5")
        first, last = int(match[1]), int(match[2] or match[1])
        if first < 1 or last < first or last > total or last - first + 1 > maximum:
            raise ParseError(f"Invalid or excessive page range: {token}")
        pages.update(range(first, last + 1))
        if len(pages) > maximum:
            raise ParseError("Selected page count exceeds max_pages")
    return sorted(pages)


def _units(text: str) -> list[str]:
    # Only label candidates, retain spelling, never transform numbers/units.
    return list(dict.fromkeys(re.findall(r"\([^()\n]{1,80}\)|\[[^\[\]\n]{1,80}\]", text)))


def _table(table_id: str, page: int, method: str) -> dict[str, Any]:
    return {"id": table_id, "page": page, "method": method, "status": "unresolved",
            "cells": [], "raw_matrix": [], "grid": [], "header_rows": [],
            "header_status": "unresolved", "caption": None, "notes": [],
            "unit_candidates": [], "continuation": {"status": "unresolved", "candidate_ids": []}}


class _TablesHTML(HTMLParser):
    """Bounded explicit HTML table expansion; no network/XML/entity resolver."""
    def __init__(self, source: str, opts: Options, deadline: float):
        super().__init__(convert_charrefs=True)
        self.source, self.opts, self.deadline = source, opts, deadline
        self.tables: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self.cell: dict[str, Any] | None = None
        self.rows: list[list[dict[str, Any]]] = []
        self.row: list[dict[str, Any]] | None = None
        self.section = "body"
        self.caption = False
        self.table_depth = 0
        self.ignore_depth = 0
        self.page_text: list[str] = []
        self.after: list[str] = []
        self.total_cells = 0
        self.total_grid_cells = 0
        self.structure: list[str] = []
        self.line_offsets = [0]
        for match in re.finditer("\n", source):
            self.line_offsets.append(match.end())

    def _offset(self) -> int:
        line, column = self.getpos()
        return self.line_offsets[line - 1] + column

    def _span(self, attrs: dict, name: str) -> int:
        raw = attrs.get(name, "1")
        if not re.fullmatch(r"[1-9]\d{0,5}", raw or ""):
            raise ParseError(f"Invalid HTML {name}: {raw!r}; zero/negative/noninteger spans need manual review")
        value = int(raw)
        if value > self.opts.max_span:
            raise ParseError(f"HTML {name} exceeds max_span")
        return value

    def handle_starttag(self, tag: str, attrs: list) -> None:
        _check(self.deadline)
        parents = {"table": {None}, "thead": {"table"}, "tbody": {"table"},
                   "tfoot": {"table"}, "caption": {"table"},
                   "tr": {"table", "thead", "tbody", "tfoot"}, "td": {"tr"}, "th": {"tr"}}
        if tag in parents:
            parent = self.structure[-1] if self.structure else None
            if parent not in parents[tag]:
                raise ParseError(f"Invalid/unclosed HTML table structure before <{tag}>")
            self.structure.append(tag)
        if tag == "br" or tag in _HTML_BREAK_TAGS or tag in {"td", "th"}:
            self._boundary(force=tag == "br")
        if tag in {"script", "style"}:
            self.ignore_depth += 1
        if tag == "table":
            if self.current is not None:
                raise ParseError("Nested HTML tables require manual extraction; not flattened")
            if self.tables and self.after:
                self.tables[-1]["notes"].append({"raw_text": "".join(self.after),
                                               "association": "following_context_candidate"})
            self.after = []
            self.current = _table(f"table-{len(self.tables) + 1}", 1, "html_explicit_spans")
            self.current["source_start"] = self._offset()
            self.current["source_line"] = self.getpos()[0]
            self.rows = []
            self.section = "body"
        elif self.current is not None:
            if tag in {"thead", "tbody", "tfoot"}:
                self.section = tag
            elif tag == "caption":
                self.caption = True
                self.current["caption"] = {"raw_text": "", "association": "explicit_caption"}
            elif tag == "tr":
                if self.row is not None:
                    raise ParseError("Unclosed/nested HTML row")
                self.row = []
                self.rows.append(self.row)
                if len(self.rows) > self.opts.max_cells:
                    raise ParseError("HTML row count exceeds max_cells")
            elif tag in {"td", "th"}:
                if self.row is None or self.cell is not None:
                    raise ParseError("HTML cell outside row or unclosed cell")
                attributes = dict(attrs)
                self.cell = {"raw_text": "", "rowspan": self._span(attributes, "rowspan"),
                             "colspan": self._span(attributes, "colspan"), "kind": tag,
                             "section": self.section, "source_line": self.getpos()[0],
                             "source_start": self._offset(), "attributes": attributes}
                self.row.append(self.cell)
                self.total_cells += 1
                if self.total_cells > self.opts.max_cells:
                    raise ParseError("HTML cell count exceeds max_cells")

    def _boundary(self, *, force: bool = False) -> None:
        """Retain rendered breaks explicitly, without silently joining numbers."""
        if self.ignore_depth:
            return
        if force or (self.page_text and not self.page_text[-1].endswith("\n")):
            self.page_text.append("\n")
        target = self.cell
        if target is None and self.caption and self.current is not None:
            target = self.current["caption"]
        if target is not None:
            value = target["raw_text"]
            if force or (value and not value.endswith("\n")):
                target["raw_text"] += "\n"
        elif self.current is None and self.tables and (force or (self.after and not self.after[-1].endswith("\n"))):
            self.after.append("\n")

    def handle_endtag(self, tag: str) -> None:
        structural = {"table", "thead", "tbody", "tfoot", "caption", "tr", "td", "th"}
        if tag in structural:
            if not self.structure or self.structure[-1] != tag:
                raise ParseError(f"Mismatched HTML table closing tag </{tag}>")
            self.structure.pop()
        if tag in _HTML_BREAK_TAGS:
            self._boundary()
        if tag in {"script", "style"}:
            self.ignore_depth = max(0, self.ignore_depth - 1)
        if self.current is None:
            return
        if tag in {"td", "th"} and self.cell is not None:
            self.cell["raw_html"] = self.source[self.cell["source_start"]:self.source.find(">", self._offset()) + 1]
            self.cell = None
        elif tag == "tr":
            if self.cell is not None:
                raise ParseError("Unclosed HTML cell")
            self.row = None
        elif tag == "caption":
            self.caption = False
        elif tag in {"thead", "tbody", "tfoot"}:
            self.section = "body"
        elif tag == "table":
            if self.row is not None or self.cell is not None:
                raise ParseError("Unclosed HTML row/cell")
            self._finish()

    def handle_data(self, data: str) -> None:
        if self.ignore_depth:
            return
        self.page_text.append(data)
        if self.cell is not None:
            self.cell["raw_text"] += data
        elif self.caption and self.current is not None:
            self.current["caption"]["raw_text"] += data
        elif self.current is None and self.tables:
            self.after.append(data)

    def _finish(self) -> None:
        table = self.current
        assert table is not None
        table["raw_html"] = self.source[table["source_start"]:self.source.find(">", self._offset()) + 1]
        occupied: dict[tuple[int, int], str] = {}
        anchors: dict[tuple[int, int], str] = {}
        for r, row in enumerate(self.rows):
            col = 0
            if any(cell["section"] == "thead" or cell["attributes"].get("scope") in {"col", "colgroup"}
                   for cell in row) or (row and all(cell["kind"] == "th" and
                   cell["attributes"].get("scope") not in {"row", "rowgroup"} for cell in row)):
                table["header_rows"].append(r)
            for cell in row:
                while (r, col) in occupied:
                    col += 1
                if r + cell["rowspan"] > len(self.rows):
                    raise ParseError("HTML rowspan extends beyond explicit rows")
                count = cell["rowspan"] * cell["colspan"]
                if len(occupied) + count > self.opts.max_cells:
                    raise ParseError("Expanded HTML grid exceeds max_cells")
                cell_id = f"{table['id']}-cell-{len(table['cells']) + 1}"
                for rr in range(r, r + cell["rowspan"]):
                    for cc in range(col, col + cell["colspan"]):
                        if (rr, cc) in occupied:
                            raise ParseError("Overlapping HTML spans; unresolved layout")
                        occupied[rr, cc] = cell_id
                cell.update({"id": cell_id, "row": r, "column": col, "bbox": None,
                             "coordinates": "html_grid", "span_evidence": "explicit_attributes",
                             "text_reconstruction": "Decoded HTML text with explicit br/block boundaries as newlines; raw_html retained"})
                anchors[r, col] = cell["raw_text"]
                table["cells"].append(cell)
                if cell["section"] == "tfoot":
                    table["notes"].append({"raw_text": cell["raw_text"], "cell_id": cell_id,
                                           "association": "explicit_tfoot"})
                col += cell["colspan"]
        width = max((c + 1 for _, c in occupied), default=0)
        if len(self.rows) * width > self.opts.max_cells:
            raise ParseError("Rectangular HTML matrix exceeds max_cells")
        self.total_grid_cells += len(self.rows) * width
        if self.total_grid_cells > self.opts.max_cells:
            raise ParseError("Document HTML grid count exceeds max_cells")
        table["grid"] = [[occupied.get((r, c)) for c in range(width)] for r in range(len(self.rows))]
        table["raw_matrix"] = [[anchors.get((r, c)) for c in range(width)] for r in range(len(self.rows))]
        table["header_status"] = "explicit_markup" if table["header_rows"] else "unresolved"
        table["unit_candidates"] = _units(" ".join(c["raw_text"] for c in table["cells"]))
        table["status"] = "extracted_pending_review"
        self.tables.append(table)
        self.current = None

    def finish(self) -> None:
        self.close()
        if self.current is not None:
            raise ParseError("Unclosed HTML table")
        if self.tables and self.after:
            self.tables[-1]["notes"].append({"raw_text": "".join(self.after),
                                           "association": "following_context_candidate"})


def _raw_pdf_word_lines(page: Any) -> list[dict]:
    words = sorted(page.extract_words() or [], key=lambda word: (float(word["top"]), float(word["x0"])))
    lines: list[dict] = []
    for word in words:
        top = float(word["top"])
        match = next((line for line in reversed(lines) if abs(line["top"] - top) <= 2.5), None)
        if match is None:
            match = {"top": top, "words": []}
            lines.append(match)
        match["top"] = min(match["top"], top)
        match["words"].append(word)
    for line in lines:
        line["words"].sort(key=lambda word: float(word["x0"]))
        line["x0"] = min(float(word["x0"]) for word in line["words"])
        line["x1"] = max(float(word["x1"]) for word in line["words"])
    return sorted(lines, key=lambda line: (line["top"], line["x0"]))


def _pdf_word_lines(page: Any) -> list[dict]:
    """Reconstruct lines while keeping wide cross-column headings intact."""
    gutter = _pdf_column_gutter(page)
    result: list[dict] = []
    for raw in _raw_pdf_word_lines(page):
        words = raw["words"]
        groups = [words]
        if gutter:
            left = [word for word in words if float(word["x1"]) <= gutter[0] + 1]
            right = [word for word in words if float(word["x0"]) >= gutter[1] - 1]
            crosses_gutter = any(float(word["x0"]) < gutter[1] - 1
                                 and float(word["x1"]) > gutter[0] + 1 for word in words)
            if left and right and not crosses_gutter:
                groups = [left, right]
        for group in groups:
            result.append({"top": raw["top"], "x0": min(float(word["x0"]) for word in group),
                           "x1": max(float(word["x1"]) for word in group),
                           "bottom": max(float(word["bottom"]) for word in group),
                           "text": " ".join(str(word["text"]) for word in group)})
    return sorted(result, key=lambda line: (line["top"], line["x0"]))


def _pdf_column_gutter(page: Any) -> tuple[float, float] | None:
    """Require repeated, prose-like local evidence; table column gaps do not count."""
    minimum = max(18.0, float(page.width) * 0.025)
    gap_rows: list[dict] = []
    for line in _raw_pdf_word_lines(page):
        words = [word for word in line["words"]
                 if not (float(word["top"]) >= page.height - 50
                         and re.fullmatch(r"\d+", str(word["text"]).strip()))]
        if len(words) < 6:
            continue
        x0 = min(float(word["x0"]) for word in words)
        x1 = max(float(word["x1"]) for word in words)
        # A spanning title/header may bridge the center; it is not evidence
        # that the body below is single-column.
        if x1 - x0 < page.width * 0.55:
            continue
        for left_word, right_word in zip(words, words[1:]):
            left, right = float(left_word["x1"]), float(right_word["x0"])
            center = (left + right) / 2
            if (right - left < minimum
                    or not 0.34 * page.width <= center <= 0.66 * page.width):
                continue
            left_words = [word for word in words if float(word["x1"]) <= left + 1]
            right_words = [word for word in words if float(word["x0"]) >= right - 1]
            if len(left_words) < 3 or len(right_words) < 3:
                continue
            gap_rows.append({"top": line["top"], "left": left, "right": right,
                             "center": center})
    best: list[dict] = []
    for row in gap_rows:
        group = [candidate for candidate in gap_rows
                 if abs(candidate["center"] - row["center"]) <= max(10.0, page.width * 0.02)]
        if len({round(candidate["top"], 1) for candidate in group}) > len(
                {round(candidate["top"], 1) for candidate in best}):
            best = group
    tops = sorted({float(row["top"]) for row in best})
    if len(tops) < 3 or tops[-1] - tops[0] < 35:
        return None
    return (sorted(row["left"] for row in best)[len(best) // 2],
            sorted(row["right"] for row in best)[len(best) // 2])


_PDF_CAPTION_LABEL = re.compile(r"^\s*(?:supplementary\s+)?tables?\s+[A-Za-z]?\d+[A-Za-z]?\b", re.I)


def _pdf_caption_indices(lines: list[dict], page: Any) -> list[int]:
    indices = []
    for index, line in enumerate(lines):
        match = _PDF_CAPTION_LABEL.match(line["text"])
        if not match or line["top"] >= page.height * 0.9:
            continue
        remainder = line["text"][match.end():].strip()
        if remainder:
            # Captions use a visible label separator or a title-like initial;
            # lowercase sentence continuations such as “Table 1 shows …” do not.
            title_start = re.match(r"^[.:：–—-]\s*", remainder) or remainder[:1].isupper()
            if not title_start:
                continue
        else:
            next_line = next((candidate for candidate in lines[index + 1:]
                              if 0 < candidate["top"] - line["top"] <= 18
                              and abs(candidate["x0"] - line["x0"]) <= 24), None)
            if next_line is None or not next_line["text"][:1].isupper():
                continue
        indices.append(index)
    return indices


def _pdf_text_candidates(page: Any, settings: dict) -> list[Any]:
    """Find tables in local crops; do not silently truncate at a fixed height."""
    lines = _pdf_word_lines(page)
    caption_indices = _pdf_caption_indices(lines, page)
    gutter = _pdf_column_gutter(page)
    candidates: list[Any] = []
    headings = [line for line in lines
                if re.match(r"^\s*\d+(?:\.\d+)*\.?\s+[A-Z]", line["text"])]
    caption_lines = [lines[index] for index in caption_indices]
    for caption in caption_lines:
        if gutter and caption["x1"] - caption["x0"] < page.width * 0.72:
            if caption["x1"] <= gutter[0] + 8:
                x0, x1 = 0.0, gutter[0] + 8
            elif caption["x0"] >= gutter[1] - 8:
                x0, x1 = gutter[1] + 3, float(page.width)
            else:
                x0, x1 = 0.0, float(page.width)
        else:
            x0, x1 = 0.0, float(page.width)
        boundaries = [line["top"] for line in [*headings, *caption_lines]
                      if line["top"] > caption["top"] + 4 and x0 - 8 <= line["x0"] <= x1 + 8]
        # Begin below the caption glyphs so a text-grid finder cannot treat
        # caption words as an extra row (or split them into phantom columns).
        y0 = max(0.0, float(caption.get("bottom", caption["top"] + 12)) + 1.0)
        # Use an actual heading/caption boundary when present; otherwise expose
        # the page remainder to the structure gate rather than clipping rows.
        y1 = min(boundaries) - 2.0 if boundaries else float(page.height)
        if y1 <= y0 + 12:
            continue
        try:
            candidates.extend(page.crop((x0, y0, x1, y1), strict=False).find_tables(settings))
        except Exception:
            continue
    if not candidates and gutter:
        for x0, x1 in ((0.0, gutter[0] - 3), (gutter[1] + 3, float(page.width))):
            try:
                candidates.extend(page.crop((x0, 0.0, x1, float(page.height)), strict=False).find_tables(settings))
            except Exception:
                continue
    if not candidates:
        candidates = page.find_tables(settings)
    unique = []
    seen = set()
    for found in candidates:
        key = tuple(round(float(value), 1) for value in found.bbox)
        if key not in seen:
            unique.append(found)
            seen.add(key)
    return unique


def _pdf_neighbor_text(page: Any, bbox: tuple[float, float, float, float], *, above: bool) -> str:
    x0, top, x1, bottom = bbox
    edge = top if above else bottom
    distance = 35.0 if above else 32.0
    lines = _pdf_word_lines(page)
    accepted_captions = {(round(lines[index]["top"], 1), round(lines[index]["x0"], 1),
                          lines[index]["text"])
                         for index in _pdf_caption_indices(lines, page)}
    selected = []
    for line in lines:
        if line["x1"] < x0 - 4 or line["x0"] > x1 + 4:
            continue
        if line["x0"] < x0 - 12 or line["x1"] > x1 + 12:
            continue
        if above:
            # Text-based grid finders can absorb the caption baseline into the
            # candidate bbox. Let an anchored caption sit on the top edge, but
            # never associate arbitrary in-table header words as a caption.
            starts_caption = (round(line["top"], 1), round(line["x0"], 1), line["text"]) in accepted_captions
            if edge - distance <= line["top"] < edge or (line["top"] <= edge + 3 and starts_caption):
                selected.append(line)
        elif edge < line["top"] <= edge + distance:
            if re.match(r"\s*(?:notes?\b|[*†‡§])", line["text"], re.I):
                selected.append(line)
    if above:
        marker = next((i for i, line in enumerate(selected)
                       if _PDF_CAPTION_LABEL.match(line["text"])), None)
        if marker is None:
            return ""
        selected = selected[marker:]
    return "\n".join(line["text"] for line in selected)


def _untrusted_pdf_table(table_id: str, number: int, bbox: Any, found: Any,
                         reason: str) -> dict:
    table = _table(table_id, number, "pdfplumber_geometry")
    matrix = found.extract() or []
    nonempty_rows = [row for row in matrix if any(str(value or "").strip() for value in row)]
    table.update({"status": "untrusted", "extraction_status": "not_reliably_extracted",
                  "bbox": list(bbox), "raw_matrix": [], "grid": [], "cells": [],
                  "candidate_shape": [len(nonempty_rows),
                                      max((len(row) for row in nonempty_rows), default=0)],
                  "failure_reason": reason,
                  "limitations": ["Candidate table geometry was rejected; no cell values are exposed as extracted data"]})
    return table


def _pdf_text_extension(page: Any, bbox: tuple[float, float, float, float],
                        x_edges: list[float]) -> tuple[list[str], float] | None:
    """Recover a compact final row omitted by pdfplumber's text-grid boundary."""
    if len(x_edges) < 3:
        return None
    bottom = bbox[3]
    words = [word for word in (page.extract_words() or [])
             if bottom - 1 <= float(word["top"]) <= bottom + 18
             and x_edges[0] - 2 <= float(word["x0"])
             and float(word["x1"]) <= x_edges[-1] + 2]
    lines: list[dict] = []
    for word in sorted(words, key=lambda item: (float(item["top"]), float(item["x0"]))):
        top = float(word["top"])
        line = next((candidate for candidate in reversed(lines)
                     if abs(candidate["top"] - top) <= 2.5), None)
        if line is None:
            line = {"top": top, "bottom": float(word["bottom"]),
                    "words": [[] for _ in range(len(x_edges) - 1)]}
            lines.append(line)
        line["bottom"] = max(line["bottom"], float(word["bottom"]))
        center = (float(word["x0"]) + float(word["x1"])) / 2
        column = next((i for i, (left, right) in enumerate(zip(x_edges, x_edges[1:]))
                       if left - 1 <= center < right + (1 if i == len(x_edges) - 2 else 0)), None)
        if column is not None:
            line["words"][column].append((float(word["x0"]), str(word["text"])))
    rows = []
    for line in lines:
        row = [" ".join(word for _, word in sorted(group)).strip() for group in line["words"]]
        if sum(bool(value) for value in row) >= 2 and row[0] and row[-1]:
            rows.append((line["top"], row))
    if not rows:
        return None
    # A continuation line can add wrapped text to a row only when it remains
    # in the middle cell and is close to the multi-column row above it.
    top, row = rows[0]
    anchor = next(line for line in lines if line["top"] == top)
    extension_bottom = anchor["bottom"]
    for line in lines:
        if not top < line["top"] <= top + 10:
            continue
        groups = line["words"]
        if len(groups) > 2 and not groups[0] and not groups[-1] and any(groups[1:-1]):
            row[len(row) // 2] += "\n" + " ".join(
                word for group in groups[1:-1] for _, word in sorted(group)).strip()
            extension_bottom = max(extension_bottom, line["bottom"])
    return row, extension_bottom


def _pdf_text_structure_failure(page: Any, bbox: tuple[float, float, float, float],
                                found: Any, matrix: list[list[Any]]) -> str | None:
    """Require local anchors and repeated geometric row/column evidence.

    Text-grid detection can turn paragraph whitespace into columns. Grammar is
    not used as a proxy: candidate rows must form a stable table-like grid and
    be locally anchored by an accepted caption or a distinct header row.
    """
    rows = [[str(value or "").strip() for value in row] for row in matrix]
    nonempty_indexes = [index for index, row in enumerate(rows) if any(row)]
    if len(nonempty_indexes) < 4:
        return "fewer_than_three_repeated_data_rows"
    header_index = nonempty_indexes[0]
    header = rows[header_index]
    columns = max(map(len, rows), default=0)
    header_cells = [header[column] if column < len(header) else ""
                    for column in range(columns)]
    if sum(bool(value) for value in header_cells) < 2:
        return "missing_distinct_header_or_caption_anchor"
    data_indexes = [index for index in nonempty_indexes if index > header_index
                    and sum(bool(value) for value in rows[index]) >= 2]
    if len(data_indexes) < 3:
        return "fewer_than_three_repeated_data_rows"

    caption = _pdf_neighbor_text(page, bbox, above=True)
    if not caption:
        # An uncaptioned candidate needs a compact, label-like header whose
        # values differ structurally from the repeated data beneath it.
        label_cells = [value for value in header_cells if value]
        label_like = sum(
            len(re.findall(r"[\w’-]+", value, re.UNICODE)) <= 3
            and value[:1].isupper()
            and not re.search(r"[.!?][\"'’)]?$", value)
            for value in label_cells
        )
        data_token_counts = [
            sum(len(re.findall(r"[\w’-]+", str(value or ""), re.UNICODE))
                for value in rows[index])
            for index in data_indexes
        ]
        data_token_counts.sort()
        median_data_tokens = data_token_counts[len(data_token_counts) // 2]
        header_tokens = sum(len(re.findall(r"[\w’-]+", value, re.UNICODE))
                            for value in label_cells)
        if (label_like < 2 or header_tokens >= median_data_tokens
                or not any(len(re.findall(r"[\w’-]+", value, re.UNICODE)) == 1
                           for value in label_cells)):
            return "missing_distinct_header_or_caption_anchor"

    boxes = [tuple(float(value) for value in box) for box in (found.cells or []) if box is not None]
    x_edges = sorted({value for box in boxes for value in (box[0], box[2])})
    y_edges = sorted({value for box in boxes for value in (box[1], box[3])})
    if len(x_edges) != columns + 1 or len(y_edges) != len(rows) + 1:
        return "unstable_geometric_row_or_column_grid"

    words = [word for word in (page.extract_words() or [])
             if bbox[1] - 2 <= float(word["top"]) <= bbox[3] + 2
             and bbox[0] - 2 <= float(word["x0"]) <= bbox[2] + 2]
    anchors: list[list[tuple[float, float]]] = [[] for _ in range(columns)]
    column_gaps: list[list[float]] = [[] for _ in range(columns - 1)]
    ordinary_gaps: list[float] = []
    for row_index in data_indexes:
        top, bottom = y_edges[row_index], y_edges[row_index + 1]
        row_words = [word for word in words
                     if top - 2 <= float(word["top"]) <= bottom + 2]
        by_column: list[list[Any]] = [[] for _ in range(columns)]
        for word in row_words:
            center = (float(word["x0"]) + float(word["x1"])) / 2
            column = next((index for index in range(columns)
                           if x_edges[index] - 1 <= center
                           and (center < x_edges[index + 1]
                                or index == columns - 1 and center <= x_edges[index + 1] + 1)), None)
            if column is not None:
                by_column[column].append(word)
        for column, cell_words in enumerate(by_column):
            cell_words.sort(key=lambda word: float(word["x0"]))
            if not cell_words:
                continue
            anchors[column].append((float(cell_words[0]["x0"]), float(cell_words[-1]["x1"])))
            for left, right in zip(cell_words, cell_words[1:]):
                gap = float(right["x0"]) - float(left["x1"])
                if 0 <= gap <= 12:
                    ordinary_gaps.append(gap)
        for column in range(columns - 1):
            if by_column[column] and by_column[column + 1]:
                right_edge = max(float(word["x1"]) for word in by_column[column])
                left_edge = min(float(word["x0"]) for word in by_column[column + 1])
                column_gaps[column].append(left_edge - right_edge)

    if any(len(values) < 3 for values in anchors):
        return "columns_lack_repeated_text_anchors"
    for values in anchors:
        left_positions = [value[0] for value in values]
        right_positions = [value[1] for value in values]
        if min(max(left_positions) - min(left_positions),
               max(right_positions) - min(right_positions)) > 14:
            return "columns_lack_stable_alignment_anchors"

    spacing = sorted(ordinary_gaps)
    typical_word_gap = spacing[len(spacing) // 2] if spacing else 2.0
    significant_gap = max(5.0, typical_word_gap * 2.0)
    if any(sum(gap >= significant_gap for gap in values) < 2 for values in column_gaps):
        return "columns_lack_repeated_whitespace_separators"
    return None


def _pdf_ambiguous_numeric_column(matrix: list[list[Any]]) -> bool:
    """Reject a text grid that split an unlabelled numeric suffix into a column."""
    rows = [[str(value or "").strip() for value in row] for row in matrix]
    rows = [row for row in rows if any(row)]
    if len(rows) < 6:
        return False
    columns = max(map(len, rows), default=0)
    for column in range(columns):
        if column < len(rows[0]) and rows[0][column]:
            continue
        values = [row[column] for row in rows[1:]
                  if column < len(row) and row[column]]
        if len(values) < 5 or len(values) < 0.7 * (len(rows) - 1):
            continue
        if all(re.fullmatch(r"[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)", value) for value in values):
            return True
    return False


def _pdf_text_grid_splits_words(page: Any, bbox: tuple[float, float, float, float],
                                found: Any) -> bool:
    """Flag text-grid boundaries that cut through original PDF words."""
    boxes = [tuple(float(value) for value in box) for box in (found.cells or []) if box is not None]
    boundaries = {round(x, 2) for box in boxes for x in (box[0], box[2])
                  if bbox[0] + 2 < x < bbox[2] - 2}
    if not boundaries:
        return False
    split_words = 0
    for word in page.extract_words() or []:
        if not (bbox[1] - 2 <= float(word["top"]) <= bbox[3] + 2):
            continue
        if any(float(word["x0"]) + 0.5 < edge < float(word["x1"]) - 0.5
               for edge in boundaries):
            split_words += 1
    return split_words >= 2


def _pdf_table(page: Any, found: Any, table_id: str, number: int, opts: Options,
               *, text_strategy: bool = False) -> dict:
    try:
        bbox = tuple(float(value) for value in found.bbox)
        if (len(bbox) != 4 or not all(math.isfinite(value) for value in bbox)
                or bbox[0] < -1 or bbox[1] < -1 or bbox[2] > page.width + 1
                or bbox[3] > page.height + 1 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]):
            return _untrusted_pdf_table(table_id, number, found.bbox, found,
                                        "invalid_or_out_of_page_geometry")
        extractor_matrix = found.extract() or []
        nonempty = [[str(value or "").strip() for value in row] for row in extractor_matrix]
        nonempty_rows = [row for row in nonempty if any(row)]
        columns = max((len(row) for row in nonempty), default=0)
        populated_columns = {column for row in nonempty for column, value in enumerate(row) if value}
        nonempty_cell_count = sum(bool(value) for row in nonempty for value in row)
        if columns < 2 or len(populated_columns) < 2 or nonempty_cell_count < 2:
            return _untrusted_pdf_table(table_id, number, bbox, found,
                                        "insufficient_repeated_column_structure")
        if text_strategy:
            structure_failure = _pdf_text_structure_failure(page, bbox, found, extractor_matrix)
            if structure_failure:
                return _untrusted_pdf_table(table_id, number, bbox, found,
                                            structure_failure)
            if _pdf_text_grid_splits_words(page, bbox, found):
                return _untrusted_pdf_table(table_id, number, bbox, found,
                                            "text_grid_boundary_splits_original_words")
            if _pdf_ambiguous_numeric_column(extractor_matrix):
                return _untrusted_pdf_table(table_id, number, bbox, found,
                                            "ambiguous_text_grid_column_segmentation")
            if bbox[3] >= page.height - 5:
                return _untrusted_pdf_table(table_id, number, bbox, found,
                                            "candidate_reaches_page_boundary_without_structural_end")
            gutter = _pdf_column_gutter(page)
            if gutter and bbox[0] < gutter[0] - 3 and bbox[2] > gutter[1] + 3:
                return _untrusted_pdf_table(table_id, number, bbox, found,
                                            "candidate_crosses_two_column_gutter")
            large_full_page_grid = (bbox[2] - bbox[0] >= page.width * 0.78
                                    and bbox[3] - bbox[1] >= page.height * 0.72)
            if large_full_page_grid and not _pdf_neighbor_text(page, bbox, above=True):
                return _untrusted_pdf_table(table_id, number, bbox, found,
                                            "unanchored_full_page_text_grid")
    except Exception:
        return _untrusted_pdf_table(table_id, number, getattr(found, "bbox", []), found,
                                    "candidate_geometry_unavailable")

    table = _table(table_id, number, "pdfplumber_text_geometry" if text_strategy else "pdfplumber_geometry")
    boxes = sorted(set(tuple(float(v) for v in box) for box in found.cells if box is not None))
    xs = sorted({v for box in boxes for v in (box[0], box[2])})
    ys = sorted({v for box in boxes for v in (box[1], box[3])})
    text_matrix = [[str(value or "") for value in row] for row in extractor_matrix]
    if text_strategy:
        extension = _pdf_text_extension(page, bbox, xs)
        if extension:
            extra_row, extra_bottom = extension
            if extra_bottom > bbox[3] + 1:
                top = bbox[3]
                bottom = min(float(page.height), extra_bottom + 2)
                if bottom > top:
                    boxes.extend((xs[column], top, xs[column + 1], bottom)
                                 for column in range(len(xs) - 1))
                    boxes = sorted(set(boxes))
                    text_matrix.append(extra_row)
                    bbox = (bbox[0], bbox[1], bbox[2], bottom)
                    ys = sorted({v for box in boxes for v in (box[1], box[3])})
    if len(boxes) > opts.max_cells or max(0, len(xs) - 1) * max(0, len(ys) - 1) > opts.max_cells:
        raise ParseError("PDF table grid exceeds max_cells")
    rows, columns = len(ys) - 1, len(xs) - 1
    matrix = [[None] * columns for _ in range(rows)]
    grid = [[None] * columns for _ in range(rows)]
    for box in boxes:
        r, c = ys.index(box[1]), xs.index(box[0])
        rs, cs = ys.index(box[3]) - r, xs.index(box[2]) - c
        chars = [char for char in page.chars if box[0] <= (char["x0"] + char["x1"]) / 2 < box[2]
                 and box[1] <= (char["top"] + char["bottom"]) / 2 < box[3]]
        extracted = (text_matrix[r][c] if text_strategy and r < len(text_matrix)
                     and c < len(text_matrix[r]) else page.crop(box, strict=False).extract_text() or "")
        cid = f"{table_id}-cell-{len(table['cells']) + 1}"
        cell = {"id": cid, "row": r, "column": c, "rowspan": rs, "colspan": cs,
                "bbox": list(box), "coordinates": "pdf_points_top_left",
                "raw_text": extracted, "raw_characters": chars,
                "text_reconstruction": "pdfplumber crop reading order; original character objects retained",
                "span_evidence": "text_inferred" if text_strategy else "geometric_edges",
                "kind": "unknown", "status": "pending_review"}
        table["cells"].append(cell)
        matrix[r][c] = extracted
        for rr in range(r, r + rs):
            for cc in range(c, c + cs):
                if grid[rr][cc] is not None:
                    raise ParseError("PDF geometric cells overlap; unresolved layout")
                grid[rr][cc] = cid
    table.update({"bbox": list(bbox), "raw_matrix": matrix, "grid": grid,
                  "extractor_matrix": text_matrix if text_strategy else extractor_matrix,
                  "status": "extracted_pending_review"})
    above = _pdf_neighbor_text(page, bbox, above=True)
    below = _pdf_neighbor_text(page, bbox, above=False)
    if above:
        table["caption"] = {"raw_text": above, "association": "adjacent_table_caption_candidate"}
    if below:
        table["notes"] = [{"raw_text": below, "association": "nearby_below_candidate"}]
    table["unit_candidates"] = _units(" ".join(c["raw_text"] for c in table["cells"]))
    if text_strategy:
        _merge_pdf_wrapped_rows(table)
    return table


def _merge_pdf_wrapped_rows(table: dict) -> None:
    """Join text-strategy continuation lines while retaining every source glyph."""
    matrix = table.get("raw_matrix", [])
    if not matrix:
        return
    cells_by_pos = {(cell["row"], cell["column"]): cell for cell in table["cells"]}
    groups: list[list[int]] = []
    for row_index, row in enumerate(matrix):
        if not any(str(value or "").strip() for value in row):
            continue
        if groups and _pdf_wrapped_row_evidence(matrix[groups[-1][-1]], row,
                                                groups[-1][-1], row_index, cells_by_pos):
            groups[-1].append(row_index)
        else:
            groups.append([row_index])
    if all(len(group) == 1 for group in groups) and len(groups) == len(matrix):
        return
    columns = max((len(row) for row in matrix), default=0)
    merged_cells, merged_matrix, merged_grid = [], [], []
    for new_row, source_rows in enumerate(groups):
        values, grid_row = [], []
        for column in range(columns):
            source_cells = [cells_by_pos[(row, column)] for row in source_rows
                            if (row, column) in cells_by_pos]
            texts = [cell["raw_text"] for cell in source_cells if cell["raw_text"].strip()]
            value = "\n".join(texts) if texts else ("" if source_cells else None)
            values.append(value)
            if not source_cells:
                grid_row.append(None)
                continue
            cid = f"{table['id']}-cell-{len(merged_cells) + 1}"
            boxes = [cell["bbox"] for cell in source_cells]
            chars = sorted((char for cell in source_cells for char in cell["raw_characters"]),
                           key=lambda char: (float(char.get("top", 0)), float(char.get("x0", 0))))
            merged = {**source_cells[0], "id": cid, "row": new_row, "column": column,
                      "rowspan": 1, "colspan": 1,
                      "bbox": [min(box[0] for box in boxes), min(box[1] for box in boxes),
                               max(box[2] for box in boxes), max(box[3] for box in boxes)],
                      "raw_text": value or "", "raw_characters": chars,
                      "source_cell_ids": [cell["id"] for cell in source_cells]}
            merged_cells.append(merged)
            grid_row.append(cid)
        merged_matrix.append(values)
        merged_grid.append(grid_row)
    table["cells"] = merged_cells
    table["raw_matrix"] = merged_matrix
    table["grid"] = merged_grid


def _pdf_wrapped_row_evidence(previous: list[Any], current: list[Any], previous_index: int,
                              current_index: int, cells_by_pos: dict) -> bool:
    """Merge only a physically adjacent, multi-column lexical continuation.

    An empty leading cell alone is deliberately insufficient: it is common in
    hierarchical tables with real values in the remaining columns.
    """
    columns = max(len(previous), len(current))
    if columns < 3:
        return False
    prev = [str(value or "").strip() for value in previous] + [""] * (columns - len(previous))
    curr = [str(value or "").strip() for value in current] + [""] * (columns - len(current))
    populated = [index for index, value in enumerate(curr) if value]
    if not populated or not all(prev[index] for index in populated):
        return False
    if len(populated) == 1:
        column = populated[0]
        if column in (0, columns - 1) or not prev[0] or not prev[-1]:
            return False
        continuation = curr[column]
        if not (continuation[:1] in {"Δ", "δ", "β", "γ", "α", "κ", "λ", "μ", "ν", "ω"}
                or prev[column].rstrip().endswith(("-", "–", "—"))
                or continuation[:1].islower()):
            return False
    elif (len(populated) == 2 and populated[0] == 0 and populated[1] == columns - 1
          and all(not curr[index] for index in range(1, columns - 1))):
        # Multiple alignment/content clues: both edge columns continue, an
        # inner cell remains occupied only in the preceding row, and the source
        # fragment begins as a lowercase continuation (e.g. Laboratory/stored).
        first, last = curr[0], curr[-1]
        if (len(first.split()) > 1 or not re.fullmatch(r"[A-Z0-9][A-Za-z0-9._-]*", first)
                or not last[:1].islower() or not re.search(r"\s", prev[0])
                or not prev[0] or not prev[-1]):
            return False
    else:
        return False
    prev_cells = [cells_by_pos.get((previous_index, column)) for column in populated]
    curr_cells = [cells_by_pos.get((current_index, column)) for column in populated]
    if any(cell is None for cell in [*prev_cells, *curr_cells]):
        return False
    for before, after in zip(prev_cells, curr_cells):
        if abs(float(before["bbox"][3]) - float(after["bbox"][1])) > 1.5:
            return False
    return True


def _continuations(tables: list[dict]) -> None:
    """Flag explicit continued captions + previous same table number, never merge."""
    for table in tables:
        caption = (table.get("caption") or {}).get("raw_text", "")
        if not re.search(r"\bcontinu(?:ed|ation)\b", caption, re.I):
            continue
        label = re.search(r"\btable\s+([A-Za-z]?\d+)\b", caption, re.I)
        if not label:
            continue
        candidates = []
        for earlier in tables:
            old = (earlier.get("caption") or {}).get("raw_text", "")
            old_label = re.search(r"\btable\s+([A-Za-z]?\d+)\b", old, re.I)
            if earlier["page"] == table["page"] - 1 and old_label and old_label[1].lower() == label[1].lower():
                if earlier["raw_matrix"] and table["raw_matrix"] and len(earlier["raw_matrix"][0]) == len(table["raw_matrix"][0]):
                    candidates.append(earlier["id"])
        table["continuation"] = {"status": "explicit_caption_candidate" if candidates else "unresolved",
                                 "candidate_ids": candidates, "merged": False,
                                 "headers_copied": False}


def parse_tsv(raw: str, width: int, height: int) -> list[dict]:
    columns = ["level", "page_num", "block_num", "par_num", "line_num", "word_num",
               "left", "top", "width", "height", "conf", "text"]
    # Tesseract emits literal text (including quotes), not CSV quoting.
    reader = csv.reader(io.StringIO(raw), delimiter="\t", quoting=csv.QUOTE_NONE)
    if next(reader, None) != columns:
        raise ParseError("Tesseract returned invalid TSV header")
    words = []
    seen_words: set[tuple[int, ...]] = set()
    for fields in reader:
        try:
            if len(fields) != len(columns):
                raise ValueError("TSV row must have exactly 12 columns")
            row = dict(zip(columns, fields))
            level = int(row["level"])
            hierarchy = tuple(int(row[k]) for k in columns[1:6])
            if not 1 <= level <= 5 or hierarchy[0] < 1 or any(v < 0 for v in hierarchy):
                raise ValueError("invalid hierarchy identifiers")
            # At each level its IDs are positive and all lower-level IDs zero.
            if any(v < 1 for v in hierarchy[:level]) or any(hierarchy[level:]):
                raise ValueError("invalid hierarchy identifiers for level")
            if level != 5:
                if row["text"]:
                    raise ValueError("text on a nonword TSV row")
                continue
            x, y, w, h = (int(row[k]) for k in ("left", "top", "width", "height"))
            conf = float(row["conf"])
            if min(x, y) < 0 or min(w, h) <= 0 or x + w > width or y + h > height or not math.isfinite(conf):
                raise ValueError("invalid bounds or confidence")
            if not -1 <= conf <= 100:
                raise ValueError("confidence outside range")
            if hierarchy in seen_words:
                raise ValueError("duplicate word hierarchy identifiers")
            seen_words.add(hierarchy)
            if not row["text"]:
                continue
            words.append({"raw_text": row["text"], "bbox_pixels": [x, y, x + w, y + h],
                          "confidence": conf, "line": [int(row[k]) for k in
                                                         ("page_num", "block_num", "par_num", "line_num")]})
        except (TypeError, ValueError, KeyError) as exc:
            raise ParseError(f"Invalid Tesseract word: {exc}") from exc
    return words


def _ocr_rows(words: list[dict], number: int) -> list[dict]:
    """Expose whitespace-separated OCR row groups as unresolved table candidates."""
    lines: dict[tuple, list] = {}
    for word in words:
        lines.setdefault(tuple(word["line"]), []).append(word)
    rows = []
    for line in lines.values():
        line.sort(key=lambda w: w["bbox_pixels"][0])
        groups: list[list[dict]] = []
        for word in line:
            if not groups or word["bbox_pixels"][0] - groups[-1][-1]["bbox_pixels"][2] > max(
                    12, 1.5 * (word["bbox_pixels"][3] - word["bbox_pixels"][1])):
                groups.append([])
            groups[-1].append(word)
        if len(groups) >= 2:
            rows.append(groups)
    if len(rows) < 2:
        return []
    table = _table(f"ocr-table-page-{number}", number, "ocr_whitespace_rows_heuristic")
    table["limitations"] = ["Row/column correspondence, merged cells, headers and footnotes unresolved",
                             "May be multi-column prose; never use as verified scientific table"]
    for r, row in enumerate(rows):
        texts, ids = [], []
        for c, group in enumerate(row):
            cid = f"{table['id']}-cell-{len(table['cells']) + 1}"
            boxes = [w["bbox_pixels"] for w in group]
            raw_text = " ".join(w["raw_text"] for w in group)
            table["cells"].append({"id": cid, "row": r, "column": c, "rowspan": None,
                                   "colspan": None, "raw_text": raw_text,
                                   "bbox": [min(b[0] for b in boxes), min(b[1] for b in boxes),
                                            max(b[2] for b in boxes), max(b[3] for b in boxes)],
                                   "coordinates": "image_pixels_top_left", "word_records": group,
                                   "span_evidence": "unresolved", "status": "unresolved"})
            texts.append(raw_text)
            ids.append(cid)
        table["raw_matrix"].append(texts)
        table["grid"].append(ids)
    return [table]


def _ocr_image(image: Any, number: int, opts: Options, deadline: float, mapper=None) -> dict:
    _check(deadline)
    if image.width * image.height > opts.max_pixels:
        raise ParseError("Rendered image exceeds max_pixels; reduce DPI or raise limit")
    executable = shutil.which(opts.tesseract)
    if not executable:
        raise ParseError("Tesseract executable missing; install Tesseract and language data, then use --tesseract PATH")
    with tempfile.TemporaryDirectory(prefix="literature-ocr-") as td:
        png = Path(td) / "page.png"
        image.save(png, format="PNG")
        # File-backed stdout avoids unbounded capture of a hostile/noisy engine.
        out_path, err_path = Path(td) / "engine.tsv", Path(td) / "engine.err"
        with out_path.open("wb") as out, err_path.open("wb") as err:
            try:
                process = subprocess.Popen([executable, str(png), "stdout", "-l", opts.language,
                                            "--dpi", str(opts.dpi), "tsv"], shell=False,
                                           stdout=out, stderr=err,
                                           start_new_session=os.name != "nt" and not _IN_WORKER)
                engine_deadline = min(deadline, time.monotonic() + opts.ocr_timeout)
                try:
                    while process.poll() is None:
                        if out_path.stat().st_size > opts.max_output_bytes or err_path.stat().st_size > opts.max_output_bytes:
                            raise ParseError("Tesseract output exceeds max_output_bytes")
                        if time.monotonic() >= engine_deadline:
                            raise ParseError("Tesseract exceeded OCR timeout")
                        time.sleep(min(.05, max(.001, engine_deadline - time.monotonic())))
                    completed_code = process.wait(timeout=5)
                finally:
                    if process.poll() is None:
                        _stop_tree(process.pid, process.kill, process_group=not _IN_WORKER)
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            # A tree tool can return success before termination,
                            # or fail to reap the exact process. Still enforce it.
                            process.kill()
                            process.wait(timeout=5)
            except OSError as exc:
                raise ParseError(f"Cannot start Tesseract: {exc}") from exc
        if out_path.stat().st_size > opts.max_output_bytes or err_path.stat().st_size > opts.max_output_bytes:
            raise ParseError("Tesseract output exceeds max_output_bytes")
        if completed_code:
            detail = err_path.read_bytes()[:2000].decode("utf-8", errors="replace")
            raise ParseError(f"Tesseract failed ({completed_code}): {detail}")
        raw = out_path.read_text(encoding="utf-8")
    words = parse_tsv(raw, image.width, image.height)
    if len(words) > opts.max_cells:
        raise ParseError("OCR word count exceeds max_cells")
    for word in words:
        if mapper:
            x0, y0, x1, y1 = word["bbox_pixels"]
            points = [mapper(x0, y0), mapper(x1, y1)]
            word["bbox_pdf_points_bottom_left"] = [min(p[0] for p in points), min(p[1] for p in points),
                                                   max(p[0] for p in points), max(p[1] for p in points)]
    mean = sum(w["confidence"] for w in words) / len(words) if words else None
    lines: dict[tuple, list[str]] = {}
    for word in words:
        lines.setdefault(tuple(word["line"]), []).append(word["raw_text"])
    return {"engine": "tesseract", "language": opts.language, "dpi": opts.dpi,
            "raw_tsv": raw, "text": "\n".join(" ".join(line) for line in lines.values()),
            "text_reconstruction": "TSV word order with inserted spaces/newlines; raw_tsv retained",
            "words": words, "mean_word_confidence": mean, "image_size_pixels": [image.width, image.height],
            "status": "pending_review", "quality": "noisy_or_empty" if mean is None or
            any(w["confidence"] < opts.min_ocr_confidence for w in words) else "confidence_only_not_validated",
            "table_candidates": _ocr_rows(words, number)}


def _ocr_pdf(path: Path, number: int, opts: Options, deadline: float) -> dict:
    pdfium = _dependency("pypdfium2", "ocr")
    _dependency("PIL.Image", "ocr")
    with pdfium.PdfDocument(str(path)) as pdf:
        page = pdf[number - 1]
        try:
            width, height = page.get_size()
            scale = opts.dpi / 72
            if math.ceil(width * scale) * math.ceil(height * scale) > opts.max_pixels:
                raise ParseError("PDF raster would exceed max_pixels; reduce DPI")
            bitmap = page.render(scale=scale)
            try:
                mapper = bitmap.get_posconv(page).to_page
                return _ocr_image(bitmap.to_pil(), number, opts, deadline, mapper)
            finally:
                bitmap.close()
        finally:
            page.close()


def parse_document(path: Path | str, options: Options | None = None) -> dict:
    """Extract locally. For untrusted documents use parse_bounded (also used by CLI)."""
    opts = options or Options()
    opts.validate()
    if opts.max_seconds < time.get_clock_info("monotonic").resolution:
        raise ParseError("max_seconds is below the platform monotonic clock resolution")
    deadline = time.monotonic() + opts.max_seconds
    path = Path(os.path.abspath(path))
    if not path.is_file() or path.stat().st_size == 0:
        raise ParseError("Input must be a nonempty file")
    if path.stat().st_size > opts.max_input_bytes:
        raise ParseError("Input exceeds max_input_bytes")
    with path.open("rb") as source:
        raw = source.read(opts.max_input_bytes + 1)
    if len(raw) > opts.max_input_bytes:
        raise ParseError("Input exceeds max_input_bytes")
    source_hash = hashlib.sha256(raw).hexdigest()
    report = {"schema_version": "document-parse/1", "source": {"path": str(path),
              "sha256": source_hash, "bytes": len(raw)}, "options": asdict(opts),
              "status": "extracted_pending_review", "ai_status": "pending",
              "science_approval": "none", "human_review": None,
              "pages": [], "tables": [], "errors": [], "limitations": [
                  "Extraction is not scientific validation; units and values are never normalized",
                  "Missing cells stay null; candidate captions/notes and continuation are not asserted",
                  "PDF reading order and OCR/table recognition require human review"]}
    def add_tables(tables):
        existing = sum(max(len(t["cells"]), sum(len(row) for row in t["grid"])) for t in report["tables"])
        extra = sum(max(len(t["cells"]), sum(len(row) for row in t["grid"])) for t in tables)
        if existing + extra > opts.max_cells:
            raise ParseError("Document total cell/grid count exceeds max_cells")
        report["tables"].extend(tables)
    suffix = path.suffix.lower()
    if suffix in {".html", ".htm"}:
        selected_pages(opts.pages, 1, opts.max_pages)
        try:
            html = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ParseError("HTML must be UTF-8; convert a copy explicitly before parsing") from exc
        parser = _TablesHTML(html, opts, deadline)
        parser.feed(html)
        parser.finish()
        text = "".join(parser.page_text)
        if not text.strip() and not parser.tables:
            raise ParseError("HTML contains no extractable text or tables")
        report["pages"] = [{"page": 1, "status": "extracted_pending_review", "native": {
            "engine": "stdlib_html", "text": text, "source_html": html,
            "text_reconstruction": "Decoded HTML text with explicit br/block/table boundaries as newlines; source_html retained"}, "ocr": None}]
        report["tables"] = parser.tables
        if opts.ocr != "off":
            report["limitations"].append("OCR applies only to PDF/images; HTML linked images are not fetched")
    elif suffix == ".pdf":
        pdfplumber = _dependency("pdfplumber", "tables")
        try:
            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                numbers = selected_pages(opts.pages, len(pdf.pages), opts.max_pages)
                report["total_pages"] = len(pdf.pages)
                for number in numbers:
                    _check(deadline)
                    page_report = {"page": number, "status": "extracted_pending_review", "native": None,
                                   "ocr": None, "errors": []}
                    report["pages"].append(page_report)
                    page = None
                    try:
                        page = pdf.pages[number - 1]
                        text = page.extract_text() or ""
                        page_report["size_pdf_points"] = [page.width, page.height]
                        page_report["rotation"] = page.rotation
                        page_report["native"] = {"engine": "pdfplumber", "text": text,
                                                  "characters": page.chars,
                                                  "coordinates": "pdf_points_top_left",
                                                  "text_reconstruction": "extractor reading order; original characters retained"}
                        settings = {"vertical_strategy": opts.table_strategy,
                                    "horizontal_strategy": opts.table_strategy}
                        found_tables = page.find_tables(settings)
                        text_strategy = opts.table_strategy == "text"
                        if text_strategy:
                            found_tables = _pdf_text_candidates(page, settings)
                        elif not found_tables:
                            # Borderless tables need the text strategy. It is run
                            # only after ruled detection returns nothing, and its
                            # candidates pass compact-region and column checks.
                            found_tables = _pdf_text_candidates(page, {
                                "vertical_strategy": "text", "horizontal_strategy": "text"})
                            text_strategy = True
                        for found in found_tables:
                            table = _pdf_table(page, found, f"table-{len(report['tables']) + 1}",
                                               number, opts, text_strategy=text_strategy)
                            add_tables([table])
                            if table["status"] == "untrusted":
                                page_report["errors"].append({"layer": "table_detection",
                                    "error": "Candidate table geometry was not reliable; no cell values were extracted"})
                    except Exception as exc:
                        page_report["errors"].append({"layer": "native", "error": str(exc)})
                    native_chars = len((page_report["native"] or {}).get("text", "").strip())
                    needs_ocr = opts.ocr == "always" or (opts.ocr == "auto" and native_chars < opts.min_native_chars)
                    if needs_ocr:
                        try:
                            page_report["ocr"] = _ocr_pdf(path, number, opts, deadline)
                            add_tables(page_report["ocr"]["table_candidates"])
                            if page_report["ocr"]["quality"] == "noisy_or_empty":
                                page_report["errors"].append({"layer": "ocr_quality", "error": "OCR empty or contains low-confidence words; unusable without review"})
                        except Exception as exc:
                            page_report["errors"].append({"layer": "ocr", "error": str(exc)})
                    if native_chars == 0 and not page_report["ocr"]:
                        page_report["errors"].append({"layer": "content", "error": "No text extracted; enable OCR for scanned pages"})
                    if page_report["errors"]:
                        page_report["status"] = "partial"
                        report["errors"].append({"page": number, "errors": page_report["errors"]})
                    if page is not None:
                        page.close()
        except ParseError:
            raise
        except Exception as exc:
            raise ParseError(f"Cannot parse PDF: {exc}") from exc
    elif suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}:
        if opts.ocr == "off":
            raise ParseError("Image inputs require --ocr auto or --ocr always")
        image_module = _dependency("PIL.Image", "ocr")
        with image_module.open(path) as image:
            total = getattr(image, "n_frames", 1)
            report["total_pages"] = total
            for number in selected_pages(opts.pages, total, opts.max_pages):
                _check(deadline)
                page_report = {"page": number, "native": None, "ocr": None, "status": "pending_review"}
                report["pages"].append(page_report)
                try:
                    image.seek(number - 1)
                    page_report["ocr"] = _ocr_image(image, number, opts, deadline)
                    add_tables(page_report["ocr"]["table_candidates"])
                    if page_report["ocr"]["quality"] == "noisy_or_empty":
                        page_report["status"] = "partial"
                        report["errors"].append({"page": number, "layer": "ocr_quality", "error": "OCR empty or low-confidence; requires review"})
                except Exception as exc:
                    page_report["status"] = "partial"
                    report["errors"].append({"page": number, "layer": "ocr", "error": str(exc)})
    else:
        raise ParseError("Supported inputs: PDF, UTF-8 HTML, PNG/JPEG/TIFF/BMP/WebP")
    _check(deadline)
    if sum(len(table["cells"]) for table in report["tables"]) > opts.max_cells:
        raise ParseError("Document total cell count exceeds max_cells")
    if path.stat().st_size != len(raw):
        raise ParseError("Source changed during extraction; output rejected")
    with path.open("rb") as source:
        current_bytes = source.read(opts.max_input_bytes + 1)
    if hashlib.sha256(current_bytes).hexdigest() != source_hash:
        raise ParseError("Source changed during extraction; output rejected")
    _continuations(report["tables"])
    for table in report["tables"]:
        table["source_sha256"] = source_hash
    if report["errors"]:
        report["status"] = "partial"
    if len((json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")) > opts.max_output_bytes:
        raise ParseError("Extraction exceeds max_output_bytes; select fewer pages")
    return report


def _worker(connection, path: str, opts: Options) -> None:
    global _IN_WORKER
    _IN_WORKER = True
    if os.name != "nt":
        os.setsid()
    try:
        result = parse_document(path, opts)
    except Exception as exc:
        result = str(exc)
    try:
        connection.send((not isinstance(result, str), result))
    except (BrokenPipeError, EOFError, OSError):
        pass  # Parent has already enforced the hard timeout.
    finally:
        connection.close()


def parse_bounded(path: Path | str, options: Options | None = None) -> dict:
    """Hard wall-clock bound for native PDF/image adapters, in a spawned worker."""
    opts = options or Options()
    opts.validate()
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(sender, str(path), opts))
    process.start()
    sender.close()
    try:
        if not receiver.poll(opts.max_seconds):
            raise ParseError("Document worker exceeded max_seconds (including startup)")
        try:
            okay, result = receiver.recv()
        except EOFError as exc:
            raise ParseError("Document worker exited without a result") from exc
        if not okay:
            raise ParseError(result)
        return result
    finally:
        receiver.close()
        if os.name != "nt" or process.is_alive():
            # Native engines may have descendants. Terminate this worker's exact
            # process tree, not a broad image-name match or unrelated engines.
            _stop_tree(process.pid, process.terminate)
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline PDF/HTML/image parsing; all evidence remains pending review")
    parser.add_argument("input", type=Path)
    parser.add_argument("--out-dir", required=True, type=Path, help="New output directory; existing paths are rejected")
    parser.add_argument("--ocr", choices=["off", "auto", "always"], default="off")
    parser.add_argument("--pages", default="", help="1-based pages/ranges, e.g. 1,3-5")
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--language", default="eng")
    parser.add_argument("--tesseract", default="tesseract")
    parser.add_argument("--table-strategy", choices=["lines", "text"], default="lines")
    for name, kind in (("ocr_timeout", float), ("max_seconds", float), ("max_input_bytes", int),
                       ("max_output_bytes", int), ("max_pages", int), ("max_cells", int),
                       ("max_span", int), ("max_pixels", int), ("min_native_chars", int),
                       ("min_ocr_confidence", float)):
        parser.add_argument("--" + name.replace("_", "-"), type=kind, default=getattr(Options(), name))
    args = parser.parse_args(argv)
    try:
        args.out_dir = Path(os.path.abspath(args.out_dir))
        _fresh_output(args.out_dir)
        opts = Options(**{name: getattr(args, name) for name in Options.__dataclass_fields__})
        report = parse_bounded(args.input, opts)
        _fresh_output(args.out_dir)
        args.out_dir.mkdir(parents=True, exist_ok=False)
        with (args.out_dir / "document.json").open("x", encoding="utf-8", newline="\n") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write("\n")
        print(json.dumps({"output": str(args.out_dir / 'document.json'),
                          "status": report["status"], "pages": len(report["pages"]),
                          "tables": len(report["tables"])}, ensure_ascii=False))
        return 3 if report["status"] == "partial" else 0
    except (OSError, ParseError) as exc:
        print(f"literature-parse: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
