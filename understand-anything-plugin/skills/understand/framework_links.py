"""Conservative, inventory-bound links for implicit framework relationships.

No framework is imported and no project code is evaluated. Unsupported/dynamic
configuration is skipped rather than guessed. The public linker mutates edges
only; every endpoint must already exist in the supplied graph.
"""
from __future__ import annotations

import ast
import json
import posixpath
import re
import shlex
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

_WHOLE_TYPES = {"file", "config", "document", "service", "pipeline", "resource", "schema", "table", "endpoint"}
_WEIGHTS = {"contains": 1.0, "depends_on": 0.6, "configures": 0.6, "serves": 0.7, "routes": 0.6, "deploys": 0.7}
_MAX_BYTES = 2 * 1024 * 1024


def _relative(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return None
    raw = raw.replace("\\", "/")
    p = PurePosixPath(raw)
    if p.is_absolute() or re.match(r"^[A-Za-z]:", raw) or ".." in p.parts:
        return None
    return p.as_posix() if p.as_posix() != "." else None


class _Links:
    def __init__(self, graph: dict[str, Any], root: Path):
        self.graph = graph
        self.root = root.resolve()
        self.nodes = {n.get("id"): n for n in graph.get("nodes", []) if isinstance(n, dict) and isinstance(n.get("id"), str)}
        candidates: dict[str, list[str]] = {}
        for nid, n in self.nodes.items():
            p = _relative(n.get("filePath"))
            if p and n.get("type") in _WHOLE_TYPES and nid == f"{n['type']}:{n['filePath']}":
                candidates.setdefault(p, []).append(nid)
        # A conflicting whole-file owner is ambiguous; child symbols never qualify.
        self.files = {p: ids[0] for p, ids in candidates.items() if len(ids) == 1}
        self.warnings = Counter()
        self.warnings["ambiguous whole-file paths"] = sum(len(v) > 1 for v in candidates.values())
        self.cache: dict[str, str | None] = {}
        self.seen = {(e.get("source"), e.get("target"), e.get("type"), e.get("direction", "forward")) for e in graph.get("edges", []) if isinstance(e, dict)}
        self.added = Counter()

    def disk(self, p: str) -> Path | None:
        if p == ".":
            return self.root
        rel = _relative(p)
        if rel is None:
            return None
        try:
            resolved = (self.root / rel).resolve(strict=True)
            resolved.relative_to(self.root)
            return resolved
        except (OSError, RuntimeError, ValueError):
            return None

    def read(self, p: str) -> str | None:
        if p not in self.files:
            return None
        if p not in self.cache:
            target = self.disk(p)
            try:
                if target is None or not target.is_file() or target.stat().st_size > _MAX_BYTES:
                    raise ValueError("unreadable or outside project")
                self.cache[p] = target.read_text(encoding="utf-8")
            except (OSError, UnicodeError, ValueError):
                self.cache[p] = None
                self.warnings["unreadable/unsafe source files"] += 1
        return self.cache[p]

    def evidence(self, p: str, marker: str) -> str:
        text = self.read(p) or ""
        offset = text.find(marker)
        line = text.count("\n", 0, offset) + 1 if offset >= 0 else 1
        return f"{p}:{line}"

    def add(self, source: str | None, target: str | None, kind: str, family: str, why: str, p: str, marker: str) -> None:
        key = (source, target, kind, "forward")
        if source not in self.nodes or target not in self.nodes or source == target or key in self.seen:
            return
        self.graph.setdefault("edges", []).append({
            "source": source, "target": target, "type": kind,
            "direction": "forward", "weight": _WEIGHTS[kind],
            "description": f"Framework ({family}): {why} [{self.evidence(p, marker)}]",
        })
        self.seen.add(key)
        self.added[family] += 1

    def file_link(self, a: str, b: str, kind: str, family: str, why: str, p: str, marker: str) -> None:
        self.add(self.files.get(a), self.files.get(b), kind, family, why, p, marker)


def _join(parent: str, child: str) -> str:
    return posixpath.normpath(posixpath.join(parent, child))


def _js_strings(text: str):
    """Yield non-interpolated string literals, ignoring comments/string interiors."""
    i = 0
    while i < len(text):
        if text.startswith("//", i):
            end = text.find("\n", i + 2); i = len(text) if end < 0 else end + 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2); i = len(text) if end < 0 else end + 2
        elif text[i] in "\"'`":
            start = i; quote = text[i]; i += 1; value = []; dynamic = False
            while i < len(text) and text[i] != quote:
                if text[i] == "\\":
                    # Escaped paths are deliberately not interpreted as literal URLs.
                    dynamic = True; i += 2; continue
                if quote == "`" and text.startswith("${", i):
                    dynamic = True
                value.append(text[i]); i += 1
            if i < len(text) and not dynamic:
                yield start, "".join(value)
            i += 1
        else:
            i += 1


