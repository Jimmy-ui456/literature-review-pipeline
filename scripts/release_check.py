"""Audit a deliberately small source candidate and its built archives."""
from __future__ import annotations
import argparse, configparser, hashlib, json, re, shutil, stat, tarfile, zipfile
from pathlib import Path, PurePosixPath

DEFAULT_CONFIG = "release_allowlist.json"
ABSOLUTE_PATH = re.compile(r"(?:(?<![A-Za-z])[A-Za-z]:[\\/][^\s\"']*|(?:file://)?/(?:Users|home|tmp|var|etc)(?:/[^\s\"']*)?)")
CREDENTIALS = {"github_token": re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"), "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"), "private_key": re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"), "generic_assignment": re.compile(r"(?i)(?:api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]{12,}")}
# Each exception is exact fixed public fixture text, never a file-wide bypass.
NARROW_EXCEPTIONS = {
    # These exact values are synthetic redaction sentinels passed to fake clients.
    # Keep scanning every other assignment in these files and all other files.
    ("tests/test_sources.py", "generic_assignment", 'api_key=' + '"KEY_SENTINEL'),
    ("tests/test_publication_sources.py", "generic_assignment", 'api_key=' + '"KEY_SENTINEL'),
    ("tests/test_net.py", "absolute_local_path", "file:" + "//" + "/" + "etc/passwd"),
    ("tests/extensions/test_zotero_safety.py", "absolute_local_path", "file:" + "//" + "/" + "tmp/zotero"),
}

def load_config(root: Path) -> dict: return json.loads((root / DEFAULT_CONFIG).read_text(encoding="utf-8"))
def safe_rel(value: str) -> str:
    if not value or "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value): raise ValueError(f"unsafe path: {value!r}")
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts or any("\x00" in x for x in p.parts): raise ValueError(f"unsafe path: {value!r}")
    return p.as_posix()
def normalized_relative(path: Path, root: Path) -> str:
    try: return safe_rel(path.relative_to(root).as_posix())
    except ValueError as exc: raise ValueError(f"outside or unsafe root path: {path}") from exc
def allowed(rel: str, config: dict) -> tuple[bool, str]:
    try: rel = safe_rel(rel)
    except ValueError: return False, "unsafe_relative_path"
    parts = PurePosixPath(rel).parts
    if any(p in config["forbidden_parts"] or p.endswith(".egg-info") for p in parts): return False, "forbidden_path_part"
    if any(rel.endswith(s) for s in config["forbidden_suffixes"]): return False, "forbidden_suffix"
    if "/" not in rel: return rel in config["root_files"], "root_not_allowlisted"
    for d, suffixes in config["directories"].items():
        if rel.startswith(d + "/"): return any(rel.endswith(s) for s in suffixes), "extension_not_allowlisted"
    return False, "directory_not_allowlisted"
def source_files(root: Path) -> list[Path]: return [p for p in root.rglob("*") if p.is_file() or p.is_symlink()]
def inspect_paths(root: Path, files: list[Path], config: dict) -> list[dict]:
    out=[]
    for p in files:
        try: rel=normalized_relative(p,root)
        except ValueError as exc: out.append({"path":str(p),"rule":"relative_path","detail":str(exc)}); continue
        if p.is_symlink(): out.append({"path":rel,"rule":"symlink","detail":"symlinks are not publishable"})
        ok, rule=allowed(rel,config)
        if not ok: out.append({"path":rel,"rule":rule,"detail":"not allowed by release_allowlist.json"})
    return out
def inspect_content(root: Path, files: list[Path]) -> list[dict]:
    out=[]
    for p in files:
        if p.is_symlink() or p.suffix.lower() not in {".py",".md",".json",".toml",".in",".yml",".yaml",".txt"}: continue
        rel=normalized_relative(p,root)
        try: text=p.read_text(encoding="utf-8")
        except UnicodeDecodeError: out.append({"path":rel,"rule":"undecodable_text","detail":"candidate text file is not UTF-8"}); continue
        for name, pattern in {"absolute_local_path":ABSOLUTE_PATH,**CREDENTIALS}.items():
            for m in pattern.finditer(text):
                matched=m.group(0)
                if (rel,name,matched) in NARROW_EXCEPTIONS: continue
                out.append({"path":rel,"line":text.count("\n",0,m.start())+1,"rule":name,"detail":"sensitive local path or credential-shaped text"})
    return out
def generated_exclusion(rel: str) -> str | None:
    parts = PurePosixPath(rel).parts
    if rel in {"PKG-INFO", "setup.cfg"}:
        return "sdist_generated_metadata"
    if any(part in {".git", ".validation", ".venv", "venv", "build", "dist", "tmp", "__pycache__"} or part.endswith(".egg-info") for part in parts):
        return "known_generated_directory"
    return None
