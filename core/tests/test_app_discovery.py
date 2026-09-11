# File: core/tests/test_app_discovery.py

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from core.actions.models import ActionRequest, ActionResult, ApplicationConfig
from core.assistant.action_commands import ActionCommandHandler
from core.assistant.prompting import PROMPT_CANCEL_TOKEN, PromptRequest, PromptType
from core.system import applications, places
from core.system.applications import ApplicationCatalog, InstalledApplication
from core.system.places import KnownFolder, find_folders, root_subfolders, vscode_folders


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.menu = root / "menu"
        (self.menu / "Microsoft Visual Studio 2022").mkdir(parents=True)
        self.exes = {}
        for stem, exe in (("Visual Studio 2022", "devenv.exe"), ("Visual Studio Code", "Code.exe"), ("Notepad++", "notepad++.exe"), ("Uninstall Thing", "uninst.exe")):
            path = root / "bin" / exe
            path.parent.mkdir(exist_ok=True)
            path.write_text("", encoding="utf-8")
            self.exes[stem] = str(path)
            (self.menu / f"{stem}.lnk").write_text("", encoding="utf-8")
        (self.menu / "Broken.lnk").write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _catalog(self) -> ApplicationCatalog:
        def resolve(link: Path) -> str | None:
            return self.exes.get(link.stem)

        catalog = ApplicationCatalog(resolve, include_registry=False)
        return catalog

    def test_scan_resolves_shortcuts_and_skips_uninstallers(self) -> None:
        with patch.object(applications, "_start_menu_roots", return_value=[self.menu]):
            catalog = self._catalog()
            names = [item.name for item in catalog.applications()]
        self.assertEqual(names, ["Notepad++", "Visual Studio 2022", "Visual Studio Code"])
        self.assertTrue(all(item.source == "start-menu" for item in catalog.applications()))

    def test_find_ranks_exact_then_prefix_then_fuzzy(self) -> None:
        with patch.object(applications, "_start_menu_roots", return_value=[self.menu]):
            catalog = self._catalog()
            self.assertEqual([item.name for item in catalog.find("visual studio")], ["Visual Studio 2022", "Visual Studio Code"])
            self.assertEqual(catalog.find("vs code")[0].name, "Visual Studio Code")
            self.assertEqual(catalog.find("notepad plus")[0].name, "Notepad++")
            self.assertEqual(catalog.find("devenv")[0].name, "Visual Studio 2022")
            self.assertEqual(catalog.find("photoshop"), [])
            self.assertEqual(catalog.find(""), [])

    def test_scan_is_cached(self) -> None:
        calls = []

        def resolve(link: Path) -> str | None:
            calls.append(link)
            return self.exes.get(link.stem)

        with patch.object(applications, "_start_menu_roots", return_value=[self.menu]):
            catalog = ApplicationCatalog(resolve, include_registry=False)
            catalog.applications()
            catalog.applications()
            self.assertEqual(len(calls), 4, "uninstallers are skipped before resolving")
            catalog.applications(refresh=True)
            self.assertEqual(len(calls), 8)


