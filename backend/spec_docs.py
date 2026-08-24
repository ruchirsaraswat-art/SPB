"""
Specification-document intake (2026-08-23 rtl-gen spec, section a).

Storage: <working_dir>/spec_docs/<phy_type>/<doc_id>.<ext> (a COPY of the
attached file; url/reference sources have no file) plus <doc_id>.meta.json
next to it, and one per-PHY answer file _status.json
({"answer": "attached"|"no_spec"|null, "answered_at": ...}) - the thing that
suppresses the intake banner. Listing searches current-then-previous
working-dir roots, newest first, exactly like architectures/interfaces.

File attach is a SERVER-SIDE PATH copy (the metadata's source carries
{"type": "file", "path": "<absolute local path>"} and the backend copies the
file in) rather than a multipart upload - the tool is a one-designer local
app where backend and browser share a filesystem, so a path is the simpler
mechanism that works locally (per the implementation note in the task; the
spec's multipart wording is satisfied in effect: the stored artifact is an
uploaded COPY under spec_docs/).

Context consumption (the pragmatic rule, spec section a): document contents
are NEVER inlined into a prompt. render_chat_context() produces the compact
metadata block for chat turns (title/family/version/user_notes/
relevant_sections locators), capped at 2000 chars - user_notes truncated
first, then docs beyond the first 3 dropped with an "N more docs attached"
line. resolve_docs_for_generation() returns metadata + the absolute file
path for generation runs, which add the selective-Read instruction
(rtl_generate.build_spec_docs_generation_block).
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from settings import all_spec_docs_roots, spec_docs_root

VALID_PHY_TYPES = ("ser-des", "ddr", "lpddr", "hbm", "optical", "die-to-die")

# Workspaces a doc can apply to ("controller" is the display name of the
# digital workspace; these are the applies_to_workspaces vocabulary).
WORKSPACES = ("controller", "interface", "firmware")

SOURCE_TYPES = ("file", "url", "reference")

# Families the UI suggests; free text is allowed (spec: "free text").
SUGGESTED_FAMILIES = (
    "JEDEC", "PCIe", "UCIe", "CXL", "Ethernet", "USB", "MIPI", "proprietary", "other",
)

ANSWERS = ("attached", "no_spec")

# Types the generation agent can actually Read; anything else is stored but
# marked unreadable_by_agent.
READABLE_EXTS = (".pdf", ".txt", ".md", ".html")

MAX_FILE_BYTES = 40 * 1024 * 1024  # 40 MB cap

CHAT_CONTEXT_CAP = 2000
CHAT_CONTEXT_MAX_DOCS = 3

_DOC_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_phy_type(phy_type: str) -> str:
    if phy_type not in VALID_PHY_TYPES:
        raise ValueError(
            f"unknown phy_type {phy_type!r} - must be one of: {', '.join(VALID_PHY_TYPES)}"
        )
    return phy_type


def validate_doc_id(doc_id: str) -> str:
    if not isinstance(doc_id, str) or not _DOC_ID_RE.match(doc_id):
        raise ValueError(
            f"invalid spec-doc id {doc_id!r} - must be 1-64 chars of [a-z0-9_-]"
        )
    return doc_id


def slugify_title(title: str) -> str:
    """doc_id = slugified title (1-64 of [a-z0-9_-])."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", (title or "").lower()).strip("-")[:64]
    return slug or "doc"


def _phy_dir(phy_type: str) -> Path:
    return spec_docs_root() / phy_type


def find_doc_dir(phy_type: str, doc_id: str) -> Path | None:
    """Directory (current-then-previous roots) that holds this doc's
    meta.json, or None."""
    for root in all_spec_docs_roots():
        if (root / phy_type / f"{doc_id}.meta.json").is_file():
            return root / phy_type
    return None


def _unique_doc_id(phy_type: str, base: str) -> str:
    """Uniquify with a -2/-3/... suffix across all roots."""
    if find_doc_dir(phy_type, base) is None:
        return base
    n = 2
    while True:
        candidate = f"{base[: 64 - len(str(n)) - 1]}-{n}"
        if find_doc_dir(phy_type, candidate) is None:
            return candidate
        n += 1


