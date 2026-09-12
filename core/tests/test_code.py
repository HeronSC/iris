# File: core/tests/test_code.py

"""Coding assistant (3.2): read-only workspace, symbol and text awareness for Business Central AL."""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from core.actions.implementations.code_tools import ALSymbolAction, ALWorkspaceAction, RepoSearchAction
from core.actions.models import ActionRequest
from core.code import CodeService
from core.code.search import RipgrepSearch, SearchToolMissing
from core.code.symbols import SymbolIndex, package_symbols, parse_source
from core.code.workspace import find_workspace_by_name, find_workspace_root, load_workspace, parse_package_name
from core.results.models import ResultKind

APP_JSON = {
    "id": "8f3c2e91-6d44-4b8f-9a2e-1c7d5b0f4a63",
    "name": "Mammoth Projects",
    "publisher": "Elephas Corporation",
    "version": "28.4.0.17",
    "platform": "28.0.0.0",
    "application": "28.0.0.0",
    "runtime": "16.0",
    "dependencies": [{"id": "7d9e1d27", "name": "Mammoth Common", "publisher": "Elephas Corporation", "version": "28.4.0.1"}],
    "idRanges": [{"from": 99500, "to": 99699}],
}

LAUNCH_JSON = {
    "configurations": [
        {"name": "BC SaaS Sandbox", "type": "al", "server": "https://businesscentral.dynamics.com", "environmentType": "Sandbox", "environmentName": "Sandbox", "startupObjectType": "Page", "startupObjectId": 89},
        {"name": "Attach", "type": "node", "port": 9229},
    ]
}

CODEUNIT_AL = """codeunit 99514 ELEPCompanyCamSyncProcess
{
    trigger OnRun()
    begin
    end;

    procedure EnqueueUpsertFromJob(Job: Record Job; Force: Boolean)
    begin
    end;

    local procedure ClearSyncEntry(JobNo: Code[20])
    begin
    end;

    [IntegrationEvent(false, false)]
    local procedure OnBeforeEnqueue(var Job: Record Job; var IsHandled: Boolean)
    begin
    end;

    [EventSubscriber(ObjectType::Codeunit, Codeunit::"Sales-Post", 'OnBeforePostLines', '', false, false)]
    local procedure HandleSalesPost(var SalesLine: Record "Sales Line")
    begin
    end;

    [EventSubscriber(ObjectType::Table, Database::Job, 'OnAfterInsertEvent', '', false, false)]
    local procedure HandleJobInsert(var Rec: Record Job)
    begin
    end;
}
"""

TABLEEXT_AL = """tableextension 99501 "ELEP Job Ext" extends Job
{
    fields
    {
        field(99500; "ELEP Photo Provider Project ID"; Text[100]) { }
    }
}
"""

PROVIDER_AL = """codeunit 99511 ELEPCompanyCamProvider implements ELEPProjectPhotoProvider, ELEPProjectPhotoReadProvider
{
    procedure Upload(FileName: Text): Boolean
    begin
    end;
}
"""

SYMBOL_REFERENCE = {
    "AppId": "437dbf0e-84ff-417a-965d-ed2bb9650972",
    "Name": "Base Application",
    "Publisher": "Microsoft",
    "Version": "28.4.53241.53839",
    "Tables": [],
    "Codeunits": [],
    "Namespaces": [
        {
            "Name": "Microsoft",
            "Namespaces": [
                {
                    "Name": "Sales",
                    "Tables": [
                        {
                            "Id": 18,
                            "Name": "Customer",
                            "ReferenceSourceFileName": "Customer.Table.al",
                            "Properties": [{"Name": "Caption", "Value": "Customer"}],
                            "Fields": [
                                {"Id": 1, "Name": "No.", "TypeDefinition": {"Name": "Code[20]"}},
                                {"Id": 2, "Name": "Name", "TypeDefinition": {"Name": "Text[100]"}},
                                {"Id": 5, "Name": "Salesperson Code", "TypeDefinition": {"Name": "Code[20]"}},
                            ],
                            "Methods": [{"Id": 1, "Name": "GetTotalAmountLCY", "Parameters": []}],
                        }
                    ],
                    "Codeunits": [
                        {
                            "Id": 80,
                            "Name": "Sales-Post",
                            "ReferenceSourceFileName": "SalesPost.Codeunit.al",
                            "Methods": [
                                {"Id": 1, "Name": "Run", "Parameters": [{"Name": "SalesHeader", "TypeDefinition": {"Name": "Record", "Subtype": {"Name": "Sales Header"}}}]},
                                {
                                    "Id": 2,
                                    "Name": "OnBeforePostLines",
                                    "Attributes": [{"Name": "IntegrationEvent", "Arguments": [{"Value": "False"}, {"Value": "False"}]}],
                                    "Parameters": [{"Name": "SalesLine", "TypeDefinition": {"Name": "Record", "Subtype": {"Name": "Sales Line"}}}],
                                },
                            ],
                        }
                    ],
                    "TableExtensions": [{"Id": 6040, "Name": "Customer Ext", "TargetObject": "Customer", "Fields": [{"Id": 6040, "Name": "Extra", "TypeDefinition": {"Name": "Text[50]"}}]}],
                    "EnumTypes": [{"Id": 5, "Name": "Sales Document Type", "Values": [{"Name": "Quote"}, {"Ordinal": 1, "Name": "Order"}]}],
                }
            ],
        }
    ],
}