def _sveltekit(link: _Links) -> None:
    configs: dict[str, list[str]] = {}
    for p in link.files:
        if PurePosixPath(p).name in {"svelte.config.js", "svelte.config.mjs", "svelte.config.cjs", "svelte.config.ts"}:
            configs.setdefault(str(PurePosixPath(p).parent), []).append(p)
    for app, choices in sorted(configs.items()):
        if len(choices) != 1:
            link.warnings["ambiguous SvelteKit configurations"] += 1; continue
        config = choices[0]; cfg = link.read(config)
        if cfg is None or not re.search(r"\bkit\s*:", cfg):
            continue
        # Spreads or file overrides can change roots. Do not evaluate project JS.
        if re.search(r"\bfiles\s*:|\.\.\.", cfg):
            link.warnings["custom/dynamic SvelteKit roots"] += 1; continue
        routes = _join(app, "src/routes"); static = _join(app, "static")
        prefix = routes + "/"
        route_files = sorted(p for p in link.files if p.startswith(prefix))
        if not route_files:
            continue
        resets = {str(PurePosixPath(p).parent) for p in route_files if re.match(r"\+(?:layout|page)@", PurePosixPath(p).name)}
        layouts = {str(PurePosixPath(p).parent): p for p in route_files if PurePosixPath(p).name == "+layout.svelte" and link.read(p) is not None}

        def nearest(directory: str, above: bool = False) -> str | None:
            current = str(PurePosixPath(directory).parent) if above else directory
            while current == routes or current.startswith(prefix):
                if current in resets:
                    return None
                candidate = _join(current, "+layout.svelte")
                # Even an excluded nearer layout stops a direct composition guess.
                if current not in layouts and link.disk(candidate) is not None:
                    return None
                if current in layouts:
                    owner = layouts[current]; content = link.read(owner) or ""
                    content = re.sub(r"<!--.*?-->", "", content, flags=re.S)
                    if re.search(r"\{@render\s+children\s*\(|<slot(?:\s|/|>)", content):
                        return owner
                    return None
                current = str(PurePosixPath(current).parent)
            return None

        def server_peer(component: str, stem: str) -> None:
            directory = str(PurePosixPath(component).parent)
            peers = [p for ext in ("ts", "js") if (p := _join(directory, f"{stem}.server.{ext}")) in link.files]
            if len(peers) != 1:
                return
            peer = peers[0]; content = link.read(peer)
            if content and re.search(r"\bexport\s+(?:const\s+(?:load|actions)\b|(?:async\s+)?function\s+load\b|\{[^}]*\bload\b)", content):
                link.file_link(component, peer, "depends_on", "sveltekit", "same-route server load/actions", peer, "export")

        pages = {}
        for p in route_files:
            name = PurePosixPath(p).name; directory = str(PurePosixPath(p).parent)
            if name == "+page.svelte" and link.read(p) is not None:
                server_peer(p, "+page")
                owner = nearest(directory)
                if owner:
                    link.file_link(owner, p, "contains", "sveltekit", "layout renders descendant route through children/slot", owner, "children" if "children" in (link.read(owner) or "") else "<slot")
                relative = directory[len(routes):].strip("/")
                if not any(c in relative for c in "[]()"):
                    pages["/" + relative if relative else "/"] = p
            elif name == "+layout.svelte":
                server_peer(p, "+layout")
                owner = nearest(directory, above=True)
                if owner:
                    link.file_link(owner, p, "contains", "sveltekit", "parent layout renders nested layout", owner, "children" if "children" in (link.read(owner) or "") else "<slot")
        hook_choices = [p for ext in ("ts", "js") if (p := _join(app, f"src/hooks.server.{ext}")) in link.files]
        if len(hook_choices) == 1:
            hook = hook_choices[0]; source = link.read(hook)
            if source and re.search(r"\bexport\s+(?:const|(?:async\s+)?function)\s+handle\b", source):
                for p in route_files:
                    if re.fullmatch(r"\+(?:(?:page|layout)\.server|server)\.[jt]s", PurePosixPath(p).name) and (body := link.read(p)) and re.search(r"\blocals\b", body):
                        link.file_link(p, hook, "depends_on", "sveltekit", "server route consumes request locals supplied by global handle", hook, "handle")
        html = _join(app, "src/app.html"); root_layout = layouts.get(routes)
        if html in link.files and (body := link.read(html)) and "%sveltekit.body%" in body:
            link.file_link(config, html, "configures", "sveltekit", "default application template and adapter configuration", config, "kit")
            if root_layout:
                link.file_link(html, root_layout, "contains", "sveltekit", "application template inserts the rendered root layout", html, "%sveltekit.body%")
        resources = sorted(p for p in link.files if p.startswith(static + "/") and link.disk(p) is not None)
        for p in resources:
            link.file_link(config, p, "serves", "sveltekit", "default static directory publishes this existing resource", config, "kit")
        # Paths/base customization makes literal URL-to-route matching ambiguous.
        urls_allowed = not re.search(r"\bpaths\s*:", cfg)
        endpoints = {}
        for p in route_files:
            if PurePosixPath(p).name in {"+server.ts", "+server.js"}:
                rel = str(PurePosixPath(p).parent)[len(routes):].strip("/")
                url = "/" + rel if rel else "/"
                if not any(c in rel for c in "[]()"):
                    endpoints.setdefault(url, []).append(p)
        for p in sorted(link.files):
            if not p.startswith(_join(app, "src") + "/") or not p.endswith((".svelte", ".html", ".ts", ".js")):
                continue
            body = link.read(p)
            if body is None:
                continue
            if p.endswith((".svelte", ".html")):
                markup = re.sub(r"<!--.*?-->", "", body, flags=re.S)
                markup = re.sub(r"(<script\b[^>]*>).*?</script>", r"\1</script>", markup, flags=re.S)
                for tag in re.finditer(r"<(?![!/{])[^>]+>", markup):
                    for attr in re.finditer(r"\b(src|href)\s*=\s*([\"'])([^\"']+)\2", tag.group()):
                        value = attr.group(3); target_url = value.split("?", 1)[0].split("#", 1)[0]
                        if any(c in target_url for c in "{}") or target_url.startswith("//"):
                            continue
                        local = target_url.removeprefix("%sveltekit.assets%")
                        resource = _join(static, local.lstrip("/")) if local.startswith("/") else ""
                        if resource in resources:
                            link.file_link(p, resource, "depends_on", "sveltekit", "literal static resource reference", p, attr.group())
                        elif urls_allowed and attr.group(1) == "href" and target_url in pages:
                            link.file_link(p, pages[target_url], "routes", "sveltekit", "literal page navigation URL", p, attr.group())
                scripts = "\n".join(re.findall(r"<script\b[^>]*>(.*?)</script>", re.sub(r"<!--.*?-->", "", body, flags=re.S), flags=re.S))
            else:
                scripts = body
            if urls_allowed:
                for offset, value in _js_strings(scripts):
                    if not value.startswith("/") or value.startswith("//"):
                        continue
                    url = value.split("?", 1)[0].split("#", 1)[0]; preceding = scripts[max(0, offset-120):offset]
                    if url in pages and re.search(r"(?:\bhref\s*:|\bgoto\s*\(|\bredirect\s*\(\s*\d+\s*,)\s*$", preceding):
                        link.file_link(p, pages[url], "routes", "sveltekit", "literal navigation/redirect declaration", p, value)
                    if len(endpoints.get(url, [])) == 1 and re.search(r"\bfetch\s*\(\s*$", preceding):
                        link.file_link(p, endpoints[url][0], "depends_on", "sveltekit", "literal fetch URL resolves to existing endpoint", p, value)
        for p in resources:
            if p.endswith((".webmanifest", "/manifest.json")):
                try:
                    manifest = json.loads(link.read(p) or "")
                except (ValueError, TypeError):
                    continue
                start = manifest.get("start_url") if isinstance(manifest, dict) else None
                if urls_allowed and isinstance(start, str) and start in pages:
                    link.file_link(p, pages[start], "routes", "sveltekit", "manifest start_url selects page", p, "start_url")
        tsconfig = _join(app, "tsconfig.json"); ambient = _join(app, "src/app.d.ts")
        if tsconfig in link.files and ambient in link.files and (ts := link.read(tsconfig)) and ".svelte-kit/tsconfig.json" in ts and (declaration := link.read(ambient)) and "namespace App" in declaration:
            link.file_link(tsconfig, ambient, "configures", "sveltekit", "SvelteKit TypeScript project includes ambient App declarations", tsconfig, "extends")


