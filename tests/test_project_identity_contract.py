import json
import pytest
from pathlib import Path

from kr_core.evidence_assets import EvidenceAssets
from runtime.knowledge import Search
from runtime.knowledge_delivery import Authorize, DeliveryStore
from runtime.research_archive import BindContext
from runtime import research_ledger as ledger


@pytest.mark.parametrize('value', ['   ', 123, None])
def test_required_project_identity_rejects_empty_or_nontext(value):
    with pytest.raises(ValueError):
        BindContext(action='bind_context', task_id='task', project_id=value, project_name='Project',
                    task_title='Task', local_grant_id='grant', local_report_path='report.md')


def test_project_ids_are_canonicalized_at_contract_boundary():
    assert Search(action="search", project_id="  KnowledgeRadar ").project_id == "knowledgeradar"
    assert BindContext(
        action="bind_context",
        task_id="task",
        project_id="KnowledgeRadar",
        project_name="KnowledgeRadar",
        task_title="Task",
        local_grant_id="a" * 32,
        local_report_path="C:/reports/report.md",
    ).project_id == "knowledgeradar"


def test_ledger_persists_canonical_project_id(monkeypatch, tmp_path):
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    task = ledger.open_task(
        objective="identity",
        budget="fast",
        considered=[],
        task_id="identity-task",
        project_id="KnowledgeRadar",
        project_name="KnowledgeRadar",
    )
    assert task["project_id"] == "knowledgeradar"
    stored = json.loads((tmp_path / "research_tasks" / "identity-task.json").read_text(encoding="utf-8"))
    assert stored["project_id"] == "knowledgeradar"
    assert ledger.project_context(stored)["project_id"] == "knowledgeradar"


def test_legacy_grant_matches_canonical_project_id(monkeypatch, tmp_path):
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    assets = EvidenceAssets(tmp_path / "evidence")
    store = DeliveryStore(tmp_path / "delivery", assets)
    root = tmp_path / "vault"
    root.mkdir()
    grant = store.authorize(Authorize(action="authorize_destination", approved_root=str(root), project_id="KnowledgeRadar"))
    assert grant["project_id"] == "knowledgeradar"
    assert store._grant(type("Request", (), {"grant_id": grant["grant_id"], "project_id": "KNOWLEDGERADAR"})()) == root


def test_legacy_scope_is_searchable_through_canonical_identity(tmp_path):
    from runtime.knowledge import search
    assets=EvidenceAssets(tmp_path/'evidence')
    ref=assets.put('https://example.org/source','完整正文',title='旧资料')
    assets.reference_many([ref['version_id']],'task',project_id='KnowledgeRadar',task_id='task')
    found=search(assets,Search(action='search',project_id='knowledgeradar'))
    assert [r['asset']['version_id'] for r in found['items']]==[ref['version_id']]


def test_archive_bind_and_ledger_share_canonical_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KR_EVIDENCE_ROOT", str(tmp_path / "state" / "evidence"))
    assets = EvidenceAssets()
    store = DeliveryStore(tmp_path / "state", assets)
    local = tmp_path / "local"
    local.mkdir()
    grant = store.authorize(Authorize(action="authorize_destination", approved_root=str(local), project_id="KnowledgeRadar"))
    ledger.open_task(objective="archive", budget="fast", considered=[], task_id="archive-task", project_id="KnowledgeRadar", project_name="KnowledgeRadar")
    from runtime.research_archive import ResearchArchive

    engine = ResearchArchive(store)
    result = engine.bind(BindContext(
        action="bind_context",
        task_id="archive-task",
        project_id="KNOWLEDGERADAR",
        project_name="KnowledgeRadar",
        task_title="Archive",
        local_grant_id=grant["grant_id"],
        local_report_path=str(local / "report.md"),
    ))
    assert result["project_id"] == "knowledgeradar"
    assert ledger.read_task(task_id="archive-task")["project_id"] == "knowledgeradar"
