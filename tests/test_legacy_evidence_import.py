"""导入历史固定版本不重算ID、不带入旧任务权限或复活撤回来源。"""
import pytest
from kr_core.evidence_assets import EvidenceAssets


def test_import_preserves_original_identity_bytes_and_time(tmp_path):
    old=EvidenceAssets(tmp_path/'old');new=EvidenceAssets(tmp_path/'current')
    ref=old.put('https://example.org/a','原文\r\n下一行',metadata={'extractor':'old/v1'})
    old.reference_many([ref['version_id']],'old-task',project_id='other',task_id='old-task')
    plan=new.plan_import(old.root,[ref['version_id']])
    assert not new.root.exists()
    result=new.apply_import(old.root,[ref['version_id']],plan['plan_id'])
    assert result['version_ids']==[ref['version_id']]
    assert new.fetch(ref['version_id'])['asset']['captured_at']==ref['captured_at']
    assert new.fetch(ref['version_id'])['text']=='原文\r\n下一行'
    with new.connection() as c:
        assert c.execute('SELECT COUNT(*) FROM asset_scopes').fetchone()[0]==0
    assert new.apply_import(old.root,[ref['version_id']],plan['plan_id'])==result


def test_import_rejects_changed_source_and_preserves_withdrawal(tmp_path):
    old=EvidenceAssets(tmp_path/'old');new=EvidenceAssets(tmp_path/'current')
    ref=old.put('https://example.org/a','原文')
    plan=new.plan_import(old.root,[ref['version_id']])
    old.withdraw(ref['source_id'])
    with pytest.raises(ValueError,match='plan_stale'):
        new.apply_import(old.root,[ref['version_id']],plan['plan_id'])
    plan=new.plan_import(old.root,[ref['version_id']])
    new.apply_import(old.root,[ref['version_id']],plan['plan_id'])
    with pytest.raises(ValueError,match='withdrawn'):new.fetch(ref['version_id'])


def test_import_requires_complete_provenance_closure_and_verified_bytes(tmp_path):
    old=EvidenceAssets(tmp_path/'old');new=EvidenceAssets(tmp_path/'current')
    parent=old.put('https://example.org/a','原件')
    child=old.put('https://example.org/a','派生件',metadata={'derived_from':[parent['version_id']]})
    with pytest.raises(ValueError,match='provenance'):
        new.plan_import(old.root,[child['version_id']])
    plan=new.plan_import(old.root,[parent['version_id'],child['version_id']])
    old._file(child['raw_sha256']).write_text('损坏',encoding='utf-8')
    with pytest.raises(ValueError,match='integrity'):
        new.apply_import(old.root,[parent['version_id'],child['version_id']],plan['plan_id'])
    assert not (new.root/'index.sqlite3').exists()


def test_import_respects_physical_capacity_without_publishing_raw_files(tmp_path):
    old=EvidenceAssets(tmp_path/'old');new=EvidenceAssets(tmp_path/'current',max_physical_bytes=1)
    ref=old.put('https://example.org/a','原文')
    plan=new.plan_import(old.root,[ref['version_id']])
    with pytest.raises(ValueError,match='physical_capacity'):
        new.apply_import(old.root,[ref['version_id']],plan['plan_id'])
    assert not new._file(ref['raw_sha256']).exists()
    with new.connection() as conn:assert conn.execute('SELECT COUNT(*) FROM assets').fetchone()[0]==0


def test_import_does_not_charge_existing_identical_raw_bytes_twice(tmp_path):
    old=EvidenceAssets(tmp_path/'old');new=EvidenceAssets(tmp_path/'current',max_bytes=6)
    original=old.put('https://example.org/a','原文',metadata={'extractor':'old/v1'})
    new.put('https://example.org/a','原文',metadata={'extractor':'new/v2'})
    plan=new.plan_import(old.root,[original['version_id']])
    assert new.apply_import(old.root,[original['version_id']],plan['plan_id'])['status']=='imported'