def prepare_stage(root: Path, stage: Path, config: dict) -> tuple[list[dict], list[str], list[dict]]:
    root,stage=root.resolve(),stage.resolve()
    if stage==root or stage in root.parents: raise ValueError("stage must not be source root or an ancestor")
    if stage.exists(): raise ValueError("stage target already exists; choose a new empty target")
    stage.mkdir(parents=True)
    findings=[]; selected=[]; excluded=[]
    for p in source_files(root):
        rel=normalized_relative(p,root); ok,rule=allowed(rel,config)
        if not ok:
            reason=generated_exclusion(rel)
            if reason: excluded.append({"path":rel,"reason":reason}); continue
            findings.append({"path":rel,"rule":rule,"detail":"unknown source candidate is not allowlisted"}); continue
        if p.is_symlink(): findings.append({"path":rel,"rule":"symlink","detail":"allowlisted source is a symlink"}); continue
        dst=stage/rel; dst.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(p,dst)
        selected.append(rel)
    return findings, selected, excluded
def archive_members(archive: Path) -> list[tuple[str,bool]]:
    if archive.suffix==".whl" or zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z: return [(x.filename,stat.S_ISLNK(x.external_attr>>16)) for x in z.infolist()]
    with tarfile.open(archive,"r:*") as t: return [(x.name,x.issym() or x.islnk()) for x in t.getmembers()]
def dependency_contract(line: str, package: str, bounds: tuple[str, ...], extra: str | None = None) -> bool:
    """Match exact emitted constraints/marker, never substrings like <4 in <40."""
    compact = re.sub(r"\s+", "", line).lower()
    requirement, _, marker = compact.partition(";")
    match = re.fullmatch(r"requires(?:-dist)?:" + re.escape(package.lower()) + r"(\([^)]*\)|[<>=!~].*)", requirement)
    if not match or set(match[1].strip("()").split(",")) != set(bounds):
        return False
    if extra is None:
        return not marker
    return marker in {f'extra=="{extra}"', f"extra=='{extra}'"}

def archive_allowed(rel: str, kind: str, config: dict) -> bool:
    try: rel=safe_rel(rel)
    except ValueError: return False
    if kind=="wheel": return ((rel.startswith("litsearch/") and rel.endswith((".py",".json"))) or (rel.startswith("litsearch_zotero/") and rel.endswith(".py")) or re.fullmatch(r"d_pan_literature_pipeline-3\.0\.0\.dist-info/(METADATA|WHEEL|entry_points\.txt|RECORD|top_level\.txt|licenses/LICENSE(?:-(CANDIDATE\.txt|STATUS\.md))?)",rel) is not None)
    root="d_pan_literature_pipeline-3.0.0/"
    if rel == root[:-1]: return True
    if not rel.startswith(root): return False
    inner=rel[len(root):]
    return (inner in {"", ".github", ".github/workflows", "d_pan_literature_pipeline.egg-info", "docs", "examples", "examples/synthetic", "litsearch", "litsearch/profiles", "litsearch_zotero", "scripts", "tests", "tests/extensions", "setup.cfg", "PKG-INFO"} or inner.endswith("/") or re.fullmatch(r"d_pan_literature_pipeline\.egg-info/(PKG-INFO|SOURCES\.txt|dependency_links\.txt|entry_points\.txt|requires\.txt|top_level\.txt)",inner) is not None or allowed(inner,config)[0])
