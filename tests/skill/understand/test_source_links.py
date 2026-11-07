"""Synthetic coverage for inventory-bounded source reference linking."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

_MODULE = (Path(__file__).resolve().parents[3] / "understand-anything-plugin" /
           "skills" / "understand" / "source_links.py")
_SPEC = importlib.util.spec_from_file_location("source_links", _MODULE)
assert _SPEC and _SPEC.loader
source_links = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(source_links)


class SourceReferenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "project"
        self.root.mkdir()
        self.graph = {"nodes": [], "edges": []}

    def add(self, rel, content="", kind="file", tags=None):
        file = self.root / rel
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding="utf-8")
        return self.node(rel, kind, tags)

    def node(self, rel, kind="file", tags=None):
        node_id = f"{kind}:{rel}"
        self.graph["nodes"].append({"id": node_id, "type": kind, "filePath": rel, "tags": tags or []})
        return node_id

    def link(self):
        return source_links.link_source_references(self.graph, self.root)[0]

    def pairs(self):
        return {(e["source"], e["target"], e["type"]) for e in self.graph["edges"]}

    def test_markdown_url_paths_and_backticks(self):
        source = self.add("docs/guide.md", "[local](../src/main.py#init)\n[space](../assets/hero%20image.png)\nRead `src/lib.py:10`.\n[external](https://example.test/src/lib.py)\n", "document")
        targets = [self.add(p) for p in ("src/main.py", "assets/hero image.png", "src/lib.py")]
        self.assertEqual(self.link(), 3)
        self.assertEqual(self.pairs(), {(source, target, "documents") for target in targets})
        self.assertTrue(all("Source docs/guide.md, line " in e["description"] for e in self.graph["edges"]))
        self.assertEqual(self.link(), 0)

    def test_ignores_fenced_examples_and_ambiguous_backticks(self):
        self.add("docs/guide.md", "`src/shared.py`\n```markdown\n[example](../src/fenced.py)\n```\n`README.md`", "document")
        for rel in ("src/shared.py", "docs/src/shared.py", "src/fenced.py", "README.md"):
            self.add(rel)
        self.assertEqual(self.link(), 0)

    def test_declared_json_fields_and_conditional_export_paths(self):
        source = self.add("pkg/package.json", json.dumps({
            "exports": {".": {"import": "./src/index.ts"}},
            "artifacts": [{"path": "./guide.md"}],
            "description": "./ignored.ts",
            "files": ["./data.json", "./untracked.json", "/outside.json"],
        }), "config")
        target = self.add("pkg/src/index.ts")
        guide = self.add("pkg/guide.md", kind="document")
        data = self.add("pkg/data.json", "{}", "config")
        self.add("pkg/ignored.ts")
        self.add("outside.json", "{}", "config")
        (self.root / "pkg/untracked.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.link(), 3)
        self.assertEqual(self.pairs(), {(source, t, "depends_on") for t in (target, guide, data)})
        self.assertTrue(any("JSON field exports...import" in e["description"] for e in self.graph["edges"]))

    def test_rejects_escaped_and_symlinked_sources_and_targets(self):
        outside = self.root.parent / "outside.md"
        outside.write_text("[inside](src/main.ts)", encoding="utf-8")
        (self.root / "alias.md").symlink_to(outside)
        self.node("alias.md", "document")
        self.add("src/main.ts")
        self.add("guide.md", "[escape](../outside.md)\n[alias](alias.md)\n`../outside.md`", "document")
        self.node("../outside.md", "document")
        self.assertEqual(self.link(), 0)

    def test_imported_instruction_body_and_code_string_are_not_project_references(self):
        self.add("foreign.md", "[src](src/main.ts)", "document", ["imported-skill"])
        self.add("code.ts", 'const sample = "src/main.ts";')
        self.add("src/main.ts")
        self.assertEqual(self.link(), 0)

    def test_catalogue_paths_point_to_existing_documents(self):
        source = self.add("catalog/catalogue.json", '{"entries":[{"path":"defs/skill.md"}]}', "config")
        target = self.add("catalog/defs/skill.md", "[outside](../package.json)", "document", ["imported-skill"])
        self.assertEqual(self.link(), 1)
        self.assertEqual(self.pairs(), {(source, target, "depends_on")})


if __name__ == "__main__":
    unittest.main()