class PlacesTests(unittest.TestCase):
    def test_vscode_folders_come_from_workspace_storage_and_associations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user = root / "User"
            projects = root / "VS" / "Elephas" / "Mammoth" / "Mammoth Projects"
            rentals = root / "VS" / "Elephas" / "Mammoth" / "Mammoth Rentals"
            gone = root / "VS" / "Gone"
            for folder in (projects, rentals):
                folder.mkdir(parents=True)
            for index, folder in enumerate((projects, rentals, gone)):
                meta = user / "workspaceStorage" / f"h{index}"
                meta.mkdir(parents=True)
                uri = "file:///" + str(folder).replace("\\", "/").replace(":", "%3A").replace(" ", "%20")
                meta.joinpath("workspace.json").write_text(json.dumps({"folder": uri}), encoding="utf-8")
            (user / "globalStorage").mkdir(parents=True)
            (user / "globalStorage" / "storage.json").write_text(json.dumps({"profileAssociations": {"workspaces": {"file:///" + str(rentals).replace("\\", "/").replace(":", "%3A").replace(" ", "%20"): "x"}}}), encoding="utf-8")
            found = vscode_folders(user)
            paths = {item.path for item in found}
            self.assertIn(str(projects), paths)
            self.assertIn(str(rentals), paths)
            self.assertNotIn(str(gone), paths, "folders that no longer exist are dropped")
            self.assertTrue(all(item.source == "vscode" for item in found))

    def test_find_folders_matches_by_name(self) -> None:
        candidates = [
            KnownFolder("E:\\VS\\Elephas\\Mammoth\\Mammoth Projects", "vscode"),
            KnownFolder("E:\\VS\\Elephas\\Mammoth\\Mammoth Rentals", "vscode"),
            KnownFolder("D:\\Docs\\Taxes 2025", "documents"),
        ]
        self.assertEqual(find_folders("mammoth projects", candidates)[0].name, "Mammoth Projects")
        self.assertEqual([item.name for item in find_folders("mammoth", candidates)], ["Mammoth Projects", "Mammoth Rentals"])
        self.assertEqual(find_folders("taxes", candidates)[0].name, "Taxes 2025")
        self.assertEqual(find_folders("nothing like it", candidates), [])

    def test_root_subfolders_walks_two_levels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a" / "b" / "c").mkdir(parents=True)
            (root / ".git").mkdir()
            names = sorted(item.name for item in root_subfolders([root]))
            self.assertEqual(names, ["a", "b"])


class _Executor:
    def __init__(self, applications: dict[str, ApplicationConfig]) -> None:
        self.context = type("Ctx", (), {"applications": applications, "web_shortcuts": {}, "app_alias_map": {}, "system": type("Sys", (), {"open_folder": staticmethod(lambda path: opened.append(path))})()})()
        self.requests: list[ActionRequest] = []
        self.known = {app.executable for app in applications.values()}

    def execute(self, request: ActionRequest) -> ActionResult:
        self.requests.append(request)
        if request.action == "launch_application":
            app = request.arguments.get("app_name", "")
            if app in self.context.applications or any(app in cfg.aliases for cfg in self.context.applications.values()):
                target = request.arguments.get("target")
                return ActionResult(status="success", message=f"Opened {target} in Visual Studio Code" if target else "Launched", action=request.action)
            return ActionResult(status="failed", message="Unknown application", action=request.action, error="Unknown application")
        if request.action == "update_config":
            self.context.applications[request.arguments["app_id"]] = ApplicationConfig(id=request.arguments["app_id"], display_name=request.arguments["display_name"], executable=request.arguments["executable"], aliases=list(request.arguments["aliases"]))
            return ActionResult(status="success", message="Added/updated application", action=request.action)
        return ActionResult(status="failed", message="unknown", action=request.action, error="unknown_action")

    def has_pending_confirmation(self) -> bool:
        return False

    def consume_expired_confirmation_notice(self) -> bool:
        return False


opened: list[str] = []


class _FakeCatalog:
    def __init__(self, items: list[InstalledApplication]) -> None:
        self.items = items

    def find(self, query: str, limit: int = 5) -> list[InstalledApplication]:
        return [item for item in self.items if query.split()[0] in item.name.lower()][:limit]