def _validate_metadata(meta: dict[str, Any]) -> list[str]:
    """All problems at once (deliverable-modes 422 style)."""
    problems: list[str] = []
    if not isinstance(meta, dict):
        return ["metadata must be a JSON object"]
    if not (meta.get("title") or "").strip():
        problems.append("title is required")
    source = meta.get("source")
    if not isinstance(source, dict) or source.get("type") not in SOURCE_TYPES:
        problems.append(
            f"source.type must be one of: {', '.join(SOURCE_TYPES)}"
        )
    else:
        stype = source["type"]
        if stype == "file" and not (source.get("path") or "").strip():
            problems.append("source.type='file' requires source.path (absolute local path to copy in)")
        if stype == "url" and not (source.get("url") or "").strip():
            problems.append("source.type='url' requires source.url")
        if stype == "reference" and not (source.get("citation") or "").strip():
            problems.append("source.type='reference' requires source.citation")
    ws = meta.get("applies_to_workspaces")
    if ws is not None:
        if not isinstance(ws, list) or any(w not in WORKSPACES for w in ws):
            problems.append(
                f"applies_to_workspaces must be a list drawn from: {', '.join(WORKSPACES)}"
            )
    sections = meta.get("relevant_sections")
    if sections is not None:
        if not isinstance(sections, list):
            problems.append("relevant_sections must be a list of {label, locator, applies_to}")
        else:
            for i, sec in enumerate(sections):
                if not isinstance(sec, dict) or not (sec.get("label") or "").strip() or not (
                    sec.get("locator") or ""
                ).strip():
                    problems.append(
                        f"relevant_sections[{i}]: needs a non-empty label and locator "
                        "(give page ranges - the generator reads only what you list)"
                    )
                elif sec.get("applies_to") is not None and (
                    not isinstance(sec["applies_to"], list)
                    or any(w not in WORKSPACES for w in sec["applies_to"])
                ):
                    problems.append(
                        f"relevant_sections[{i}].applies_to must be a list drawn from: "
                        + ", ".join(WORKSPACES)
                    )
    return problems


