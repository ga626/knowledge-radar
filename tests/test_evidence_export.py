"""替代旧ZIP路径：原字节固定交付，不再重写换行或接受任意包路径。"""
import hashlib
import json
import sqlite3
from pathlib import Path
import pytest
from kr_core.evidence_assets import EvidenceAssets
from runtime.knowledge_delivery import Authorize,Deliver,DeliveryStore


def test_delivery_preserves_original_utf8_and_user_notes(tmp_path):
    assets=EvidenceAssets(tmp_path/'assets')
    text='头🙂\r\n中段\r独立\n末尾反例'
    ref=assets.put('https://example.org/a',text,title='证据')
    assets.reference_many([ref['version_id']],'t',project_id='p',task_id='t')
    root=tmp_path/'vault'
    root.mkdir()
    note=root/'人工笔记.md'
    note.write_text('不要覆盖',encoding='utf-8')
    store=DeliveryStore(tmp_path/'state',assets)
    grant=store.authorize(Authorize(action='authorize_destination',approved_root=str(root),project_id='p'))
    req=Deliver(action='deliver',grant_id=grant['grant_id'],project_id='p',task_id='t',version_ids=[ref['version_id']])
    result=store.deliver(req)
    assert result['status']=='local_committed'
    assert result['entries'][0]['file']=='网页 - 证据.md'
    visible=(Path(result['path'])/result['entries'][0]['file']).read_bytes()
    assert visible.startswith('> [!info] 资料摘要'.encode())
    with sqlite3.connect(store.db) as conn:
        parts=[json.loads(row[0]) for row in conn.execute(
            'SELECT payload FROM archive_parts WHERE round_id=? ORDER BY ordinal',(result['delivery_id'],))]
    assert len(parts)==1 and parts[0]['version_id']==ref['version_id']
    part=parts[0]
    output=visible[part['export_start']:part['export_end']]
    assert output==text.encode('utf-8')
    assert hashlib.sha256(output).hexdigest()==ref['raw_sha256']==part['part_sha256']
    assert assets.fetch(ref['version_id'])['text'].encode('utf-8')==output
    assert note.read_text('utf-8')=='不要覆盖'
    assets.withdraw(ref['source_id'])
    with pytest.raises(ValueError,match='withdrawn'):
        store.deliver(req)