# A small OpenStep property-list parser, not a project-code evaluator.
def _openstep(text: str) -> dict[str, Any]:
    tokens = []; i = 0
    while i < len(text):
        if text[i].isspace(): i += 1; continue
        if text.startswith("//", i):
            end = text.find("\n", i); i = len(text) if end < 0 else end+1; continue
        if text.startswith("/*", i):
            end = text.find("*/", i+2)
            if end < 0: raise ValueError("unclosed comment")
            i = end+2; continue
        if text[i] in "{}()=;,": tokens.append(text[i]); i += 1; continue
        if text[i] == '"':
            start = i; i += 1
            while i < len(text) and text[i] != '"': i += 2 if text[i] == "\\" else 1
            if i == len(text): raise ValueError("unclosed string")
            tokens.append(json.loads(text[start:i+1])); i += 1; continue
        end = i
        while end < len(text) and not text[end].isspace() and text[end] not in '{}()=;,"': end += 1
        if end == i: raise ValueError("bad token")
        tokens.append(text[i:end]); i = end
    cursor = 0
    def value(depth=0):
        nonlocal cursor
        if depth > 80 or cursor >= len(tokens): raise ValueError("invalid plist")
        token = tokens[cursor]; cursor += 1
        if token == "{":
            result = {}
            while cursor < len(tokens) and tokens[cursor] != "}":
                key = tokens[cursor]; cursor += 1
                if cursor >= len(tokens) or tokens[cursor] != "=": raise ValueError("missing =")
                cursor += 1; item = value(depth+1)
                if key in result: raise ValueError("duplicate plist key")
                result[key] = item
                if cursor >= len(tokens) or tokens[cursor] != ";": raise ValueError("missing ;")
                cursor += 1
            if cursor >= len(tokens): raise ValueError("unclosed object")
            cursor += 1; return result
        if token == "(":
            result = []
            while cursor < len(tokens) and tokens[cursor] != ")":
                result.append(value(depth+1))
                if cursor < len(tokens) and tokens[cursor] == ",": cursor += 1
                elif cursor < len(tokens) and tokens[cursor] != ")": raise ValueError("missing ,")
            if cursor >= len(tokens): raise ValueError("unclosed array")
            cursor += 1; return result
        return token
    result = value()
    if cursor != len(tokens) or not isinstance(result, dict): raise ValueError("invalid root")
    return result