def save_spec_doc(phy_type: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """Create one spec doc: validate metadata (raising ValueError listing all
    problems), copy the source file in when source.type=='file', write
    <doc_id>.meta.json, and mark the per-PHY answer 'attached'. Returns the
    stored metadata."""
    validate_phy_type(phy_type)
    problems = _validate_metadata(metadata)
    src_path: Path | None = None
    source = metadata.get("source") if isinstance(metadata, dict) else None
    if not problems and isinstance(source, dict) and source.get("type") == "file":
        src_path = Path(str(source["path"])).expanduser()
        if not src_path.is_file():
            problems.append(f"source.path {src_path} does not exist or is not a file")
        elif src_path.stat().st_size > MAX_FILE_BYTES:
            problems.append(
                f"source.path {src_path} is {src_path.stat().st_size} bytes - "
                f"over the {MAX_FILE_BYTES // (1024 * 1024)} MB cap"
            )
    if problems:
        raise ValueError("spec-doc problems: " + "; ".join(problems))

    doc_id = _unique_doc_id(phy_type, slugify_title(metadata["title"]))
    phy_dir = _phy_dir(phy_type)
    phy_dir.mkdir(parents=True, exist_ok=True)

    stored: dict[str, Any] = {
        "id": doc_id,
        "title": str(metadata["title"]).strip(),
        "standard_family": str(metadata.get("standard_family") or "").strip(),
        "version": str(metadata.get("version") or "").strip(),
        "source": dict(source or {}),
        "user_notes": str(metadata.get("user_notes") or ""),
        "relevant_sections": [dict(s) for s in (metadata.get("relevant_sections") or [])],
        "applies_to_workspaces": list(
            metadata.get("applies_to_workspaces") or list(WORKSPACES)
        ),
        "phy_type": phy_type,
        "added_at": _now(),
    }
    if src_path is not None:
        ext = src_path.suffix.lower() or ".bin"
        dest = phy_dir / f"{doc_id}{ext}"
        shutil.copy2(src_path, dest)
        stored["source"] = {
            "type": "file",
            "path": f"spec_docs/{phy_type}/{dest.name}",
        }
        if ext not in READABLE_EXTS:
            stored["unreadable_by_agent"] = True

    (phy_dir / f"{doc_id}.meta.json").write_text(json.dumps(stored, indent=2))
    set_answer(phy_type, "attached")
    return stored


def update_spec_doc(phy_type: str, doc_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """Metadata edit (relevant_sections is the common one). The stored file
    (if any) and the id are untouched; editable fields are merged over the
    existing record and re-validated."""
    validate_phy_type(phy_type)
    validate_doc_id(doc_id)
    phy_dir = find_doc_dir(phy_type, doc_id)
    if phy_dir is None:
        raise FileNotFoundError(f"spec doc {phy_type}/{doc_id} not found")
    meta_path = phy_dir / f"{doc_id}.meta.json"
    stored = json.loads(meta_path.read_text())
    for key in ("title", "standard_family", "version", "user_notes",
                "relevant_sections", "applies_to_workspaces"):
        if key in metadata:
            stored[key] = metadata[key]
    # Re-validate the merged record (source stays as stored - a file source's
    # path already points at the copy, which _validate_metadata would reject
    # as relative, so exempt it).
    check = dict(stored)
    if (stored.get("source") or {}).get("type") == "file":
        check["source"] = {"type": "reference", "citation": "stored file"}
    problems = _validate_metadata(check)
    if problems:
        raise ValueError("spec-doc problems: " + "; ".join(problems))
    stored["updated_at"] = _now()
    meta_path.write_text(json.dumps(stored, indent=2))
    return stored


def delete_spec_doc(phy_type: str, doc_id: str) -> None:
    """Remove the metadata AND the stored file copy (any extension)."""
    validate_phy_type(phy_type)
    validate_doc_id(doc_id)
    phy_dir = find_doc_dir(phy_type, doc_id)
    if phy_dir is None:
        raise FileNotFoundError(f"spec doc {phy_type}/{doc_id} not found")
    (phy_dir / f"{doc_id}.meta.json").unlink()
    for f in phy_dir.glob(f"{doc_id}.*"):
        if f.name != f"{doc_id}.meta.json" and f.suffix != ".json":
            f.unlink()


def set_answer(phy_type: str, answer: str) -> dict[str, Any]:
    """Record the per-PHY spec-doc answer - what suppresses the banner."""
    validate_phy_type(phy_type)
    if answer not in ANSWERS:
        raise ValueError(f"answer must be one of: {', '.join(ANSWERS)}")
    phy_dir = _phy_dir(phy_type)
    phy_dir.mkdir(parents=True, exist_ok=True)
    status = {"answer": answer, "answered_at": _now()}
    (phy_dir / "_status.json").write_text(json.dumps(status, indent=2))
    return status


def get_status(phy_type: str) -> dict[str, Any]:
    """The per-PHY answer record, {"answer": None} when never answered.
    Searched across roots (current first)."""
    for root in all_spec_docs_roots():
        path = root / phy_type / "_status.json"
        if path.is_file():
            try:
                return json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
    return {"answer": None, "answered_at": None}


def list_spec_docs(phy_type: str | None = None) -> dict[str, Any]:
    """All docs' metadata (newest first) + the per-PHY _status records,
    across working-dir roots (first root a doc id appears in wins)."""
    phys = [phy_type] if phy_type else list(VALID_PHY_TYPES)
    docs: list[dict[str, Any]] = []
    status: dict[str, Any] = {}
    for phy in phys:
        seen: set[str] = set()
        for root in all_spec_docs_roots():
            phy_dir = root / phy
            if not phy_dir.is_dir():
                continue
            for meta_path in sorted(phy_dir.glob("*.meta.json")):
                try:
                    meta = json.loads(meta_path.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                doc_id = meta.get("id") or meta_path.name[: -len(".meta.json")]
                if doc_id in seen:
                    continue
                seen.add(doc_id)
                docs.append(meta)
        status[phy] = get_status(phy)
    docs.sort(key=lambda d: d.get("added_at") or "", reverse=True)
    return {"docs": docs, "status": status}


def resolve_docs_for_generation(
    phy_type: str, spec_doc_ids: list[str]
) -> list[dict[str, Any]]:
    """Metadata + resolved absolute file path per requested doc id, for the
    generation prompt. Raises ValueError listing every unknown id."""
    validate_phy_type(phy_type)
    unknown: list[str] = []
    out: list[dict[str, Any]] = []
    for doc_id in spec_doc_ids:
        try:
            validate_doc_id(doc_id)
        except ValueError:
            unknown.append(str(doc_id))
            continue
        phy_dir = find_doc_dir(phy_type, doc_id)
        if phy_dir is None:
            unknown.append(doc_id)
            continue
        meta = json.loads((phy_dir / f"{doc_id}.meta.json").read_text())
        source = meta.get("source") or {}
        if source.get("type") == "file":
            rel = source.get("path") or ""
            # stored as spec_docs/<phy>/<file> relative to the working dir
            meta["abs_path"] = str(phy_dir / Path(rel).name)
        out.append(meta)
    if unknown:
        raise ValueError("unknown spec_doc_ids: " + ", ".join(unknown))
    return out


# ---------------------------------------------------------------------------
# Chat-context rendering (consumption mode 1)


def _render_one_doc(meta: dict[str, Any], notes_cap: int | None = None) -> str:
    title = meta.get("title") or meta.get("id") or "untitled"
    family = meta.get("standard_family") or "unspecified family"
    version = meta.get("version") or ""
    header = f"- {title} ({family}{', ' + version if version else ''})"
    lines = [header]
    notes = (meta.get("user_notes") or "").strip()
    if notes:
        if notes_cap is not None and len(notes) > notes_cap:
            notes = notes[: max(notes_cap, 0)].rstrip() + "..." if notes_cap > 0 else ""
        if notes:
            lines.append(f"  notes: {notes}")
    for sec in meta.get("relevant_sections") or []:
        lines.append(f"  section: {sec.get('label', '')} - {sec.get('locator', '')}")
    return "\n".join(lines)


def render_chat_context(spec_docs: list[dict[str, Any]] | None) -> str:
    """The compact metadata block for chat prompts. Empty string when there
    are no docs. Capped at CHAT_CONTEXT_CAP chars total: user_notes are
    truncated first, then docs beyond the first CHAT_CONTEXT_MAX_DOCS are
    dropped with an 'N more docs attached' line."""
    docs = [d for d in (spec_docs or []) if isinstance(d, dict)]
    if not docs:
        return ""
    preamble = (
        "## Specification documents on file (metadata only)\n"
        "These documents are attached to this workspace as the requirements\n"
        "source. You CANNOT read the documents in this chat - only generation\n"
        "runs can. Cite the section locators below when advising, and say so\n"
        "explicitly when an answer really requires reading the document.\n"
    )
    kept = docs[:CHAT_CONTEXT_MAX_DOCS]
    dropped = len(docs) - len(kept)
    suffix = f"\n({dropped} more docs attached)" if dropped else ""

    for notes_cap in (None, 200, 0):
        body = "\n".join(_render_one_doc(d, notes_cap) for d in kept)
        block = preamble + body + suffix
        if len(block) <= CHAT_CONTEXT_CAP:
            return block
    # Still over: drop docs from the end until it fits.
    while len(kept) > 1:
        kept = kept[:-1]
        dropped = len(docs) - len(kept)
        body = "\n".join(_render_one_doc(d, 0) for d in kept)
        block = preamble + body + f"\n({dropped} more docs attached)"
        if len(block) <= CHAT_CONTEXT_CAP:
            return block
    return block[:CHAT_CONTEXT_CAP]