class LaunchFlowTests(unittest.TestCase):
    def _handler(self, answers: list[str], catalog: Any, applications: dict[str, ApplicationConfig] | None = None, folders: list[KnownFolder] | None = None):
        prompts: list[PromptRequest] = []
        outputs: list[str] = []
        executor = _Executor(applications or {})

        def provider(request: PromptRequest | str) -> str:
            prompts.append(request)
            return answers.pop(0)

        handler = ActionCommandHandler(executor, None, None, output=lambda text, role=None: outputs.append(text), prompt_provider=provider, application_catalog=catalog, folder_finder=(lambda name: folders or []))
        return handler, executor, prompts, outputs

    def test_unknown_app_offers_discovered_programs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "devenv.exe"
            exe.write_text("", encoding="utf-8")
            catalog = _FakeCatalog([InstalledApplication("Visual Studio 2022", str(exe), "start-menu"), InstalledApplication("Visual Studio Code", str(exe), "start-menu")])
            handler, executor, prompts, outputs = self._handler(["yes", f"Visual Studio 2022  ({exe})", "", ""], catalog)
            self.assertTrue(handler.handle("/launch visual studio", {}))
            self.assertEqual([p.prompt_type for p in prompts], [PromptType.YES_NO_CANCEL, PromptType.CHOICE, PromptType.TEXT, PromptType.TEXT])
            self.assertIn("Browse for the program...", prompts[1].choices)
            added = [r for r in executor.requests if r.action == "update_config"][0]
            self.assertEqual(added.arguments["executable"], str(exe))
            self.assertEqual(added.arguments["display_name"], "Visual Studio 2022")
            self.assertEqual(executor.requests[-1].action, "launch_application")

    def test_no_candidates_falls_back_to_a_file_prompt(self) -> None:
        handler, executor, prompts, outputs = self._handler(["yes", "C:\\Tools\\thing.exe", "Thing", "thing"], _FakeCatalog([]))
        self.assertTrue(handler.handle("/launch thing", {}))
        self.assertEqual(prompts[1].prompt_type, PromptType.FILE)
        added = [r for r in executor.requests if r.action == "update_config"][0]
        self.assertEqual(added.arguments["executable"], "C:\\Tools\\thing.exe")

    def test_browse_choice_leads_to_the_file_prompt_and_cancel_stops(self) -> None:
        catalog = _FakeCatalog([InstalledApplication("Visual Studio Code", "C:\\code.exe", "start-menu")])
        handler, executor, prompts, outputs = self._handler(["yes", "Browse for the program...", PROMPT_CANCEL_TOKEN], catalog)
        self.assertTrue(handler.handle("/launch visual studio", {}))
        self.assertEqual([p.prompt_type for p in prompts], [PromptType.YES_NO_CANCEL, PromptType.CHOICE, PromptType.FILE])
        self.assertTrue(any("cancelled" in text.lower() for text in outputs))
        self.assertFalse(any(r.action == "update_config" for r in executor.requests))

    def test_open_a_known_folder_in_vs_code(self) -> None:
        apps = {"vscode": ApplicationConfig(id="vscode", display_name="Visual Studio Code", executable="C:\\code.exe", aliases=["vs code", "code"])}
        folders = [KnownFolder("E:\\VS\\Elephas\\Mammoth\\Mammoth Projects", "vscode"), KnownFolder("E:\\VS\\Elephas\\Mammoth\\Mammoth Rentals", "vscode")]
        handler, executor, prompts, outputs = self._handler([folders[0].label], _FakeCatalog([]), applications=apps, folders=folders)
        self.assertTrue(handler.handle_natural_language("open mammoth projects"))
        self.assertEqual(prompts[-1].prompt_type, PromptType.CHOICE)
        self.assertIn("Visual Studio Code", prompts[-1].text)
        launched = executor.requests[-1]
        self.assertEqual(launched.action, "launch_application")
        self.assertEqual(launched.arguments, {"app_name": "vscode", "target": "E:\\VS\\Elephas\\Mammoth\\Mammoth Projects"})
        self.assertIn("Opened E:\\VS\\Elephas\\Mammoth\\Mammoth Projects in Visual Studio Code", outputs[-1])

    def test_declining_the_folder_falls_back_to_the_app_flow(self) -> None:
        folders = [KnownFolder("E:\\VS\\Thing", "documents")]
        handler, executor, prompts, outputs = self._handler(["None of these", "no"], _FakeCatalog([]), folders=folders)
        self.assertTrue(handler.handle_natural_language("open thing"))
        self.assertEqual([p.prompt_type for p in prompts], [PromptType.CHOICE, PromptType.YES_NO_CANCEL])
        self.assertTrue(any("cancelled" in text.lower() for text in outputs))

    def test_launch_verb_does_not_look_for_folders(self) -> None:
        folders = [KnownFolder("E:\\VS\\Thing", "documents")]
        handler, executor, prompts, outputs = self._handler(["no"], _FakeCatalog([]), folders=folders)
        self.assertTrue(handler.handle_natural_language("launch thing"))
        self.assertEqual([p.prompt_type for p in prompts], [PromptType.YES_NO_CANCEL])


if __name__ == "__main__":
    unittest.main()
