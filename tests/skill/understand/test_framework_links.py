"""Synthetic regression fixtures for inventory-safe framework relationship linking."""
from __future__ import annotations

import copy
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MODULE = Path(os.environ.get("UA_FRAMEWORK_LINKS_MODULE", str(_REPO_ROOT / "understand-anything-plugin/skills/understand/framework_links.py")))
_spec = importlib.util.spec_from_file_location("framework_links", _MODULE)
assert _spec and _spec.loader
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)


def node(path, kind="file"):
    return {"id": f"{kind}:{path}", "type": kind, "filePath": path, "name": Path(path).name}


class Fixtures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.graph = {"nodes": [], "edges": [], "project": {"unchanged": True}}

    def put(self, path, content, kind="file", inventory=True):
        p = self.root / path; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(content)
        if inventory: self.graph["nodes"].append(node(path, kind))
        return f"{kind}:{path}"

    def run_linker(self):
        original = copy.deepcopy(self.graph["nodes"]); before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file() and not p.is_symlink()}
        count, report = module.link_framework_relationships(self.graph, self.root)
        self.assertEqual(original, self.graph["nodes"])
        self.assertEqual(self.graph["project"], {"unchanged": True})
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file() and not p.is_symlink()})
        ids = {n["id"] for n in self.graph["nodes"]}
        for e in self.graph["edges"]:
            self.assertIn(e["source"], ids); self.assertIn(e["target"], ids)
            self.assertNotEqual(e["source"], e["target"])
            self.assertEqual(e["direction"], "forward")
            self.assertIn("Framework (", e["description"])
        return count, report

    def triples(self):
        return {(e["source"], e["target"], e["type"]) for e in self.graph["edges"]}

    def svelte(self, app="ui"):
        self.put(f"{app}/svelte.config.js", "export default { kit: {} };\n")
        self.put(f"{app}/src/routes/+layout.svelte", "<script>let {children}=$props();</script>{@render children()}\n")
        return app

    def test_default_svelte_route_data_hooks_template_and_static(self):
        self.svelte()
        page = self.put("ui/src/routes/jobs/+page.svelte", '<script>let {data,form}=$props();</script><img src="/badge.svg"/><a href="/jobs">Jobs</a>')
        server = self.put("ui/src/routes/jobs/+page.server.ts", 'export const load=({locals})=>({person:locals.person}); export const actions={};')
        hook = self.put("ui/src/hooks.server.ts", 'export const handle=async({event,resolve})=>{event.locals.person="demo";return resolve(event);};')
        template = self.put("ui/src/app.html", '<head>%sveltekit.head%<script src="%sveltekit.assets%/theme.js"></script></head>%sveltekit.body%')
        resource = self.put("ui/static/theme.js", 'document.documentElement.dataset.theme="dark";')
        badge = self.put("ui/static/badge.svg", '<svg/>')
        self.put("ui/static/excluded.png", "pixels", inventory=False)
        self.run_linker(); triples = self.triples()
        self.assertIn((page,server,"depends_on"),triples)
        self.assertIn((server,hook,"depends_on"),triples)
        self.assertIn(("file:ui/src/routes/+layout.svelte",page,"contains"),triples)
        self.assertIn((template,resource,"depends_on"),triples)
        self.assertIn((page,badge,"depends_on"),triples)
        self.assertFalse(any("excluded.png" in str(e) for e in self.graph["edges"]))
        after = copy.deepcopy(self.graph)
        count,_ = module.link_framework_relationships(self.graph,self.root)
        self.assertEqual(count,0); self.assertEqual(after,self.graph)

    def test_nearest_nested_layout_and_parent_composition(self):
        self.svelte("front")
        nested=self.put("front/src/routes/admin/+layout.svelte", "<slot />")
        page=self.put("front/src/routes/admin/a/+page.svelte", "<p>Page</p>")
        self.run_linker(); t=self.triples()
        self.assertIn(("file:front/src/routes/+layout.svelte",nested,"contains"),t)
        self.assertIn((nested,page,"contains"),t)
        self.assertNotIn(("file:front/src/routes/+layout.svelte",page,"contains"),t)

    def test_layout_reset_and_excluded_nearer_layout_do_not_guess(self):
        self.svelte()
        page=self.put("ui/src/routes/escape/a/+page.svelte","<p/>")
        self.put("ui/src/routes/escape/+layout@.svelte","<slot/>")
        excluded=self.put("ui/src/routes/hidden/a/+page.svelte","<p/>")
        self.put("ui/src/routes/hidden/+layout.svelte","<slot/>",inventory=False)
        special=self.put("ui/src/routes/+page@.svelte","<p/>")
        self.run_linker()
        self.assertFalse(any(e["type"]=="contains" and e["target"] in {page,excluded,special} for e in self.graph["edges"]))

    def test_custom_roots_and_ambiguous_configs_skip(self):
        self.svelte()
        self.put("ui/svelte.config.js",'export default {kit:{files:{routes:"other"}}};',inventory=False)
        self.put("ui/src/routes/+page.svelte","<p/>")
        count,report=self.run_linker();self.assertEqual(count,0);self.assertTrue(any("custom/dynamic" in s for s in report))
        self.put("ui/svelte.config.mjs","export default {kit:{}};")
        count,report=self.run_linker();self.assertEqual(count,0);self.assertTrue(any("ambiguous SvelteKit" in s for s in report))

    def test_apps_do_not_cross_scopes_and_dynamic_or_comment_urls_skip(self):
        for app in ("one","two"):
            self.svelte(app)
            self.put(f"{app}/src/routes/item/+page.svelte","<p/>")
        helper=self.put("one/src/navigation.ts", '''// goto('/item')\nconst text="goto('/item')"; const f=()=>goto(`/item/${id}`);''')
        self.put("one/src/routes/+page.svelte",'<script>fetch("/status");goto("/item")</script>')
        endpoint=self.put("one/src/routes/status/+server.ts","export const GET=()=>new Response();")
        self.run_linker()
        self.assertFalse(any(e["source"]==helper for e in self.graph["edges"]))
        self.assertIn(("file:one/src/routes/+page.svelte",endpoint,"depends_on"),self.triples())
        self.assertFalse(any(e["source"].startswith("file:one/") and e["target"].startswith("file:two/") for e in self.graph["edges"]))

    def test_ambiguous_server_pairs_and_svelte_base_paths_skip_urls(self):
        self.svelte()
        self.put("ui/svelte.config.js",'export default {kit:{paths:{base:"/prefix"}}};',inventory=False)
        page=self.put("ui/src/routes/+page.svelte",'<script>goto("/item");</script>')
        self.put("ui/src/routes/+page.server.ts","export const load=()=>({});")
        self.put("ui/src/routes/+page.server.js","export const load=()=>({});")
        self.put("ui/src/routes/item/+page.svelte","<p/>")
        self.run_linker()
        self.assertFalse(any(e["source"]==page and e["type"] in {"routes","depends_on"} for e in self.graph["edges"]))

    def test_unsafe_missing_symlink_and_child_ids_never_become_file_owners(self):
        self.svelte()
        self.graph["nodes"].append(node("../../outside.ts"))
        self.graph["nodes"].append(node("ui/src/routes/missing/+page.svelte"))
        p="ui/src/routes/excluded/+page.svelte";self.put(p,"<p/>",inventory=False)
        self.graph["nodes"].append({"id":f"class:{p}:Widget","type":"class","filePath":p})
        with tempfile.TemporaryDirectory() as outside:
            foreign=Path(outside)/"foreign.svelte";foreign.write_text("<p>external</p>")
            local=self.root/"ui/src/routes/escape/+page.svelte";local.parent.mkdir(parents=True);local.symlink_to(foreign)
            self.graph["nodes"].append(node("ui/src/routes/escape/+page.svelte"))
            self.run_linker()
        self.assertFalse(any("outside" in str(e) or "missing" in str(e) or "excluded" in str(e) or "escape" in str(e) for e in self.graph["edges"]))

    def xcode(self, plist="App/Info.plist"):
        project="native/Demo.xcodeproj/project.pbxproj"
        source=self.put("native/App/Entry.swift","@main struct Entry: App { var body: some Scene { WindowGroup {} } }")
        info=self.put("native/App/Info.plist",'<?xml version="1.0"?><plist><dict/></plist>',"config")
        rootasset=self.put("native/App/Images.xcassets/Contents.json",'{"info":{"version":1}}',"config")
        childasset=self.put("native/App/Images.xcassets/Brand.appiconset/Contents.json",'{"images":[{"filename":"pixels.png"}],"info":{"version":1}}',"config")
        self.put("native/App/Images.xcassets/Brand.appiconset/pixels.png","pixels",inventory=False)
        pbx='''// !$*UTF8*$!\n{objects={
 PR={isa=PBXProject;mainGroup=ROOT;};
 ROOT={isa=PBXGroup;children=(GROUP,);sourceTree="<group>";};
 GROUP={isa=PBXGroup;path=App;children=(SRC,RES,);sourceTree="<group>";};
 SRC={isa=PBXFileReference;path=Entry.swift;sourceTree="<group>";};
 RES={isa=PBXFileReference;path=Images.xcassets;sourceTree="<group>";};
 BS={isa=PBXBuildFile;fileRef=SRC;}; BR={isa=PBXBuildFile;fileRef=RES;};
 SP={isa=PBXSourcesBuildPhase;files=(BS,);}; RP={isa=PBXResourcesBuildPhase;files=(BR,);};
 T={isa=PBXNativeTarget;name=DemoApp;buildPhases=(SP,RP,);buildConfigurationList=CL;};
 CL={isa=XCConfigurationList;buildConfigurations=(CFG,);};
 CFG={isa=XCBuildConfiguration;buildSettings={INFOPLIST_FILE="PLIST";ASSETCATALOG_COMPILER_APPICON_NAME=Brand;};};
 };rootObject=PR;}'''.replace('"PLIST"', '"' + plist + '"')
        pid=self.put(project,pbx)
        workspace=self.put("native/Demo.xcodeproj/project.xcworkspace/contents.xcworkspacedata",'<Workspace><FileRef location="self:"/></Workspace>')
        spec=self.put("native/project.yml","targets:\n  DemoApp:\n    info:\n      path: App/Info.plist\n","config")
        return pid,source,info,rootasset,childasset,workspace,spec

    def test_xcode_explicit_membership_catalogue_plist_workspace_and_spec(self):
        project,source,info,rootasset,childasset,workspace,spec=self.xcode()
        self.run_linker();t=self.triples()
        for edge in [(project,source,"configures"),(project,info,"depends_on"),(project,rootasset,"depends_on"),(rootasset,childasset,"contains"),(workspace,project,"depends_on"),(spec,project,"configures"),(spec,info,"configures"),(info,source,"configures")]:self.assertIn(edge,t)
        self.assertFalse(any("pixels.png" in str(e) for e in self.graph["edges"]))

    def test_xcode_source_root_variable_and_invalid_project_are_safe(self):
        project,_,info,*_=self.xcode("$(SRCROOT)/App/Info.plist")
        self.put("broken/Foo.xcodeproj/project.pbxproj","{ not a plist }")
        self.run_linker();self.assertIn((project,info,"depends_on"),self.triples())

    def test_compose_command_uses_actual_copy_mapping_and_service_node(self):
        compose=self.put("compose.yaml",'services:\n  worker:\n    build: .\n    command: ["node", "--experimental-strip-types", "src/worker.mjs"]\n',"service")
        script=self.put("src/worker.mjs","export const work=()=>0;")
        self.put("Dockerfile","FROM node:22\nWORKDIR /app\nCOPY src /app/src\n")
        sid="service:compose.yaml:worker";self.graph["nodes"].append({"id":sid,"type":"service","filePath":"compose.yaml","name":"worker"})
        self.run_linker();self.assertIn((sid,script,"deploys"),self.triples())
        self.assertNotIn((compose,script,"deploys"),self.triples())

    def test_compose_image_reuse_nested_context_and_array_sequence(self):
        self.put("stack/compose.prod.yml",'''services:
  image:
    image: local-worker
    build: {context: ../worker, dockerfile: Containerfile}
  run:
    image: local-worker
    command:
      - python3
      - -u
      - app.py
''',"service")
        script=self.put("worker/app.py","print('fixture')")
        self.put("worker/Containerfile","FROM python:3\nWORKDIR /service\nCOPY app.py /service/app.py\n")
        self.run_linker();self.assertIn(("service:stack/compose.prod.yml",script,"deploys"),self.triples())

    def test_compose_bind_mount_with_explicit_working_directory(self):
        compose=self.put("compose.yml",'''services:
  runner:
    image: public/python
    working_dir: /work
    volumes: ["./source:/work:ro"]
    command: "python3 job.py"
''',"service")
        script=self.put("source/job.py","pass")
        self.run_linker();self.assertIn((compose,script,"deploys"),self.triples())

    def test_compose_dynamic_shell_alias_and_uncopied_paths_skip(self):
        self.put("compose.yml",'''services:
  shell:
    build: .
    command: ["sh","-c","node src/work.js; echo done"]
  dynamic:
    build: .
    command: ["node","${ENTRY}"]
  missing_copy:
    build: .
    command: ["node","src/work.js"]
  alias:
    <<: *defaults
    command: ["node","src/work.js"]
''',"service")
        self.put("src/work.js","export default 1;")
        self.put("Dockerfile","FROM node:22\nWORKDIR /app\n")
        self.run_linker();self.assertFalse(any(e["type"]=="deploys" for e in self.graph["edges"]))

    def test_compose_build_target_does_not_borrow_other_stage_copies(self):
        self.put("compose.yml",'services:\n  tools:\n    build: {context: ., target: tools}\n    command: ["node","src/worker.js"]\n',"service")
        self.put("Dockerfile","FROM node:22 AS runtime\nWORKDIR /app\nCOPY src /app/src\nFROM node:22 AS tools\nWORKDIR /app\n")
        self.put("src/worker.js","export default 1")
        self.run_linker();self.assertFalse(any(e["type"]=="deploys" for e in self.graph["edges"]))

    def test_invalid_literal_config_shapes_do_not_abort_other_links(self):
        self.svelte()
        page = self.put("ui/src/routes/+page.svelte", "<p/>")
        self.put("ui/static/manifest.webmanifest", '{"start_url": ["/"]}')
        self.put("compose.yml", "services:\n  bad:\n    build: {context: ., target: [runtime]}\n    command: ['node', 'entry.js']\n", "service")
        self.put("Dockerfile", "FROM node:22\nWORKDIR /app\nCOPY entry.js /app/entry.js\n")
        self.put("entry.js", "export default 1")
        self.run_linker()
        self.assertIn(("file:ui/src/routes/+layout.svelte", page, "contains"), self.triples())
        self.assertFalse(any(e["type"] == "deploys" for e in self.graph["edges"]))

    def test_order_determinism_and_non_forward_existing_edges(self):
        self.svelte();page=self.put("ui/src/routes/+page.svelte","<p/>")
        before=copy.deepcopy(self.graph);self.graph["nodes"].reverse();self.run_linker();a=copy.deepcopy(self.graph["edges"])
        module.link_framework_relationships(before,self.root);self.assertEqual(a,before["edges"])
        self.assertIn(("file:ui/src/routes/+layout.svelte",page,"contains"),self.triples())


if __name__ == "__main__":
    unittest.main()
