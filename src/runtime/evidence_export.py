"""精选证据包：固定原版本、LF/code point映射和有界文本，不写私人资料目录。"""
from __future__ import annotations
import hashlib
import io
import json
from pathlib import Path
import zipfile

MAX_TEXT_BYTES = 8 * 1024 * 1024


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def normalize_with_mapping(text):
    # 对连续相同偏移变化做游程编码；CRLF占两原始code point、一个LF。
    out, runs = [], []
    raw_pos, normalized_pos = 0, 0
    while raw_pos < len(text):
        char = text[raw_pos]
        crlf = char == '\r' and raw_pos + 1 < len(text) and text[raw_pos+1] == '\n'
        out.append('\n' if char == '\r' else char)
        raw_pos += 2 if crlf else 1
        normalized_pos += 1
        if crlf:
            if runs and runs[-1]['last'] + runs[-1]['step'] == normalized_pos:
                runs[-1]['last'] = normalized_pos
                runs[-1]['count'] += 1
            elif runs and runs[-1]['count'] == 1:
                runs[-1]['step'] = normalized_pos - runs[-1]['last']
                runs[-1]['last'] = normalized_pos
                runs[-1]['count'] = 2
            else:
                runs.append({'first': normalized_pos, 'last': normalized_pos, 'step': 0, 'count': 1})
    return ''.join(out), {'algorithm': 'crlf-removals-rle/v1', 'unit': 'unicode_code_point', 'runs': runs, 'rule': 'raw_offset = lf_offset + count(removal_positions <= lf_offset); standalone CR becomes LF without length change'}


def split_utf8(text, limit=MAX_TEXT_BYTES):
    chunk, size, start = [], 0, 0
    for char in text:
        width = len(char.encode('utf-8'))
        if size + width > limit:
            yield start, ''.join(chunk)
            start += len(chunk)
            chunk, size = [], 0
        chunk.append(char)
        size += width
    if chunk:
        yield start, ''.join(chunk)


def build_bundle(assets, version_ids, *, export_acknowledged=False, reports=None):
    if export_acknowledged is not True:
        raise ValueError('selected_source_export_permission_must_be_acknowledged')
    reports = reports or []
    if not isinstance(version_ids, list) or len(version_ids) > 100 or not (version_ids or reports):
        raise ValueError('select_1_to_100_fixed_versions')
    files, records, report_records = {}, [], []
    for index, report in enumerate(reports):
        normalized, mapping = normalize_with_mapping(report['text'])
        raw = normalized.encode('utf-8')
        if len(raw) > MAX_TEXT_BYTES:
            raise ValueError('report_too_large')
        name = f'report-{index + 1}.md'
        files[name] = raw
        report_records.append({'file':name, 'title':report['title'], 'sha256':sha(raw), 'bytes':len(raw), 'raw_sha256':sha(report['text'].encode('utf-8')), 'locator_mapping':mapping})
    for version in dict.fromkeys(version_ids):
        chunks, offset, asset, locators = [], 0, None, {}
        while True:
            read = assets.fetch(version, start=offset, max_chars=24000)
            chunks.append(read['text'])
            asset = read['asset']
            for locator in read.get('locators', []): locators[json.dumps(locator, sort_keys=True)] = locator
            if read['next_start'] is None:
                break
            offset = read['next_start']
        raw_text = ''.join(chunks)
        normalized, mapping = normalize_with_mapping(raw_text)
        documents = []
        for index, (start, text) in enumerate(split_utf8(normalized)):
            name = f"{asset['source_id']}-{version[:16]}-{index+1}.md"
            raw = text.encode('utf-8')
            files[name] = raw
            documents.append({'file': name, 'sha256': sha(raw), 'bytes': len(raw), 'lf_start': start, 'lf_end': start+len(text)})
        records.append({'asset': asset, 'raw_sha256': sha(raw_text.encode()), 'lf_sha256': sha(normalized.encode()), 'locator_mapping': mapping, 'documents': documents, 'coverage': asset['coverage'], 'missing': ['media_body_not_in_text_package'], 'permission': 'operator_acknowledged_selected_sources', 'revocation': 'source withdrawal blocks future fetch/export; previously exported copies require explicit destination withdrawal'})
        records[-1]['original_locators'] = list(locators.values())
        if sum(map(len, files.values())) > 64 * 1024 * 1024: raise ValueError('bundle_too_large_select_fewer_versions')
    read_only = bool(getattr(assets, 'read_only', False))
    manifest = {'schema': 'knowledgeradar-text-evidence-bundle/v1', 'sources': records, 'source_reference_registration': 'not_registered_read_only_export' if read_only else 'registered', 'menslink': {'mode': 'explicit_cli_preview_import', 'max_file_bytes': MAX_TEXT_BYTES, 'compatibility': 'menslink-p1/v1 subset; no automatic synchronization'}, 'obsidian': {'mode': 'explicit_inbox_only', 'overwrite': False}}
    manifest['reports'] = report_records
    manifest_raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode()
    if len(manifest_raw) > MAX_TEXT_BYTES or sum(map(len, files.values())) > 64 * 1024 * 1024:
        raise ValueError('bundle_too_large_select_fewer_versions')
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in [('manifest.json', manifest_raw), *sorted(files.items())]:
            item = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(item, raw)
    if not read_only:
        for record in records:
            assets.reference(record['asset']['version_id'], 'bundle:' + sha(manifest_raw))
    return stream.getvalue(), manifest


