"""不可变完整证据、固定版本回读和显式撤销；摘要不参与资产身份。"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import tempfile

from runtime.paths import runtime_state_dir
from runtime.research_storage import guarded_transaction, admit_bytes


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class EvidenceAssets:
    def __init__(self, root=None, *, max_bytes=None, max_physical_bytes=None, read_only=False):
        self.root = Path(root or os.environ.get('KR_EVIDENCE_ROOT') or runtime_state_dir() / 'evidence_assets')
        self.max_bytes = max_bytes
        self.max_physical_bytes = max_physical_bytes
        self.read_only = read_only

    @guarded_transaction
    @contextmanager
    def connection(self):
        if self.read_only:
            database = self.root / 'index.sqlite3'
            if not database.is_file():
                raise ValueError('evidence_store_not_created')
            conn = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute('PRAGMA query_only=ON')
                yield conn
            finally:
                conn.close()
            return
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.root / 'index.sqlite3', timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS assets(
                    version_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_url TEXT NOT NULL,
                    kind TEXT NOT NULL, title TEXT NOT NULL, raw_sha256 TEXT NOT NULL,
                    normalized_sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                    chars INTEGER NOT NULL, captured_at REAL NOT NULL, metadata TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sources(source_id TEXT PRIMARY KEY, withdrawn INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS asset_refs(version_id TEXT NOT NULL, owner TEXT NOT NULL,
                    PRIMARY KEY(version_id,owner));
                CREATE TABLE IF NOT EXISTS knowledge_meta(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
                INSERT OR IGNORE INTO knowledge_meta VALUES('generation',0);
                CREATE TABLE IF NOT EXISTS asset_scopes(version_id TEXT NOT NULL,project_id TEXT NOT NULL,task_id TEXT NOT NULL,PRIMARY KEY(version_id,project_id,task_id));
                CREATE TABLE IF NOT EXISTS archive_dirty(task_id TEXT PRIMARY KEY, revision INTEGER NOT NULL);
                CREATE VIRTUAL TABLE IF NOT EXISTS asset_fts USING fts5(version_id UNINDEXED, body, tokenize='trigram');
            ''')
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _file(self, sha):
        if not re.fullmatch(r'[a-f0-9]{64}', str(sha)):
            raise ValueError('invalid_asset_identity')
        return self.root / (sha + '.utf8')

    def put(self, url, text, *, kind='content', title='', metadata=None):
        if self.read_only:
            raise ValueError('evidence_read_only')
        from kr_core.source_urls import canonical_url, public_evidence_payload
        if not isinstance(text, str) or not text.strip():
            raise ValueError('empty_evidence')
        raw = text.encode('utf-8')
        if len(raw) > 32 * 1024 * 1024:
            raise ValueError('evidence_too_large')
        url = canonical_url(str(url))
        source_id = digest(url.encode('utf-8'))[:24]
        raw_sha = digest(raw)
        normalized = text.replace('\r\n', '\n').replace('\r', '\n')
        meta = public_evidence_payload(dict(metadata or {}))
        from .evidence_locators import validate_locators, paragraph_locators
        meta['locators'] = validate_locators(text, meta.get('locators', paragraph_locators(text)))
        locator_sha = digest(json.dumps(meta['locators'], sort_keys=True, ensure_ascii=False).encode())
        identity = [source_id, kind, raw_sha, meta.get('extractor', 'unknown'), locator_sha]
        if meta.get('evidence_identity'):
            identity.append(meta['evidence_identity'])
        version = digest(json.dumps(identity, ensure_ascii=False).encode())
        with self.connection() as conn:
            conn.execute('INSERT OR IGNORE INTO sources(source_id) VALUES(?)', (source_id,))
            if conn.execute('SELECT withdrawn FROM sources WHERE source_id=?', (source_id,)).fetchone()[0]:
                raise ValueError('source_withdrawn')
            existing = conn.execute('SELECT * FROM assets WHERE version_id=?', (version,)).fetchone()
            if existing:
                self._verified_text(dict(existing))
                return self._ref(dict(existing))
            path = self._file(raw_sha)
            used = conn.execute('SELECT COALESCE(SUM(size_bytes),0) FROM (SELECT raw_sha256,MAX(size_bytes) size_bytes FROM assets GROUP BY raw_sha256)').fetchone()[0]
            indexed = conn.execute('SELECT 1 FROM assets WHERE raw_sha256=? LIMIT 1', (raw_sha,)).fetchone()
            if self.max_bytes is not None and not indexed and used + len(raw) > self.max_bytes:
                raise ValueError('evidence_capacity_exceeded')
            row = (version, source_id, url, str(kind), str(title)[:1024], raw_sha, digest(normalized.encode()), len(raw), len(text), time.time(), json.dumps(meta, ensure_ascii=False))
            conn.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?,?)', row)
            conn.execute('INSERT INTO asset_fts(version_id,body) VALUES(?,?)', (version, normalized))
            conn.execute("UPDATE knowledge_meta SET value=value+1 WHERE key='generation'")
            # 包括当前事务尚未刷盘的索引页；原件发布前进行物理容量准入。
            database_bytes = conn.execute('PRAGMA page_count').fetchone()[0] * conn.execute('PRAGMA page_size').fetchone()[0]
            other_bytes = sum(p.stat().st_size for p in self.root.iterdir() if p.is_file() and p.name != 'index.sqlite3')
            if self.max_physical_bytes is not None and database_bytes + other_bytes + (0 if path.exists() else len(raw)) > self.max_physical_bytes:
                raise ValueError('evidence_physical_capacity_exceeded')
            admit_bytes(self.root, len(raw)*4 + 1024*1024)
            if path.exists():
                if digest(path.read_bytes()) != raw_sha:
                    raise ValueError('asset_integrity_mismatch') from None
            else:
                # SQLite写锁串行化本库写入；原件完成落盘后才发布文件名。
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=self.root, prefix='.asset-', suffix='.tmp', delete=False) as handle:
                        temporary = Path(handle.name)
                        handle.write(raw)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, path)
                finally:
                    if temporary and temporary.exists():
                        temporary.unlink()
            result = dict(conn.execute('SELECT * FROM assets WHERE version_id=?', (version,)).fetchone())
        return self._ref(result)

    @staticmethod
    def _ref(row):
        meta = json.loads(row['metadata'])
        provenance = meta.get('provenance') if isinstance(meta.get('provenance'), dict) else {}
        provenance = {key: provenance[key] for key in ('provider_id', 'source_url', 'resolved_url', 'mode', 'coverage', 'pdf_processing', 'ocr_images', 'comments_metadata', 'academic', 'github', 'input_sha256', 'structure', 'reuse', 'location') if key in provenance}
        provenance.update({key: meta[key] for key in ('parent_version_id', 'legacy_source_sha256', 'completeness_proven', 'derived_from', 'project_id', 'task_id', 'report_id', 'input_sha256') if key in meta})
        return {k: v for k, v in row.items() if k != 'metadata'} | {'locator_unit': 'unicode_code_point', 'coverage': meta.get('coverage', 'unknown'), 'extractor': meta.get('extractor', 'unknown'), 'locator_count': len(meta.get('locators') or []), 'provenance': provenance}

    def _verified_text(self, row):
        path = self._file(row['raw_sha256'])
        if not path.is_file():
            raise ValueError('asset_missing')
        raw = path.read_bytes()
        if digest(raw) != row['raw_sha256']:
            raise ValueError('asset_integrity_mismatch')
        return raw.decode('utf-8')

    def assert_available(self, conn, row, seen=None):
        seen = set() if seen is None else seen
        pending = [row]
        while pending:
            item = pending.pop()
            if item['version_id'] in seen: continue
            seen.add(item['version_id'])
            state = conn.execute('SELECT withdrawn FROM sources WHERE source_id=?',(item['source_id'],)).fetchone()
            if state is None or state[0]: raise ValueError('version_not_found_or_withdrawn')
            for parent in json.loads(item['metadata']).get('derived_from',[]):
                if parent in seen: continue
                origin = conn.execute('SELECT * FROM assets WHERE version_id=?',(parent,)).fetchone()
                if origin is None: raise ValueError('provenance_version_missing')
                pending.append(dict(origin))

    def fetch(self, version_id, *, start=0, max_chars=8000, line_start=None, line_end=None):
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            raise ValueError('invalid_locator')
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or not 1 <= max_chars <= 24000:
            raise ValueError('invalid_read_budget')
        with self.connection() as conn:
            row = conn.execute('SELECT a.* FROM assets a JOIN sources s USING(source_id) WHERE version_id=? AND s.withdrawn=0', (str(version_id),)).fetchone()
            if row is None:
                raise ValueError('version_not_found_or_withdrawn')
            row = dict(row)
            text = self._verified_text(row)
            self.assert_available(conn,row)
        if line_end is not None and line_start is None:
            raise ValueError('line_end_requires_line_start')
        line_stop = len(text)
        if line_start is not None:
            if start != 0 or isinstance(line_start, bool) or not isinstance(line_start, int) or line_start < 1:
                raise ValueError('invalid_source_line_range')
            if line_end is not None and (isinstance(line_end, bool) or not isinstance(line_end, int) or line_end < line_start):
                raise ValueError('invalid_source_line_range')
            lines = text.splitlines(keepends=True)
            if line_start > len(lines) or (line_end is not None and line_end > len(lines)):
                raise ValueError('invalid_source_line_range')
            start = sum(len(x) for x in lines[:line_start - 1])
            line_stop = sum(len(x) for x in lines[:line_end]) if line_end is not None else len(text)
        if start > len(text):
            raise ValueError('invalid_locator')
        end = min(line_stop, start + max_chars)
        locations = json.loads(row['metadata']).get('locators') or []
        return {'asset': self._ref(row), 'text': text[start:end], 'locator': {'start': start, 'end': end, 'unit': 'unicode_code_point', 'line_start': text[:start].count('\n') + 1}, 'locators': [loc for loc in locations if loc['start'] < end and loc['end'] > start], 'next_start': end if end < line_stop else None, 'truncated': end < line_stop}

    def search(self, query='', *, limit=20):
        if not isinstance(query, str) or len(query) > 500 or not 1 <= int(limit) <= 100:
            raise ValueError('invalid_query')
        if self.read_only and not (self.root / 'index.sqlite3').is_file():
            return []
        with self.connection() as conn:
            if len(query) >= 3:
                phrase = '"' + query.replace('"', '""') + '"'
                rows = conn.execute("SELECT a.* FROM asset_fts f JOIN assets a ON a.version_id=f.version_id JOIN sources s USING(source_id) WHERE asset_fts MATCH ? AND s.withdrawn=0 AND a.kind NOT IN ('academic_card','academic_inline','document_processing','document_receipt','github_checkpoint','media_ledger') ORDER BY a.captured_at DESC LIMIT ?", (phrase, int(limit))).fetchall()
            elif query:
                # 短词按字面匹配，不把用户输入当SQL通配模式。
                rows = conn.execute("SELECT a.* FROM asset_fts f JOIN assets a ON a.version_id=f.version_id JOIN sources s USING(source_id) WHERE instr(f.body,?)>0 AND s.withdrawn=0 AND a.kind NOT IN ('academic_card','academic_inline','document_processing','document_receipt','github_checkpoint','media_ledger') ORDER BY a.captured_at DESC LIMIT ?", (query, int(limit))).fetchall()
            else:
                rows = conn.execute("SELECT a.* FROM assets a JOIN sources s USING(source_id) WHERE s.withdrawn=0 AND a.kind NOT IN ('academic_card','academic_inline','document_processing','document_receipt','github_checkpoint','media_ledger') ORDER BY captured_at DESC LIMIT ?", (int(limit),)).fetchall()
        return [self._ref(dict(row)) for row in rows]

    def withdraw(self, source_id):
        with self.connection() as conn:
            changed = conn.execute('UPDATE sources SET withdrawn=1 WHERE source_id=? AND withdrawn=0', (str(source_id),)).rowcount
            if changed:
                conn.execute("UPDATE knowledge_meta SET value=value+1 WHERE key='generation'")
        return {'withdrawn': bool(changed), 'physical_deletion': False}

    def reference(self, version_id, owner):
        self.reference_many([version_id], owner)

    def reference_many(self, version_ids, owner, *, project_id="", task_id=""):
        """同一事务校验并保护整批固定版本；缺件/撤回不留下半批引用。"""
        if not owner or len(owner) > 200:
            raise ValueError('invalid_reference')
        with self.connection() as conn:
            for version_id in dict.fromkeys(version_ids):
                row = conn.execute('SELECT a.* FROM assets a JOIN sources s USING(source_id) WHERE version_id=? AND s.withdrawn=0', (version_id,)).fetchone()
                if row is None:
                    raise ValueError('version_not_found_or_withdrawn')
                self._verified_text(dict(row))
                self.assert_available(conn,dict(row))
                conn.execute('INSERT OR IGNORE INTO asset_refs VALUES(?,?)', (version_id, owner))
                scope_task = task_id or (owner if owner.startswith('kr-research-') else '')
                if scope_task:
                    changed = conn.execute('INSERT OR IGNORE INTO asset_scopes VALUES(?,?,?)', (version_id, project_id, scope_task)).rowcount
                    if changed:
                        conn.execute("UPDATE knowledge_meta SET value=value+1 WHERE key='generation'")
                        conn.execute('INSERT INTO archive_dirty VALUES(?,1) ON CONFLICT(task_id) DO UPDATE SET revision=revision+1', (scope_task,))

    def retention_plan(self):
        with self.connection() as conn:
            refs = conn.execute('SELECT COUNT(*) FROM asset_refs').fetchone()[0]
            size = conn.execute('SELECT COALESCE(SUM(size_bytes),0) FROM assets').fetchone()[0]
        physical = sum(path.stat().st_size for path in self.root.iterdir() if path.is_file() and not path.is_symlink())
        return {'automatic_deletion': False, 'reference_count': refs, 'logical_bytes': size, 'physical_bytes': physical, 'capacity_bytes': self.max_bytes, 'physical_capacity_bytes': self.max_physical_bytes, 'capacity_basis': 'original_content_and_total_physical_index_admission', 'policy': 'protected_evidence_not_cache'}
