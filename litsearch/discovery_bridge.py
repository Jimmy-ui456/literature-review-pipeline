"""Convert discovery observations to untrusted Records without losing provenance.

Each usable observation remains a separate Record. Candidates with declared or
recomputed identity conflicts are withheld, so a later merge cannot select a winner.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile

from .core import norm_title, title_key
from .discovery import _doi, _epmc_id
from .models import Record
from .verification import epmc_record

MAX_INPUT_BYTES = 30 * 1024 * 1024
MAX_OBSERVATIONS = 20_000


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key: " + key)
        result[key] = value
    return result


def _text(item: dict, key: str, default: str = "") -> str:
    value = item.get(key, default)
    if not isinstance(value, str):
        raise ValueError("Expected string: " + key)
    return value


def _strings(item: dict, key: str) -> list[str]:
    value = item.get(key, [])
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise ValueError("Expected string array: " + key)
    return value


def _bounded_shape(data) -> None:
    stack = [(data, 0)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if depth > 64 or nodes > 1_000_000:
            raise ValueError("Discovery JSON nesting or node budget exceeded")
        if isinstance(value, dict):
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)


def _record(payload: dict) -> tuple[Record, list[str]]:
    epmc = _epmc_id(payload.get("source"), payload.get("id"))
    if epmc:
        source, identifier = epmc
        clean = {key: _text(payload, key) for key in
                 ("title", "doi", "pubYear", "abstractText", "pmcid", "pageInfo")}
        clean.update(source=source, id=identifier)
        if clean["doi"] and not _doi(clean["doi"]):
            raise ValueError("Invalid Europe PMC DOI")
        for key in ("authorList", "journalInfo", "pubTypeList"):
            if key in payload:
                if not isinstance(payload[key], dict):
                    raise ValueError("Expected object: " + key)
                clean[key] = payload[key]
        authors = clean.get("authorList", {}).get("author", [])
        if not isinstance(authors, list) or any(not isinstance(a, dict) for a in authors):
            raise ValueError("Invalid Europe PMC authors")
        for author in authors:
            for key in ("fullName", "firstName", "lastName"):
                _text(author, key)
        info = clean.get("journalInfo", {})
        for key in ("volume", "issue"):
            _text(info, key)
        journal = info.get("journal", {})
        if not isinstance(journal, dict):
            raise ValueError("Invalid Europe PMC journal")
        _text(journal, "title")
        _strings(clean.get("pubTypeList", {}), "pubType")
        record = epmc_record(clean)
        ids = [f"epmc:{source}:{identifier}"]
        if record.doi:
            ids.append("doi:" + record.doi)
    else:
        doi = _doi(payload.get("DOI"))
        if not doi:
            raise ValueError("Unsupported or missing stable source identifier")
        titles = _strings(payload, "title")
        if not titles or not any(x.strip() for x in titles):
            raise ValueError("Missing Crossref title")
        author_items = payload.get("author", [])
        if not isinstance(author_items, list) or any(not isinstance(a, dict) for a in author_items):
            raise ValueError("Invalid Crossref authors")
        authors = [" ".join(filter(None, (_text(a, "given"), _text(a, "family"))))
                   or _text(a, "name") for a in author_items]
        year = ""
        date = payload.get("issued", payload.get("published", {}))
        if not isinstance(date, dict):
            raise ValueError("Invalid Crossref date")
        parts = date.get("date-parts", [])
        if not isinstance(parts, list) or (parts and (not isinstance(parts[0], list) or not parts[0]
                or type(parts[0][0]) is not int or not 1 <= parts[0][0] <= 9999)):
            raise ValueError("Invalid Crossref date-parts")
        if parts:
            year = str(parts[0][0])
        record = Record(title=norm_title(" ".join(titles)), doi=doi, year=year,
                        authors=[a for a in authors if a],
                        journal=" ".join(_strings(payload, "container-title")),
                        abstract=norm_title(_text(payload, "abstract")),
                        volume=_text(payload, "volume"), issue=_text(payload, "issue"),
                        pages=_text(payload, "page"), url="https://doi.org/" + doi,
                        source_ids=["Crossref:" + doi])
        ids = ["doi:" + doi]
    if not record.title.strip():
        raise ValueError("Missing observation title")
    # Validate the normalized schema and discard every imported trust claim.
    record = Record.from_dict(record.to_dict())
    record.clear_derived_fields()
    return record, ids


def convert_discovery(path: Path) -> tuple[list[dict], dict]:
    if not path.is_file():
        raise ValueError("Discovery input must be a local JSON file")
    with path.open("rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("Discovery input exceeds 30 MiB")
    def nonfinite(value):
        raise ValueError("Non-finite JSON number: " + value)
    try:
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique, parse_constant=nonfinite)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid bounded JSON input") from exc
    _bounded_shape(data)
    if (not isinstance(data, dict) or type(data.get("schema_version")) is not int
            or data["schema_version"] != 2):
        raise ValueError("Expected discovery schema_version 2")
    if not isinstance(data.get("status"), str) or data["status"] not in {"partial", "bounded_retrieval_finished"}:
        raise ValueError("Expected actual retrieval results; offline_plan has no observations to bridge")
    if not isinstance(data.get("topic_fingerprint"), str) or not re.fullmatch(
            r"[0-9a-f]{64}", data["topic_fingerprint"]):
        raise ValueError("Invalid topic fingerprint")
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or len(candidates) > MAX_OBSERVATIONS:
        raise ValueError("Invalid or excessive candidates")
    digest = hashlib.sha256(raw).hexdigest()
    records, links, metadata, seen = [], [], [], set()
    count = 0
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("Candidate must be an object")
        key = _text(candidate, "candidate_id")
        if not key or len(key) > 2048 or key in seen:
            raise ValueError("Invalid or duplicate candidate_id")
        seen.add(key)
        identifiers = _strings(candidate, "identifiers")
        observations = candidate.get("observations")
        conflicts = candidate.get("conflicts", {})
        if not isinstance(observations, list) or not observations or not isinstance(conflicts, dict):
            raise ValueError("Invalid observations or conflicts")
        candidate_meta = {"candidate_id": key, "identifiers": identifiers, "conflicts": conflicts}
        metadata.append(candidate_meta)
        prepared = []
        candidate_links = []
        for index, observation in enumerate(observations):
            count += 1
            if count > MAX_OBSERVATIONS:
                raise ValueError("Too many observations")
            if (not isinstance(observation, dict) or not isinstance(observation.get("payload"), dict)
                    or not isinstance(observation.get("route"), dict)
                    or (observation.get("request_id") is not None and
                        (type(observation["request_id"]) is not int or observation["request_id"] < 0))):
                raise ValueError("Malformed discovery observation")
            pointer = f"discovery:{digest}:{key}:{index}"
            link = {"source_id": pointer, "candidate_id": key, "observation_index": index,
                    "observation": observation, "record_index": None}
            try:
                record, ids = _record(observation["payload"])
                if not set(ids).issubset(set(identifiers)):
                    raise ValueError("Observation identity disagrees with candidate identifiers")
                record.source_ids.append(pointer)
                prepared.append((record, ids, link))
            except ValueError as exc:
                link["unresolved_reason"] = str(exc)
            candidate_links.append(link)
        computed = {}
        for field in ("title", "year", "doi", "pmid", "pmcid"):
            values = {title_key(record.title) if field == "title" else getattr(record, field)
                      for record, _, _ in prepared}
            values.discard("")
            if len(values) > 1:
                computed[field] = sorted(values)
        # Candidate-level IDs can expose conflict even when one observation is
        # incomplete and could not be normalized to a bibliographic Record.
        for prefix in ("doi:", "epmc:MED:", "epmc:PMC:"):
            values = {value[len(prefix):] for value in identifiers if value.startswith(prefix)}
            if len(values) > 1:
                computed[prefix.rstrip(":")] = sorted(values)
        candidate_meta["computed_conflicts"] = computed
        candidate_meta["withheld"] = bool(conflicts or computed)
        if candidate_meta["withheld"]:
            for link in candidate_links:
                if "unresolved_reason" in link:
                    link["normalization_error"] = link["unresolved_reason"]
                link["unresolved_reason"] = "withheld_conflicting_candidate"
        else:
            for record, _, link in prepared:
                link["record_index"] = len(records)
                records.append(record.to_dict())
        links.extend(candidate_links)
    unresolved = data.get("unresolved_references", [])
    if not isinstance(unresolved, list):
        raise ValueError("unresolved_references must be an array")
    provenance = {"schema_version": 1, "source_file": path.name, "source_sha256": digest,
                  "input_status": data["status"], "topic_fingerprint": data["topic_fingerprint"],
                  "policy": "withhold_conflicting_candidates; one_record_per_usable_observation; no_trust_inheritance",
                  "candidates": metadata, "observations": links, "original_unresolved_references": unresolved,
                  "input_metadata": {key: value for key, value in data.items()
                                     if key not in {"candidates", "unresolved_references"}},
                  "counts": {"records": len(records), "observations": count,
                             "withheld_conflicting_candidates": sum(x["withheld"] for x in metadata),
                             "unresolved_observations": sum(x["record_index"] is None for x in links)}}
    return records, provenance


def _redirected(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024))


def bridge(path: Path, out_dir: Path) -> dict:
    out_dir = Path(os.path.abspath(out_dir))
    if out_dir.exists() or any(_redirected(p) for p in (out_dir, *out_dir.parents)):
        raise ValueError("Choose a new, nonredirected output directory")
    records, provenance = convert_discovery(path)
    payloads = {"records.json": records, "provenance.json": provenance}
    encoded = {name: (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
               for name, value in payloads.items()}
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".discovery-bridge-", dir=out_dir.parent))
    try:
        for name, raw in encoded.items():
            (stage / name).write_bytes(raw)
        manifest = {"schema_version": 1, "status": "records_prepared_unverified",
                    "source_sha256": provenance["source_sha256"], "counts": provenance["counts"],
                    "input_status": provenance["input_status"],
                    "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in encoded.items()},
                    "scientific_approval": False}
        (stage / "bridge_report.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        if out_dir.exists() or out_dir.is_symlink():
            raise ValueError("Output appeared during conversion")
        stage.rename(out_dir)
        return manifest
    finally:
        if stage.exists() and stage.resolve().parent == out_dir.parent.resolve():
            shutil.rmtree(stage)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("discovery", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(bridge(args.discovery, args.out_dir), ensure_ascii=True, indent=2))
        return 0
    except (ValueError, OSError) as exc:
        print("Discovery bridge: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