def write_bundle(raw, destination):
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as handle:
        handle.write(raw)
    return {'path': str(path.resolve()), 'sha256': sha(raw), 'bytes': len(raw)}


def materialize_bundle(raw, inbox, *, approved_root):
    root, target = Path(approved_root).resolve(), Path(inbox).resolve()
    if target == root or not target.is_relative_to(root):
        raise ValueError('dedicated_inbox_inside_approved_root_required')
    for path in (Path(approved_root), *Path(approved_root).parents, Path(inbox), *Path(inbox).parents):
        if path.exists() and (path.is_symlink() or path.is_junction()):
            raise ValueError('links_not_allowed')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names) or len(names) > 500 or sum(i.file_size for i in archive.infolist()) > 72 * 1024 * 1024:
            raise ValueError('invalid_bundle_size')
        manifest = json.loads(archive.read('manifest.json'))
        if manifest.get('schema') != 'knowledgeradar-text-evidence-bundle/v1':
            raise ValueError('unsupported_bundle')
        expected = {'manifest.json'}
        for source in [*manifest['sources'], {'documents':manifest.get('reports', [])}]:
            for item in source['documents']:
                name = item['file']
                if Path(name).name != name or '/' in name or '\\' in name or not name.endswith('.md'):
                    raise ValueError('unsafe_bundle_path')
                content = archive.read(name)
                if len(content) > MAX_TEXT_BYTES or sha(content) != item['sha256']:
                    raise ValueError('bundle_integrity_mismatch')
                content.decode('utf-8', errors='strict')
                expected.add(name)
        if set(names) != expected:
            raise ValueError('unlisted_bundle_files')
        # 全部文件落盘后发布目录；失败不会留下半份可见成果。
        import tempfile
        import shutil
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if not target.is_dir() or any(not (target / name).is_file() or (target / name).is_symlink() or (target / name).read_bytes() != archive.read(name) for name in names):
                raise ValueError('existing_export_modified_choose_new_destination')
            return {'inbox': str(target), 'files': len(names), 'automatic_sync': False, 'already_exported': True}
        staging = Path(tempfile.mkdtemp(prefix='.kr-export-', dir=target.parent))
        try:
            for name in names:
                with (staging / name).open('xb') as handle: handle.write(archive.read(name))
            staging.rename(target)
        finally:
            if staging.exists(): shutil.rmtree(staging)
    return {'inbox': str(target), 'files': len(names), 'automatic_sync': False}