def _string_list(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _xcode(link: _Links) -> None:
    for project in sorted(p for p in link.files if p.endswith(".xcodeproj/project.pbxproj")):
        body = link.read(project)
        if body is None: continue
        try:
            objects = _openstep(body).get("objects", {})
            if not isinstance(objects, dict): raise ValueError("missing objects")
        except (ValueError, TypeError, RecursionError):
            link.warnings["unsupported Xcode projects"] += 1; continue
        base = str(PurePosixPath(project).parent.parent)
        parents: dict[str, list[str]] = {}
        for key, obj in objects.items():
            if isinstance(obj, dict):
                for child in _string_list(obj.get("children")): parents.setdefault(child, []).append(key)
        roots = {o.get("mainGroup") for o in objects.values() if isinstance(o, dict) and o.get("isa") == "PBXProject" and isinstance(o.get("mainGroup"), str)}
        def resolve(key: str, seen=frozenset()) -> str | None:
            if key in seen: return None
            obj = objects.get(key)
            if not isinstance(obj, dict): return None
            raw = obj.get("path", "")
            if not isinstance(raw, str) or "$" in raw or PurePosixPath(raw).is_absolute(): return None
            tree = obj.get("sourceTree", "<group>")
            if tree == "SOURCE_ROOT" or key in roots: parent = base
            elif tree == "<group>" and len(parents.get(key, [])) == 1: parent = resolve(parents[key][0], seen | {key})
            else: return None
            if parent is None: return None
            joined = _join(parent, raw)
            return joined if _relative(joined) is not None and link.disk(joined) is not None else None
        for target in objects.values():
            if not isinstance(target, dict) or target.get("isa") != "PBXNativeTarget": continue
            sources = []; catalogues = []
            for phase_id in _string_list(target.get("buildPhases")):
                phase = objects.get(phase_id, {})
                if not isinstance(phase, dict) or phase.get("isa") not in {"PBXSourcesBuildPhase", "PBXResourcesBuildPhase"}: continue
                for build_id in _string_list(phase.get("files")):
                    build = objects.get(build_id, {}); ref = build.get("fileRef") if isinstance(build, dict) else None
                    source = resolve(ref) if isinstance(ref, str) else None
                    if source is None: continue
                    if phase["isa"] == "PBXSourcesBuildPhase":
                        sources.append(source)
                        link.file_link(project, source, "configures", "xcode", "explicit Sources build-phase membership", project, build_id)
                    elif source.endswith(".xcassets"):
                        catalogues.append(source); rootmeta = _join(source, "Contents.json")
                        for resource in sorted(p for p in link.files if p.startswith(source + "/") and p.endswith("/Contents.json") and link.disk(p) is not None):
                            link.file_link(project, resource, "depends_on", "xcode", "metadata in explicitly included asset catalogue", project, build_id)
                            if resource != rootmeta:
                                link.file_link(rootmeta, resource, "contains", "xcode", "asset catalogue contains this checked metadata set", resource, '"info"')
                    else:
                        link.file_link(project, source, "depends_on", "xcode", "explicit Resources build-phase membership", project, build_id)
            entries = [p for p in sources if p.endswith(".swift") and (src := link.read(p)) and re.search(r"@main\b", src)]
            config_list = target.get("buildConfigurationList")
            configs = objects.get(config_list, {}) if isinstance(config_list, str) else {}
            config_ids = _string_list(configs.get("buildConfigurations")) if isinstance(configs, dict) else []
            for config_id in config_ids:
                config = objects.get(config_id, {}); settings = config.get("buildSettings", {}) if isinstance(config, dict) else {}
                if not isinstance(settings, dict): continue
                raw = settings.get("INFOPLIST_FILE")
                if isinstance(raw, str):
                    raw = re.sub(r"^\$\((?:SRCROOT|PROJECT_DIR)\)/", "", raw)
                    raw = re.sub(r"^\$\{(?:SRCROOT|PROJECT_DIR)\}/", "", raw)
                    if "$" not in raw and not PurePosixPath(raw).is_absolute():
                        plist = _join(base, raw)
                        if link.disk(plist) is not None:
                            link.file_link(project, plist, "depends_on", "xcode", "build configuration selects INFOPLIST_FILE", project, "INFOPLIST_FILE")
                            if len(entries) == 1:
                                link.file_link(plist, entries[0], "configures", "xcode", "selected bundle metadata configures target app entry point", project, "INFOPLIST_FILE")
        # Embedded workspace self: references are explicit and unambiguous.
        workspace = str(PurePosixPath(project).parent / "project.xcworkspace/contents.xcworkspacedata")
        if workspace in link.files and (xml := link.read(workspace)):
            try:
                tree = ET.fromstring(xml)
                if any(ref.get("location") == "self:" for ref in tree.iter("FileRef")):
                    link.file_link(workspace, project, "depends_on", "xcode", "embedded workspace self reference", workspace, "self:")
            except ET.ParseError:
                link.warnings["unsupported Xcode workspaces"] += 1
        # XcodeGen default specification: only direct, literal declarations.
        for spec in (_join(base, "project.yml"), _join(base, "project.yaml")):
            if spec not in link.files or not (text := link.read(spec)): continue
            try:
                fields = _yaml_fields(_yaml_lines(text))
            except ValueError:
                link.warnings["unsupported XcodeGen specifications"] += 1
                continue
            targets = fields.get("targets")
            if targets:
                for _, target in _yaml_fields(targets[2]).items():
                    options = _yaml_fields(target[2]); info = options.get("info")
                    if info:
                        path = _yaml_fields(info[2]).get("path")
                        scalar = _scalar(path[0]) if path else None
                        if isinstance(scalar, str) and "$" not in scalar:
                            resolved = _join(base, scalar)
                            if link.disk(resolved) is not None:
                                link.file_link(spec, resolved, "configures", "xcode", "XcodeGen target declares property-list input", spec, scalar)
                # Presence alone is not proof of a generated project association;
                # bind only a matching application target name.
                names = set(_yaml_fields(targets[2]))
                if any(isinstance(o, dict) and o.get("isa") == "PBXNativeTarget" and o.get("name") in names for o in objects.values()):
                    link.file_link(spec, project, "configures", "xcode", "XcodeGen specification declares matching target", spec, "targets:")


# Restricted YAML reader: mappings, scalar/flow values and simple sequences.
# It does not implement aliases, merges, tags, interpolation or arbitrary YAML.
def _yaml_lines(text: str) -> list[tuple[int, str, int]]:
    lines = []
    for number, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw[:len(raw)-len(raw.lstrip())]: raise ValueError("YAML tabs")
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            lines.append((len(raw)-len(raw.lstrip()), stripped, number))
    return lines


def _yaml_fields(lines: list[tuple[int, str, int]]) -> dict[str, tuple[str, int, list]]:
    if not lines: return {}
    indent = min(x[0] for x in lines); result = {}; duplicate = set()
    starts = [(i, re.match(r"([A-Za-z0-9_.-]+):(?:\s+(.*))?$", content), number) for i, (depth, content, number) in enumerate(lines) if depth == indent]
    for cursor, (i, match, number) in enumerate(starts):
        if not match: continue
        key = match[1]; stop = starts[cursor+1][0] if cursor+1 < len(starts) else len(lines)
        if key in result: duplicate.add(key)
        result[key] = (match[2] or "", number, lines[i+1:stop])
    for key in duplicate: result.pop(key, None)
    return result


def _flow_parts(raw: str) -> list[str]:
    result = []; start = 0; depth = 0; quote = None
    for i, c in enumerate(raw):
        if quote:
            if c == quote and (i == 0 or raw[i-1] != "\\"): quote = None
        elif c in "\"'": quote = c
        elif c in "[{": depth += 1
        elif c in "]}": depth -= 1
        elif c == "," and depth == 0: result.append(raw[start:i].strip()); start = i+1
    result.append(raw[start:].strip()); return result


def _scalar(raw: str) -> Any:
    raw = re.sub(r"\s+#.*$", "", raw).strip()
    if not raw or raw[0] in "&*!" or raw.startswith("<<:") or "${" in raw: return None
    if raw.startswith("{") and raw.endswith("}"):
        result = {}
        for item in _flow_parts(raw[1:-1]):
            match = re.fullmatch(r"([A-Za-z0-9_.-]+):\s*(.+)", item)
            if not match or match[1] in result: return None
            result[match[1]] = _scalar(match[2])
        return result
    if raw[0] in "[\"'":
        try: return json.loads(raw)
        except ValueError:
            try: return ast.literal_eval(raw)
            except (ValueError, SyntaxError): return None
    return raw


def _field_values(field: tuple | None) -> Any:
    if field is None: return None
    raw, _, children = field
    if raw.startswith("[") and not raw.rstrip().endswith("]"):
        raw += " " + " ".join(c for _, c, _ in children)
    if raw: return _scalar(raw)
    values = [_scalar(content[2:]) for _, content, _ in children if content.startswith("- ")]
    return values or None


def _script_arg(command: Any) -> str | None:
    try: argv = shlex.split(command) if isinstance(command, str) else command
    except ValueError: return None
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv): return None
    if any(any(c in a for c in "$`\n") for a in argv): return None
    if argv[0] == "exec": argv = argv[1:]
    if not argv: return None
    program = posixpath.basename(argv[0]); rest = argv[1:]
    if program in {"sh", "bash"} and rest and rest[0] in {"-c", "-lc"}:
        if len(rest) != 2 or re.search(r"[;&|<>]", rest[1]): return None
        return _script_arg(rest[1])
    if program == "npx":
        while rest and rest[0] in {"-y", "--yes"}: rest = rest[1:]
        return _script_arg(rest)
    allowed = {"node", "nodejs", "tsx", "ts-node", "bun", "deno", "python", "python3", "ruby", "php", "sh", "bash"}
    if program not in allowed:
        return argv[0] if re.search(r"\.(?:[cm]?js|tsx?|py|rb|php|sh)$", argv[0]) else None
    safe_flags = {"--experimental-strip-types", "--enable-source-maps", "--no-warnings", "-u", "-B", "-O", "-OO"}
    while rest and rest[0] in safe_flags: rest = rest[1:]
    if program == "deno" and rest and rest[0] == "run": rest = rest[1:]
    if not rest or rest[0].startswith("-") or not re.search(r"\.(?:[cm]?js|tsx?|py|rb|php|sh)$", rest[0]): return None
    return rest[0]


