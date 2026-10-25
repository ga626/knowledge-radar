import io
import json
import zipfile
import pytest
from kr_core.evidence_assets import EvidenceAssets
from runtime.evidence_export import build_bundle, materialize_bundle, normalize_with_mapping, split_utf8


def test_lf_mapping_bundle_safe_inbox_and_withdrawal(tmp_path):
    assets = EvidenceAssets(tmp_path / 'assets')
    text = '头🙂\r\n中段\r独立\n末尾反例'
    ref = assets.put('https://example.org/a', text, title='证据')
    raw, manifest = build_bundle(assets, [ref['version_id']], export_acknowledged=True)
    source = manifest['sources'][0]
    assert source['raw_sha256'] == ref['raw_sha256']
    assert source['lf_sha256'] != ref['raw_sha256']
    normalized, mapping = normalize_with_mapping(text)
    assert normalized == '头🙂\n中段\n独立\n末尾反例'
    for pos in range(len(normalized)+1):
        removed = sum(sum(1 for x in range(run['count']) if run['first']+x*run['step']<=pos) for run in mapping['runs'])
        assert text[:pos+removed].replace('\r\n','\n').replace('\r','\n') == normalized[:pos]
    root = tmp_path / 'vault';root.mkdir()
    note = root / '人工笔记.md';note.write_text('不要覆盖', encoding='utf-8')
    inbox = root / 'KR收件箱'
    result = materialize_bundle(raw, inbox, approved_root=root)
    assert result['files'] == 2
    assert note.read_text(encoding='utf-8') == '不要覆盖'
    assert next(inbox.glob('*.md')).read_text(encoding='utf-8') == normalized
    assert materialize_bundle(raw, inbox, approved_root=root)['already_exported'] is True
    next(inbox.glob('*.md')).write_text('user edit',encoding='utf-8')
    with pytest.raises(ValueError,match='existing_export_modified'):
        materialize_bundle(raw, inbox, approved_root=root)
    assets.withdraw(ref['source_id'])
    with pytest.raises(ValueError, match='withdrawn'): build_bundle(assets,[ref['version_id']],export_acknowledged=True)


def test_size_unicode_split_permission_and_zip_slip(tmp_path):
    assert ''.join(text for _,text in split_utf8('🙂中abc'*10, 11)) == '🙂中abc'*10
    assert all(len(text.encode())<=11 for _,text in split_utf8('🙂中abc'*10, 11))
    assets = EvidenceAssets(tmp_path / 'assets')
    ref = assets.put('https://example.org','正文')
    with pytest.raises(ValueError, match='permission'):build_bundle(assets,[ref['version_id']])
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as archive:
        archive.writestr('manifest.json',json.dumps({'schema':'knowledgeradar-text-evidence-bundle/v1','sources':[{'documents':[{'file':'../escape.md','sha256':'bad'}]}]}))
        archive.writestr('../escape.md','outside')
    with pytest.raises(ValueError,match='unsafe_bundle_path'): materialize_bundle(stream.getvalue(),tmp_path/'inbox',approved_root=tmp_path)
    assert not (tmp_path/'escape.md').exists()
