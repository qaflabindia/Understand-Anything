"""Link explicit Markdown and manifest references within graph inventory."""
from __future__ import annotations

import json
import posixpath
import re
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote, urlsplit

_FILE_TYPES = {"file", "config", "document", "service", "pipeline", "schema", "resource", "endpoint", "table"}
_PATH_FIELDS = {"path", "file", "files", "entry", "main", "module", "types", "source", "sources", "extends", "include", "fixtures", "schemas", "specification"}
_PATH_MAP_FIELDS = {"exports", "imports", "paths"}
_LINK = re.compile(r"\[[^\]\n]+\]\(\s*(?:<([^>\n]+)>|([^\s)]+))(?:\s+[^)\n]*)?\)")
_BACKTICK = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_MAX_BYTES = 8 * 1024 * 1024


def _inventory(graph: dict[str, Any], root: Path) -> dict[str, str]:
    inventory: dict[str, str] = {}
    for node in graph.get("nodes", []):
        rel = node.get("filePath")
        kind = node.get("type")
        node_id = node.get("id")
        if kind not in _FILE_TYPES or not isinstance(rel, str) or not rel:
            continue
        if node_id != f"{kind}:{rel}":
            continue
        if (rel.startswith(("/", "../")) or rel == ".." or "\\" in rel
                or posixpath.normpath(rel) != rel):
            continue
        source = root / rel
        try:
            # Comparing the lexical and resolved paths excludes symlinked
            # sources, including aliases into another inventoried directory.
            if source.resolve(strict=True) != source or not source.is_file():
                continue
        except (OSError, RuntimeError, ValueError):
            continue
        if rel not in inventory or kind == "file":
            inventory[rel] = node_id
    return inventory


def _resolve(value: str, origin: str, inventory: dict[str, str], mode: str) -> str | None:
    value = value.strip()
    if mode == "backtick":
        value = re.sub(r"(?:::[\w.]+|:\d+(?:-\d+)?)$", "", value)
    try:
        url = urlsplit(value)
    except ValueError:
        return None
    if url.scheme or url.netloc or url.query or value.startswith("//"):
        return None
    if mode == "json" and url.fragment:
        return None
    rel = unquote(url.path)
    if not rel or any(char in rel for char in ("\\", "\x00", "\n", "\r", "*", "?", "{", "}", "$")):
        return None
    if rel.startswith("/"):
        if mode == "json":
            return None
        candidates = {posixpath.normpath(rel.lstrip("/"))}
    else:
        candidates = {posixpath.normpath(posixpath.join(posixpath.dirname(origin), rel))}
        # Inline code often names a repository-relative path. A Markdown link
        # always follows URL-relative semantics. Never search by basename.
        if mode != "link" and "/" in rel and not rel.startswith(("./", "../")):
            candidates.add(posixpath.normpath(rel))
    matches = candidates.intersection(inventory)
    if len(matches) != 1 or origin in matches:
        return None
    return next(iter(matches))


def _markdown_paths(source: str) -> Iterator[tuple[str, int, str]]:
    fence = ""
    for number, line in enumerate(source.splitlines(), 1):
        match = _FENCE.match(line)
        if fence:
            if match and match[1].startswith(fence) and not match[2].strip():
                fence = ""
            continue
        if match:
            fence = match[1]
            continue
        for link in _LINK.finditer(line):
            yield link[1] or link[2], number, "link"
        for code in _BACKTICK.finditer(line):
            yield code[1], number, "backtick"


def _manifest_paths(data: Any, field: str = "", path_value: bool = False,
                    path_map: bool = False) -> Iterator[tuple[str, str]]:
    if isinstance(data, str) and (path_value or path_map):
        yield data, field
    elif isinstance(data, list):
        for index, entry in enumerate(data):
            yield from _manifest_paths(entry, f"{field}[{index}]", path_value, path_map)
    elif isinstance(data, dict):
        for key, value in data.items():
            yield from _manifest_paths(value, f"{field}.{key}" if field else key,
                                       key in _PATH_FIELDS, path_map or key in _PATH_MAP_FIELDS)


def link_source_references(graph: dict[str, Any], project_root: Path) -> tuple[int, list[str]]:
    """Append missing literal references using only existing whole-file nodes."""
    root = Path(project_root).resolve()
    inventory = _inventory(graph, root)
    by_id = {node.get("id"): node for node in graph.get("nodes", [])}
    edges = graph.setdefault("edges", [])
    existing = {(edge.get("source"), edge.get("target"), edge.get("type")) for edge in edges}
    added = 0
    report = []
    for rel, source_id in sorted(inventory.items()):
        suffix = Path(rel).suffix.lower()
        if suffix not in {".md", ".mdx", ".json"}:
            continue
        node = by_id[source_id]
        if suffix != ".json" and (node.get("type") != "document" or "imported-skill" in node.get("tags", [])):
            continue
        try:
            source = root / rel
            if source.stat().st_size > _MAX_BYTES:
                report.append(f"  Source references skipped oversized file {rel}")
                continue
            content = source.read_text(encoding="utf-8")
            if suffix == ".json":
                references = ((value, f"JSON field {field}", "json")
                              for value, field in _manifest_paths(json.loads(content)))
            else:
                references = ((value, f"line {line}", mode) for value, line, mode in _markdown_paths(content))
            for value, evidence, mode in references:
                target = _resolve(value, rel, inventory, mode)
                if target is None:
                    continue
                kind = "documents" if mode != "json" else "depends_on"
                if mode == "json" and "archived-evidence" in node.get("tags", []):
                    kind = "related"
                key = source_id, inventory[target], kind
                if key in existing:
                    continue
                existing.add(key)
                edges.append({
                    "source": key[0], "target": key[1], "type": kind,
                    "direction": "forward", "weight": 0.6 if kind == "depends_on" else 0.5,
                    "description": f"Source {rel}, {evidence}, references {target}",
                })
                added += 1
        except (OSError, UnicodeError, ValueError, RecursionError) as error:
            report.append(f"  Source references skipped {rel} ({type(error).__name__})")
    return added, [f"  {added} literal source reference edge(s) added", *report]