def inspect_archive(archive: Path, config: dict) -> dict:
    kind="wheel" if archive.suffix==".whl" else "sdist"; members=archive_members(archive); findings=[]; names=[]
    for name,link in members:
        names.append(name)
        if link or not archive_allowed(name,kind,config): findings.append({"path":name,"rule":"archive_link" if link else "archive_member_not_allowlisted"})
    for req in ("litsearch/profiles/pantothenate.json","litsearch/profiles/lysine.json","litsearch/profiles/glutathione.json"):
        expected=req if kind=="wheel" else "d_pan_literature_pipeline-3.0.0/"+req
        if expected not in names: findings.append({"path":expected,"rule":"required_member_missing"})
    required_modules=("litsearch/__init__.py","litsearch/cli.py","litsearch/core.py",
                      "litsearch/service_probe.py","litsearch/discovery.py","litsearch/document_parse.py",
                      "litsearch_zotero/__init__.py","litsearch_zotero/attach_pdfs.py",
                      "litsearch_zotero/dynamic_citations.py", "litsearch_zotero/word_plugin.py")
    for req in required_modules:
        expected=req if kind=="wheel" else "d_pan_literature_pipeline-3.0.0/"+req
        if expected not in names: findings.append({"path":expected,"rule":"required_member_missing"})
    metadata_name=(next((n for n in names if n.endswith(".dist-info/METADATA")), None) if kind=="wheel" else "d_pan_literature_pipeline-3.0.0/PKG-INFO")
    try:
        if kind=="wheel":
            with zipfile.ZipFile(archive) as z: metadata=z.read(metadata_name).decode("utf-8")
        else:
            with tarfile.open(archive,"r:*") as t: metadata=t.extractfile(metadata_name).read().decode("utf-8")
    except (KeyError, AttributeError, UnicodeDecodeError):
        findings.append({"path":str(metadata_name),"rule":"metadata_missing_or_unreadable"}); metadata=""
    normalized={line.replace(" ", "") for line in metadata.splitlines() if line.startswith("Requires-Dist:") or line.startswith("Requires:")}
    for requirement in ("openpyxl>=3.1,<4", "pypdf>=5,<7"):
        package, constraints=requirement.split(">=",1); expected=(">="+constraints.split(",")[0], "<"+constraints.split(",")[1][1:])
        if not any(dependency_contract(line, package, expected) for line in normalized): findings.append({"path":str(metadata_name),"rule":"required_dependency_constraint_missing","detail":requirement})
    optional = {
        "citations": {"lxml": (">=5", "<7")},
        "tables": {"pdfplumber": (">=0.11", "<0.12")},
        "ocr": {"pdfplumber": (">=0.11", "<0.12"), "pypdfium2": (">=4.30", "<6"), "pillow": (">=10", "<13")},
        "parsing": {"pdfplumber": (">=0.11", "<0.12"), "pypdfium2": (">=4.30", "<6"),
                    "pillow": (">=10", "<13")},
    }
    for extra, requirements in optional.items():
        for package, bounds in requirements.items():
            if not any(dependency_contract(line, package, bounds, extra) for line in normalized):
                findings.append({"path":str(metadata_name), "rule":"optional_dependency_missing",
                                 "detail":extra + ":" + package})
    win32_markers = {'sys_platform=="win32"andextra=="citations"',
                     'extra=="citations"andsys_platform=="win32"'}
    has_word_bridge = False
    for line in normalized:
        requirement, _, marker = line.partition(";")
        if (dependency_contract(requirement, "pywin32", (">=306", "<400"))
                and marker.replace("'", '"') in win32_markers):
            has_word_bridge = True
    if not has_word_bridge:
        findings.append({"path":str(metadata_name), "rule":"optional_dependency_missing",
                         "detail":"citations:pywin32_windows_only"})
    entry_name = (next((n for n in names if n.endswith(".dist-info/entry_points.txt")), None)
                  if kind == "wheel" else "d_pan_literature_pipeline-3.0.0/d_pan_literature_pipeline.egg-info/entry_points.txt")
    expected_entries = {
        "literature-cite": "litsearch_zotero.dynamic_citations:main",
        "literature-cite-word": "litsearch_zotero.word_plugin:main",
        "literature-service-probe": "litsearch.service_probe:main",
        "literature-discover": "litsearch.discovery:main",
        "literature-parse": "litsearch.document_parse:main",
    }
    entries = configparser.ConfigParser(interpolation=None)
    try:
        if kind == "wheel":
            with zipfile.ZipFile(archive) as z: entry_text = z.read(entry_name).decode("utf-8")
        else:
            with tarfile.open(archive, "r:*") as t: entry_text = t.extractfile(entry_name).read().decode("utf-8")
        entries.read_string(entry_text)
    except (KeyError, AttributeError, UnicodeDecodeError, configparser.Error):
        findings.append({"path":str(entry_name), "rule":"tool_entrypoints_missing_or_invalid"})
    for name, target in expected_entries.items():
        if entries.get("console_scripts", name, fallback="").strip() != target:
            findings.append({"path":str(entry_name), "rule":"tool_entrypoint_missing", "detail":name})
    return {"archive":str(archive),"kind":kind,"sha256":hashlib.sha256(archive.read_bytes()).hexdigest(),"members":names,"findings":findings,"result":"pass" if not findings else "fail"}
def main() -> int:
    p=argparse.ArgumentParser(); p.add_argument("--root",type=Path,default=Path.cwd()); p.add_argument("--prepare-stage",type=Path); p.add_argument("--archive",type=Path,action="append"); p.add_argument("--archive-only",action="store_true"); p.add_argument("--report",type=Path); a=p.parse_args(); root=a.root.resolve(); config=load_config(root)
    selected=[]; excluded=[]
    if a.prepare_stage:
        findings,selected,excluded=prepare_stage(root,a.prepare_stage.resolve(),config); checked=source_files(a.prepare_stage.resolve()) if not findings else []
        if not findings: findings+=inspect_paths(a.prepare_stage.resolve(),checked,config)+inspect_content(a.prepare_stage.resolve(),checked)
    elif a.archive_only: checked=[]; findings=[]
    else: checked=source_files(root); findings=inspect_paths(root,checked,config)+inspect_content(root,checked)
    archives=[inspect_archive(x.resolve(),config) for x in a.archive or []]; findings += [x for arc in archives for x in arc["findings"]]
    report={"root":str(root),"checked":[str(x) for x in checked],"selected":selected,"excluded":excluded,"findings":findings,"archives":archives,"result":"pass" if not findings else "fail"}; rendered=json.dumps(report,ensure_ascii=False,indent=2)+"\n"
    if a.report: a.report.parent.mkdir(parents=True,exist_ok=True); a.report.write_text(rendered,encoding="utf-8")
    print(rendered); return 0 if not findings else 1
if __name__=="__main__": raise SystemExit(main())