def _docker_mapping(link: _Links, context: str, dockerfile: str, target: str | None) -> tuple[str, list[tuple[str, str, bool]]] | None:
    text = link.read(dockerfile)
    if text is None: return None
    stages = {}; current = None
    for raw in re.sub(r"\\\n", " ", text).splitlines():
        match = re.match(r"\s*(FROM|WORKDIR|COPY)\s+(.+?)\s*$", raw, re.I)
        if not match: continue
        op, value = match[1].upper(), match[2]
        if "$" in value: continue
        if op == "FROM":
            parts = value.split(); base = stages.get(parts[0]); current = {"wd": base["wd"] if base else "/", "copies": list(base["copies"]) if base else []}
            if len(parts) == 3 and parts[1].upper() == "AS": stages[parts[2]] = current
        elif current is not None and op == "WORKDIR": current["wd"] = _join(current["wd"], value)
        elif current is not None and op == "COPY":
            if re.search(r"(?:^|\s)--from(?:=|\s)", value): continue
            value = re.sub(r"^(?:--[a-z-]+=[^\s]+\s+)+", "", value)
            try: args = json.loads(value) if value.startswith("[") else shlex.split(value)
            except ValueError: continue
            if not isinstance(args, list) or len(args) < 2 or not all(isinstance(a, str) for a in args): continue
            destination = _join(current["wd"], args[-1])
            for source in args[:-1]:
                if re.search(r"[*?\[\]]", source) or PurePosixPath(source).is_absolute(): continue
                local = _join(context, source); disk = link.disk(local)
                if disk is None: continue
                isdir = disk.is_dir(); dest = destination
                if not isdir and (args[-1].endswith("/") or len(args) > 2): dest = _join(dest, PurePosixPath(source).name)
                current["copies"].append((dest, local, isdir))
    selected = stages.get(target) if target else current
    return (selected["wd"], selected["copies"]) if selected else None


