import pytest


def test_long_text_preview_has_exact_read_continuation():
    from runtime.agent_contracts import compact_evidence
    value = {'content': '正文甲乙' * 1000, 'evidence_assets': [{'kind':'content','version_id':'a'*64}]}
    result = compact_evidence(value)
    item = result['disclosure']['parts'][0]
    assert item['total_chars'] == 4000 and item['displayed_chars'] == 800
    assert item['next_start'] == 800
    assert item['request']['start'] == 800
    assert value['content'] == '正文甲乙' * 1000


def test_explicit_reuse_attaches_fixed_version_without_reextract(tmp_path, monkeypatch):
    from kr_core.evidence_assets import EvidenceAssets
    from runtime import knowledge_delivery, research_workspace
    assets = EvidenceAssets(tmp_path / 'assets')
    ref = assets.put('https://example.org/article', 'original evidence', kind='content', title='Original')
    monkeypatch.setattr(research_workspace.ResearchRecords, 'snapshot', lambda self, task: {'project_id':'demo'})
    store = knowledge_delivery.DeliveryStore(tmp_path / 'state', assets=assets)
    request = knowledge_delivery.AttachEvidence(action='attach_evidence', project_id='demo', task_id='research-new', version_ids=[ref['version_id']])
    result = knowledge_delivery.execute(request, store)
    assert result['status'] == 'attached'
    assert result['content_reprocessed'] is False
    with assets.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM assets').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM asset_scopes WHERE task_id=?', ('research-new',)).fetchone()[0] == 1
    again = knowledge_delivery.execute(request, store)
    assert again['status'] == 'attached'
    with pytest.raises(ValueError, match='project_mismatch'):
        knowledge_delivery.execute(request.model_copy(update={'project_id':'other'}), store)