def _package_bytes(payload: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("NavxManifest.xml", "<manifest/>")
        archive.writestr("SymbolReference.json", json.dumps(payload))
    return b"NAVX" + bytes(36) + buffer.getvalue()


def _build_workspace(root: Path) -> Path:
    project = root / "Kloter Farms BC Projects"
    (project / "src" / "Codeunits").mkdir(parents=True)
    (project / "src" / "Tables").mkdir(parents=True)
    (project / ".vscode").mkdir()
    (project / ".alpackages").mkdir()
    (project / ".git").mkdir()
    (project / "app.json").write_text(json.dumps(APP_JSON), encoding="utf-8")
    (project / ".vscode" / "launch.json").write_text(json.dumps(LAUNCH_JSON), encoding="utf-8")
    (project / "src" / "Codeunits" / "ELEPCompanyCamSyncProcess.Codeunit.al").write_text(CODEUNIT_AL, encoding="utf-8")
    (project / "src" / "Codeunits" / "ELEPCompanyCamProvider.Codeunit.al").write_text(PROVIDER_AL, encoding="utf-8")
    (project / "src" / "Tables" / "ELEPJobExt.TableExt.al").write_text(TABLEEXT_AL, encoding="utf-8")
    (project / ".alpackages" / "Microsoft_Base Application_28.4.53241.53839.app").write_bytes(_package_bytes(SYMBOL_REFERENCE))
    older = dict(SYMBOL_REFERENCE, Version="28.4.53241.53451")
    (project / ".alpackages" / "Microsoft_Base Application_28.4.53241.53451.app").write_bytes(_package_bytes(older))
    (project / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    return project


class WorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.project = _build_workspace(self.root)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_the_root_is_found_from_any_file_below_it(self) -> None:
        self.assertEqual(find_workspace_root(self.project / "src" / "Codeunits" / "ELEPCompanyCamProvider.Codeunit.al"), self.project)
        self.assertIsNone(find_workspace_root(self.root))

    def test_app_launch_and_packages_are_read(self) -> None:
        workspace = load_workspace(self.project / "src")
        self.assertEqual(workspace.name, "Mammoth Projects")
        self.assertEqual(workspace.id_ranges, ((99500, 99699),))
        self.assertEqual([item.name for item in workspace.launch], ["BC SaaS Sandbox"])
        self.assertEqual(workspace.launch[0].startup_object, "Page 89")
        self.assertEqual(len(workspace.packages), 2)
        self.assertEqual(workspace.packages[0].publisher, "Microsoft")
        described = workspace.describe()
        self.assertIn("Mammoth Projects 28.4.0.17 by Elephas Corporation", described)
        self.assertIn("Depends on Mammoth Common 28.4.0.1", described)
        self.assertIn("Object ids 99500-99699", described)
        self.assertEqual(len(workspace.source_files()), 3)
        self.assertEqual(workspace.find_source("elepjobext.tableext.al").name, "ELEPJobExt.TableExt.al")

    def test_a_workspace_is_found_by_folder_name_or_app_name(self) -> None:
        self.assertEqual(find_workspace_by_name("kloter farms bc projects", [self.root]), self.project)
        self.assertEqual(find_workspace_by_name("Mammoth Projects", [self.root]), self.project)
        self.assertIsNone(find_workspace_by_name("Nope", [self.root]))

    def test_package_names_parse(self) -> None:
        info = parse_package_name(Path("Elephas Corporation_Mammoth Common_28.4.0.1.app"))
        self.assertEqual((info.publisher, info.name, info.version), ("Elephas Corporation", "Mammoth Common", "28.4.0.1"))


class SymbolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.project = _build_workspace(self.root)
        self.workspace = load_workspace(self.project)
        self.index = SymbolIndex(self.workspace, cache_dir=self.root / "cache")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_package_symbols_walk_namespaces_and_keep_fields_events_and_targets(self) -> None:
        parsed = package_symbols(self.project / ".alpackages" / "Microsoft_Base Application_28.4.53241.53839.app")
        self.assertEqual(parsed.name, "Base Application")
        by_name = {(item.kind, item.name): item for item in parsed.symbols}
        customer = by_name[("table", "Customer")]
        self.assertEqual(customer.id, 18)
        self.assertEqual(customer.namespace, "Microsoft.Sales")
        self.assertEqual([item.name for item in customer.fields], ["No.", "Name", "Salesperson Code"])
        self.assertEqual(customer.fields[0].type, "Code[20]")
        post = by_name[("codeunit", "Sales-Post")]
        self.assertEqual([item.name for item in post.events], ["OnBeforePostLines"])
        self.assertEqual(post.events[0].parameters, ("SalesLine: Record Sales Line",))
        self.assertEqual(by_name[("tableextension", "Customer Ext")].target, "Customer")
        self.assertEqual([item.name for item in by_name[("enum", "Sales Document Type")].fields], ["Quote", "Order"])

    def test_sources_parse_headers_procedures_events_and_subscribers(self) -> None:
        symbols = parse_source(CODEUNIT_AL + TABLEEXT_AL + PROVIDER_AL, file_name="x.al", app_name="Mammoth Projects")
        self.assertEqual([item.label for item in symbols], ["codeunit 99514 ELEPCompanyCamSyncProcess", "tableextension 99501 ELEP Job Ext extends Job", "codeunit 99511 ELEPCompanyCamProvider"])
        sync = symbols[0]
        self.assertEqual([item.name for item in sync.events], ["OnBeforeEnqueue"])
        self.assertEqual([item.name for item in sync.methods if not item.local and not item.event], ["EnqueueUpsertFromJob"])
        self.assertEqual([(item.object_name, item.event, item.procedure) for item in sync.subscriptions], [("Sales-Post", "OnBeforePostLines", "HandleSalesPost"), ("Job", "OnAfterInsertEvent", "HandleJobInsert")])
        self.assertEqual(symbols[2].implements, ("ELEPProjectPhotoProvider", "ELEPProjectPhotoReadProvider"))

    def test_the_index_prefers_workspace_objects_and_finds_by_id_prefix_and_target(self) -> None:
        self.assertEqual(self.index.find("customer")[0].label, "table 18 Customer")
        self.assertEqual(self.index.find("80")[0].name, "Sales-Post")
        self.assertEqual([item.name for item in self.index.find("ELEPCompanyCam")], ["ELEPCompanyCamProvider", "ELEPCompanyCamSyncProcess"])
        self.assertEqual(self.index.find("customer", kind="tableextension")[0].name, "Customer Ext")
        self.assertEqual([item.name for item in self.index.extensions_of("Customer")], ["Customer Ext"])
        subscribers = self.index.subscribers_to("Sales-Post", "OnBeforePostLines")
        self.assertEqual([(owner.name, sub.procedure) for owner, sub in subscribers], [("ELEPCompanyCamSyncProcess", "HandleSalesPost")])
        self.assertEqual(self.index.counts(), {"codeunit": 2, "tableextension": 1})

    def test_only_the_newest_package_version_is_loaded_and_it_is_cached(self) -> None:
        packages = self.index.packages()
        self.assertEqual([item.version for item in packages], ["28.4.53241.53839"])
        cache_files = list((self.root / "cache").glob("*.json"))
        self.assertEqual(len(cache_files), 1)
        fresh = SymbolIndex(self.workspace, cache_dir=self.root / "cache")
        self.assertEqual(fresh.find("Customer")[0].fields[1].name, "Name")


@unittest.skipUnless(shutil.which("rg"), "ripgrep is not installed")
class SearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.project = _build_workspace(self.root)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_matches_come_back_with_file_and_line_and_globs_narrow_them(self) -> None:
        outcome = RipgrepSearch().search("EventSubscriber", self.project)
        self.assertEqual(outcome.files, [str(Path("src") / "Codeunits" / "ELEPCompanyCamSyncProcess.Codeunit.al")])
        self.assertEqual([match.line for match in outcome.matches], [20, 25])
        self.assertEqual(len(RipgrepSearch().search("procedure", self.project, glob="*Provider*").matches), 1)
        self.assertEqual(len(RipgrepSearch().search("procedure", self.project, max_results=2).matches), 2)
        self.assertEqual(RipgrepSearch().search(r"On\w+Event", self.project, regex=True).matches[0].text.strip()[:16], "[EventSubscriber")

    def test_a_missing_rg_is_a_clear_error(self) -> None:
        with self.assertRaises(SearchToolMissing):
            RipgrepSearch(executable="").search("x", self.project)


class ServiceAndToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.project = _build_workspace(self.root)
        self.context = SimpleNamespace(paused=False, current=lambda: SimpleNamespace(target=None, project="Kloter Farms BC Projects", extra={"file": "ELEPJobExt.TableExt.al"}))
        self.service = CodeService([self.root], cache_dir=self.root / "cache", context_service=self.context)
        self.execution = SimpleNamespace(code_service=self.service)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_the_active_workspace_comes_from_the_vs_code_window(self) -> None:
        workspace = self.service.active_workspace()
        self.assertEqual(workspace.root, self.project)
        self.assertEqual(self.service.active_file().name, "ELEPJobExt.TableExt.al")
        line = self.service.prompt_line()
        self.assertIn("AL workspace: Mammoth Projects 28.4.0.17", line)
        self.assertIn("Business Central 28.0.0.0", line)
        self.assertIn("Open AL file: src", line)
        self.assertTrue(self.service.within_roots(self.project / "src"))
        self.assertFalse(self.service.within_roots(Path(self.tempdir.name).parent / "elsewhere"))

    def test_al_workspace_describes_the_project_in_view(self) -> None:
        action = ALWorkspaceAction()
        validation = action.validate(ActionRequest(action="al_workspace", arguments={}), self.execution)
        self.assertTrue(validation.ok)
        result = action.execute(ActionRequest(action="al_workspace", arguments=validation.resolved_arguments), self.execution)
        self.assertEqual(result.status, "success")
        self.assertIn("Defines 2 codeunits, 1 tableextension", result.message)
        self.assertEqual([item.kind for item in result.results], [ResultKind.TEXT, ResultKind.TABLE, ResultKind.TABLE, ResultKind.TABLE])

    def test_al_symbol_answers_from_packages_and_sources(self) -> None:
        action = ALSymbolAction()
        validation = action.validate(ActionRequest(action="al_symbol", arguments={"name": "Sales-Post", "detail": "events"}), self.execution)
        self.assertTrue(validation.ok)
        result = action.execute(ActionRequest(action="al_symbol", arguments=validation.resolved_arguments), self.execution)
        self.assertIn("codeunit 80 Sales-Post in Base Application", result.message)
        self.assertIn("Events: OnBeforePostLines", result.message)
        self.assertIn("ELEPCompanyCamSyncProcess.HandleSalesPost on OnBeforePostLines", result.message)
        titles = [item.title for item in result.results]
        self.assertIn("Events published by Sales-Post", titles)
        self.assertIn("Subscribers to Sales-Post in this workspace", titles)
        fields = action.execute(ActionRequest(action="al_symbol", arguments={"name": "Customer", "kind": "table", "detail": "fields", "path": str(self.project)}), self.execution)
        self.assertIn("Extended by tableextension 6040 Customer Ext", fields.message)
        self.assertIn("Fields: No. (Code[20])", fields.message)
        several = action.execute(ActionRequest(action="al_symbol", arguments={"name": "ELEPCompanyCam", "detail": "summary", "path": str(self.project)}), self.execution)
        self.assertIn("2 objects match", several.message)
        missing = action.execute(ActionRequest(action="al_symbol", arguments={"name": "Nothing", "detail": "summary", "path": str(self.project)}), self.execution)
        self.assertEqual(missing.results[0].kind, ResultKind.STATUS)

    def test_paths_outside_the_roots_are_refused(self) -> None:
        outside = str(Path(self.tempdir.name).parent / "elsewhere")
        validation = ALWorkspaceAction().validate(ActionRequest(action="al_workspace", arguments={"path": outside}), self.execution)
        self.assertFalse(validation.ok)
        self.assertIn("outside the folders Iris may read", validation.error)
        no_service = RepoSearchAction().validate(ActionRequest(action="repo_search", arguments={"pattern": "x"}), SimpleNamespace())
        self.assertFalse(no_service.ok)

    @unittest.skipUnless(shutil.which("rg"), "ripgrep is not installed")
    def test_repo_search_returns_a_table_of_matches(self) -> None:
        action = RepoSearchAction()
        validation = action.validate(ActionRequest(action="repo_search", arguments={"pattern": "OnBeforePostLines"}), self.execution)
        self.assertTrue(validation.ok)
        self.assertEqual(validation.resolved_target, str(self.project))
        result = action.execute(ActionRequest(action="repo_search", arguments=validation.resolved_arguments), self.execution)
        self.assertEqual(result.status, "success")
        self.assertIn("1 match for 'OnBeforePostLines' in 1 file", result.message)
        self.assertEqual(result.results[0].kind, ResultKind.TABLE)
        self.assertEqual(result.results[0].data["rows"][0][1], 20)
        none = action.execute(ActionRequest(action="repo_search", arguments={"pattern": "zzzz", "path": str(self.project)}), self.execution)
        self.assertEqual(none.results[0].kind, ResultKind.STATUS)


if __name__ == "__main__":
    unittest.main()