def _compose(link: _Links) -> None:
    for compose in sorted(p for p in link.files if re.fullmatch(r"(?:docker-)?compose(?:\.[A-Za-z0-9_-]+)*\.ya?ml", PurePosixPath(p).name)):
        text = link.read(compose)
        if text is None: continue
        try: root = _yaml_fields(_yaml_lines(text))
        except ValueError:
            link.warnings["unsupported Compose YAML"] += 1; continue
        service_field = root.get("services")
        if not service_field: continue
        services = _yaml_fields(service_field[2]); base = str(PurePosixPath(compose).parent)
        images = {}; builds = {}
        for name, item in services.items():
            fields = _yaml_fields(item[2])
            if "extends" in fields or any(re.search(r"(?:^|\s)(?:<<:|[&*][A-Za-z])", c) for _, c, _ in item[2]): continue
            build = fields.get("build"); value = _scalar(build[0]) if build else None
            if build and not build[0]: value = {k: _scalar(v[0]) for k, v in _yaml_fields(build[2]).items()}
            if isinstance(value, str): value = {"context": value}
            if isinstance(value, dict) and isinstance(value.get("context"), str):
                dockerfile = value.get("dockerfile", "Dockerfile")
                target = value.get("target")
                if not isinstance(dockerfile, str) or ("target" in value and not isinstance(target, str)):
                    continue
                context = _join(base, value["context"]); docker = _join(context, dockerfile)
                if link.disk(context) is not None and link.disk(docker) is not None:
                    builds[name] = (context, docker, value.get("target"))
                    image = _field_values(fields.get("image"))
                    if isinstance(image, str): images.setdefault(image, []).append(builds[name])
        for name, item in services.items():
            fields = _yaml_fields(item[2])
            if "extends" in fields or any(re.search(r"(?:^|\s)(?:<<:|[&*][A-Za-z])", c) for _, c, _ in item[2]): continue
            command = _field_values(fields.get("command")); script = _script_arg(command)
            if not script: continue
            image = _field_values(fields.get("image")); build = builds.get(name)
            if build is None and isinstance(image, str) and len(images.get(image, [])) == 1: build = images[image][0]
            mapped = _docker_mapping(link, *build) if build else None
            wd, copies = mapped if mapped else (None, [])
            working = _field_values(fields.get("working_dir"))
            if isinstance(working, str) and working.startswith("/"): wd = working
            volumes = _field_values(fields.get("volumes"))
            if isinstance(volumes, list):
                for volume in volumes:
                    if isinstance(volume, str) and volume.count(":") in {1, 2}:
                        host, container = volume.split(":")[:2]
                        if host.startswith(".") and container.startswith("/"):
                            local = _join(base, host); disk = link.disk(local)
                            if disk is not None: copies.append((container, local, disk.is_dir()))
            if not script.startswith("/") and wd is None: continue
            wire = posixpath.normpath(script if script.startswith("/") else _join(wd, script)); matches = set()
            for container, local, isdir in copies:
                if wire == container: candidate = local
                elif isdir and wire.startswith(container.rstrip("/") + "/"): candidate = _join(local, wire[len(container.rstrip("/"))+1:])
                else: continue
                if candidate in link.files and link.disk(candidate) is not None: matches.add(candidate)
            if len(matches) != 1:
                if len(matches) > 1: link.warnings["ambiguous Compose command mappings"] += 1
                continue
            source = f"service:{compose}:{name}"
            if source not in link.nodes or link.nodes[source].get("filePath") != compose: source = link.files.get(compose)
            link.add(source, link.files[next(iter(matches))], "deploys", "compose", "explicit command resolves through declared COPY/bind mapping", compose, "command:")


def link_framework_relationships(graph: dict[str, Any], project_root: Path | str) -> tuple[int, list[str]]:
    """Add conservative SvelteKit/Xcode/Compose links using existing node IDs.

    The graph's nodes and project files are never changed. Missing, unsafe and
    unsupported sources are skipped. Re-running on the same graph adds no edges.
    """
    link = _Links(graph, Path(project_root))
    for linker in (_sveltekit, _xcode, _compose):
        linker(link)
    count = sum(link.added.values())
    report = [f"  Added {count} framework relationships (" + ", ".join(f"{k}: {v}" for k, v in sorted(link.added.items())) + ")."]
    report.extend(f"  Framework linker skipped {value} {key}." for key, value in sorted(link.warnings.items()) if value)
    return count, report
