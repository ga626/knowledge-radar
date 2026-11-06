"""GitHub 对象读取：固定提交的文件/目录，及有界、可续页的讨论。"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
import time
import uuid
from typing import Literal
from urllib.parse import quote, unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from collectors.platform.github_api import CONTRACT, GitHubClient, GitHubError
from kr_core.evidence_assets import EvidenceAssets
from kr_core.models import utc_now_iso
from runtime.execution_context import check_execution
from runtime.operation_checkpoint import OperationCheckpoint
from runtime.paths import runtime_state_dir


class GitHubReadOptions(BaseModel):
    """只指定对象范围，不暴露命令、账号或任意 API 请求。"""
    model_config = ConfigDict(extra='forbid')
    kind: Literal['auto', 'overview', 'readme', 'file', 'tree', 'issue', 'pull_request'] = 'auto'
    ref: str = Field(default='', max_length=200, description='分支、标签或commit；歧义URL须显式指定。请求执行后固定到commit。')
    path: str | None = Field(default=None, max_length=1000, description='仓库相对文件/目录；不接受本机路径。')
    page: int = Field(default=1, ge=1, le=10000)
    per_page: int = Field(default=30, ge=1, le=100)
    max_pages: int = Field(default=1, ge=1, le=10, description='每个所选评论流最多读取的页数；返回下一页，绝不冒充全量。')
    comment_stream: Literal['all', 'issue_comments', 'reviews', 'review_comments'] = 'all'
    refresh: bool = Field(default=False, description='重新获取已固定提交的文件；任务恢复仍沿原commit与已完成页。')

    @model_validator(mode='after')
    def safe_paths(self):
        for name, value in [('path', self.path), ('ref', self.ref)]:
            if value is None:
                continue
            if '\\' in value or '\x00' in value or any(ord(x) < 32 for x in value) or value.startswith('/') or any(x in {'.', '..'} for x in value.split('/')):
                raise ValueError(name + '_must_be_a_safe_repository_relative_value')
        return self


def matches_url(url):
    try:
        return urlsplit(url).hostname in {'github.com', 'www.github.com'}
    except ValueError:
        return False


def parse_target(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in {'github.com', 'www.github.com'} or parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise GitHubError('INVALID_URL', 'Use an HTTPS github.com repository/object URL without credentials.')
    parts = [unquote(p) for p in parsed.path.strip('/').split('/')]
    if len(parts) < 2 or not all(re.fullmatch(r'[A-Za-z0-9_.-]+', p or '') and p not in {'.', '..'} for p in parts[:2]):
        raise GitHubError('INVALID_URL', 'Expected https://github.com/owner/repository[/object].')
    owner, repo = parts[0], parts[1].removesuffix('.git')
    tail = parts[2:]
    kind, number = 'overview', None
    if tail:
        if tail[0] in {'blob', 'tree'}:
            kind = 'file' if tail[0] == 'blob' else 'tree'
        elif tail[0] in {'issues', 'pull'} and len(tail) >= 2 and tail[1].isdecimal():
            kind, number = ('issue' if tail[0] == 'issues' else 'pull_request'), int(tail[1])
            if len(tail) > 2:
                raise GitHubError('UNSUPPORTED_OBJECT', 'Use the Issue/PR root URL; files/diffs are separate source objects.')
        else:
            raise GitHubError('UNSUPPORTED_OBJECT', 'Supported GitHub objects: repository, README, tree, blob, Issue and PR. Discussions/releases/wiki require a separate reader.')
    return {'repository': owner + '/' + repo, 'kind': kind, 'number': number, 'tail': tail,
            'fragment': parsed.fragment}


def line_locators(text, *, commit, path):
    lines = text.splitlines(keepends=True)
    step = max(1, math.ceil(len(lines) / 40000))
    result, offset = [], 0
    for start in range(0, len(lines), step):
        value = ''.join(lines[start:start + step])
        if value:
            result.append({'start': offset, 'end': offset + len(value), 'unit': 'unicode_code_point',
                'kind': 'source_lines', 'commit': commit, 'path': path,
                'line_start': start + 1, 'line_end': min(len(lines), start + step)})
            offset += len(value)
    return result


class GitHubReader:
    def __init__(self, request, *, client=None, assets=None):
        self.request = request
        self.options = GitHubReadOptions.model_validate(request.options.get('github') or {})
        self.target = parse_target(request.url)
        self.client = client or GitHubClient.configured()
        self.assets = assets or EvidenceAssets()
        scope = request.options.get('github_checkpoint_id') or uuid.uuid4().hex
        self.checkpoint = OperationCheckpoint(runtime_state_dir() / 'github/checkpoints',
            {'scope': scope, 'url': request.url, 'options': self.options.model_dump(),
             'comments': request.options.get('include_comments')},
            {'contract': CONTRACT, 'auth_context': self.client.identity})
        saved = self.checkpoint._load()
        if saved and saved.get('identity') != self.checkpoint.identity:
            raise GitHubError('AUTH_CONTEXT_CHANGED', 'This task checkpoint belongs to another authentication context; start a new explicit request.')
        if saved and time.time() - saved.get('at', 0) > 86400:
            raise GitHubError('CHECKPOINT_EXPIRED', 'The saved discussion/ref snapshot expired; start a new explicit request rather than silently following a new head.')
        self.state = self.checkpoint.read() or {'objects': {}, 'acquired_at': utc_now_iso()}
        self.base = 'repos/' + self.target['repository']

    def _load(self, version):
        with self.assets.connection() as conn:
            row = conn.execute('SELECT * FROM assets WHERE version_id=?', (version,)).fetchone()
            if row is None:
                raise GitHubError('CHECKPOINT_ASSET_MISSING', 'A saved GitHub page is missing; start a new explicit request.')
            row = dict(row)
            self.assets.assert_available(conn, row)
            return json.loads(self.assets._verified_text(row))

    def _object(self, key, endpoint, params=None):
        check_execution()
        if key in self.state['objects']:
            saved = self._load(self.state['objects'][key])
            return saved['payload'], saved['headers']
        payload, headers = self.client.json(endpoint, params=params)
        if key == 'repository' and (not isinstance(payload, dict) or payload.get('private') is not False
                                   or payload.get('visibility', 'public') != 'public'):
            raise GitHubError('PRIVATE_SCOPE_NOT_AUTHORIZED', 'Only confirmed public repository metadata may enter this research store.')
        snapshot = {'payload': payload, 'headers': {k: v for k, v in headers.items() if k.lower() in {
            'link', 'etag', 'last-modified', 'x-github-request-id'}}, 'acquired_at': utc_now_iso()}
        ref = self.assets.put(self.request.url, json.dumps(snapshot, ensure_ascii=False), kind='github_checkpoint',
            metadata={'extractor': CONTRACT, 'evidence_identity': self.checkpoint.key + ':' + key})
        self.state['objects'][key] = ref['version_id']
        self.checkpoint.write(self.state)
        return payload, snapshot['headers']

    def read(self):
        repo, _ = self._object('repository', self.base)
        if not isinstance(repo, dict) or not (repo.get('private') is False and repo.get('visibility', 'public') == 'public'):
            raise GitHubError('PRIVATE_SCOPE_NOT_AUTHORIZED', 'This request is restricted to confirmed public repositories.')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', str(repo.get('full_name') or '')):
            raise GitHubError('SCHEMA_CHANGED', 'Repository identity is missing.')
        self.base = 'repos/' + repo['full_name']
        inferred = self.target['kind']
        kind = inferred if self.options.kind == 'auto' else self.options.kind
        if inferred in {'issue', 'pull_request'} and kind != inferred:
            raise GitHubError('OBJECT_KIND_MISMATCH', 'The requested kind must match this discussion URL.')
        if kind in {'issue', 'pull_request'} and not self.target['number']:
            raise GitHubError('INVALID_URL', 'An Issue/PR number must be present in the URL.')
        self.github = {'repository': repo['full_name'], 'repository_id': repo['id'],
            'object_kind': kind, 'public_only': True, 'contract': CONTRACT,
            'transport': self.client.transport, 'acquired_at': self.state['acquired_at']}
        if kind in {'issue', 'pull_request'}:
            result = self._discussion(kind)
        else:
            ref, path = self._ref_path(repo, kind)
            commit, _ = self._object('commit:' + ref, self.base + '/commits/' + quote(ref, safe=''))
            sha = commit.get('sha', '')
            if not re.fullmatch(r'[a-f0-9]{40,64}', sha):
                raise GitHubError('SCHEMA_CHANGED', 'GitHub did not resolve an immutable commit.')
            self.commit = commit
            self.github.update(requested_ref=ref, commit=sha, path=path,
                source_mutability='immutable_commit', ref_resolved_at=self.state['acquired_at'])
            if kind in {'overview', 'tree'}:
                result = self._tree(repo, commit, path, kind)
            else:
                result = self._file(path, kind)
        if not result.get('content') and result.get('body_complete') is True:
            result['content_observation'] = 'observed_empty_original_text'
        if self.target['fragment']:
            self.github['requested_fragment'] = self.target['fragment'][:200]
        result.update(platform='GitHub', collector='github_objects', content_fidelity='public_source_original',
            fetched_at=utc_now_iso(), source_presentation={'label': 'GitHub', 'site': 'GitHub', 'method': '官方只读 API；' + ('gh认证传输' if self.client.transport == 'gh' else 'REST传输')},
            processing_method='GitHub原始对象读取，未做网页正文清洗',
            evidence_source_url=result['url'],
            provenance={'provider_id': 'github', 'source_url': self.request.url, 'resolved_url': result['url'],
                        'github': dict(self.github), 'processing_key': CONTRACT + ':' + self.github['repository'] + ':'
                            + str(self.github.get('commit') or self.github.get('number')) + ':' + str(self.github.get('path', ''))},
            metadata={'strategy': 'github_objects', 'coverage': 'requested_object_scope',
                      'attempts': self.client.attempts, 'snapshot_atomic': False,
                      'checkpoint_reused': bool(self.state.get('completed_before'))})
        self.state['completed_before'] = True
        self.checkpoint.write(self.state)
        return result

    def _ref_path(self, repo, kind):
        tail = self.target['tail']
        chosen = self.options.ref or repo.get('default_branch', '')
        path = self.options.path or ''
        if self.target['kind'] in {'file', 'tree'}:
            rest = '/'.join(tail[1:])
            if self.options.ref:
                if rest != chosen and not rest.startswith(chosen + '/'):
                    raise GitHubError('REF_PATH_MISMATCH', 'github.ref must match the ref prefix in the URL.')
            elif rest.startswith(chosen + '/') or rest == chosen:
                pass
            elif re.fullmatch(r'[a-f0-9]{40,64}', tail[1] if len(tail) > 1 else ''):
                chosen = tail[1]
            else:
                raise GitHubError('AMBIGUOUS_REF_PATH', 'Provide github.ref explicitly for a non-default branch/tag; slash-containing refs cannot be guessed.')
            inferred_path = rest[len(chosen):].lstrip('/')
            if self.options.path is not None and self.options.path != inferred_path:
                raise GitHubError('REF_PATH_MISMATCH', 'github.path does not match the URL path.')
            path = inferred_path
        if not chosen:
            raise GitHubError('EMPTY_REPOSITORY', 'Repository has no resolvable default branch.')
        GitHubReadOptions(ref=chosen, path=path)
        if kind == 'file' and not path:
            raise GitHubError('FILE_PATH_REQUIRED', 'Specify github.path or a blob URL.')
        return chosen, path

    def _file(self, path, kind):
        sha = self.github['commit']
        if kind == 'file' and not self.options.refresh:
            cached = self._cached_file(path)
            if cached is not None:
                return cached
        endpoint = self.base + ('/readme' if kind == 'readme' else '/contents/' + quote(path, safe='/'))
        item, _ = self._object('file:' + sha + ':' + kind + ':' + path, endpoint, {'ref': sha})
        if not isinstance(item, dict) or item.get('type') not in {'file'}:
            raise GitHubError('NOT_A_REGULAR_FILE', 'Requested object is not a regular file; inspect its tree entry first.')
        if item.get('submodule_git_url') or item.get('target'):
            raise GitHubError('NONREGULAR_FILE', 'Submodule/symlink targets are not followed automatically.')
        entry = self._entry(item['path'])
        if entry.get('mode') not in {'100644', '100755'} or entry.get('sha') != item.get('sha'):
            raise GitHubError('NONREGULAR_FILE', 'File metadata does not match a regular Git blob in the selected tree.')
        limit = min(16 * 1024 * 1024, max(1, int(os.environ.get('KR_GITHUB_MAX_FILE_BYTES', str(2 * 1024 * 1024)))))
        if int(item.get('size', limit + 1)) > limit:
            raise GitHubError('CONTENT_TOO_LARGE', 'Source exceeds KR_GITHUB_MAX_FILE_BYTES; narrow the requested file scope.', size_bytes=item.get('size'), max_bytes=limit)
        if item.get('encoding') == 'base64':
            try:
                raw = base64.b64decode(re.sub(r'\s+', '', item.get('content') or ''), validate=True)
            except (ValueError, binascii.Error):
                raise GitHubError('SCHEMA_CHANGED', 'File base64 payload is invalid.') from None
        else:
            raw, _ = self.client.get(endpoint, params={'ref': sha}, accept='application/vnd.github.raw+json', max_bytes=limit)
        if len(raw) != item.get('size') or len(raw) > limit:
            raise GitHubError('CONTENT_INTEGRITY_FAILED', 'File byte count differs from GitHub metadata.')
        blob = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\x00' + raw).hexdigest()
        if re.fullmatch(r'[a-f0-9]{40}', str(item.get('sha', ''))) and blob != item['sha']:
            raise GitHubError('CONTENT_INTEGRITY_FAILED', 'File payload differs from its Git blob identity.')
        if raw.startswith(b'version https://git-lfs.github.com/spec/v1'):
            raise GitHubError('LFS_POINTER', 'This is an LFS pointer; large-object download is not authorized by a text read.')
        try:
            text = raw.decode('utf-8')
        except UnicodeError:
            raise GitHubError('UNSUPPORTED_ENCODING', 'Source is not UTF-8; no lossy conversion was applied.') from None
        if '\x00' in text:
            raise GitHubError('BINARY_CONTENT', 'Binary content is not a readable source file.')
        path = item['path']
        url = 'https://github.com/' + self.github['repository'] + '/blob/' + sha + '/' + quote(path, safe='/')
        self.github.update(path=path, blob_sha=item.get('sha'), input_sha256=hashlib.sha256(raw).hexdigest(),
                           encoding='utf-8', bytes=len(raw), content_modified=False)
        return {'status': 'ok', 'url': url, 'title': path, 'content': text,
                'content_format': 'markdown' if path.lower().endswith(('.md', '.markdown')) else 'code',
                'body_complete': True, 'input_sha256': self.github['input_sha256'],
                'content_locators': line_locators(text, commit=sha, path=path)}

    def _entry(self, path):
        tree_sha = self.commit['commit']['tree']['sha']
        parts = path.split('/')
        for index, segment in enumerate(parts):
            tree, _ = self._object('tree:' + tree_sha, self.base + '/git/trees/' + tree_sha)
            node = next((row for row in tree.get('tree', []) if row.get('path') == segment), None)
            if node is None:
                raise GitHubError('TREE_TRUNCATED' if tree.get('truncated') else 'PATH_NOT_FOUND',
                                  'Cannot verify this file in the selected Git tree.')
            if index == len(parts) - 1:
                return node
            if node.get('type') != 'tree':
                raise GitHubError('NONREGULAR_FILE', 'A path component is not a directory.')
            tree_sha = node['sha']

    def _cached_file(self, path):
        from kr_core.source_urls import canonical_url
        url = 'https://github.com/' + self.github['repository'] + '/blob/' + self.github['commit'] + '/' + quote(path, safe='/')
        with self.assets.connection() as conn:
            row = conn.execute("SELECT a.* FROM assets a JOIN sources s USING(source_id) WHERE a.source_url=? AND a.kind='content' AND s.withdrawn=0 ORDER BY a.captured_at DESC LIMIT 1", (canonical_url(url),)).fetchone()
            if row is None:
                return None
            row = dict(row)
            meta = json.loads(row['metadata'])
            github = (meta.get('provenance') or {}).get('github') or {}
            if github.get('contract') != CONTRACT or github.get('commit') != self.github['commit'] or github.get('path') != path:
                return None
            self.assets.assert_available(conn, row)
            text = self.assets._verified_text(row)
        self.github.update({key: github[key] for key in ('blob_sha', 'input_sha256', 'encoding', 'bytes', 'content_modified') if key in github})
        self.github.update(reused_from_version=row['version_id'], file_network_reverified=False)
        return {'status':'ok', 'url':url, 'title':path, 'content':text, 'body_complete':True,
                'content_format':meta.get('content_format','code'), 'input_sha256':github.get('input_sha256'),
                'content_locators':meta.get('locators', [])}

    def _tree(self, repo, commit, path, kind):
        tree_sha = commit['commit']['tree']['sha']
        traversed = ''
        for segment in [p for p in path.split('/') if p]:
            tree, _ = self._object('tree:' + tree_sha, self.base + '/git/trees/' + tree_sha)
            node = next((r for r in tree.get('tree', []) if r.get('path') == segment and r.get('type') == 'tree'), None)
            if not node:
                raise GitHubError('PATH_NOT_FOUND', 'Directory path is absent from the selected commit.')
            tree_sha = node['sha']; traversed += '/' + segment
        tree, _ = self._object('tree:' + tree_sha, self.base + '/git/trees/' + tree_sha)
        entries = [{'path': r['path'], 'kind': 'submodule' if r.get('mode') == '160000' else 'symlink' if r.get('mode') == '120000' else r.get('type'),
                    'sha': r['sha'], 'size': r.get('size')} for r in tree.get('tree', [])]
        data = {'repository': repo['full_name'], 'commit': self.github['commit'], 'path': path, 'entries': entries}
        if kind == 'overview':
            data['metadata'] = {k: repo.get(k) for k in ('description', 'default_branch', 'language', 'license', 'archived', 'stargazers_count')}
            data['reading_hint'] = 'Choose github.kind=readme or file/tree plus path; metadata and stars do not prove implementation.'
        self.github['tree_sha'] = tree_sha
        self.github['tree_truncated'] = bool(tree.get('truncated'))
        return {'status': 'partial' if tree.get('truncated') else 'ok',
            'url': 'https://github.com/' + repo['full_name'] + '/tree/' + self.github['commit'] + ('/' + quote(path, safe='/') if path else ''),
            'title': path or repo['full_name'], 'content': json.dumps(data, ensure_ascii=False, indent=2),
            'content_format': 'json', 'body_complete': not bool(tree.get('truncated')),
            'body_gap': 'github_tree_truncated' if tree.get('truncated') else ''}

    def _discussion(self, kind):
        number = self.target['number']
        endpoint = self.base + ('/pulls/' if kind == 'pull_request' else '/issues/') + str(number)
        item, _ = self._object('discussion-body', endpoint)
        if kind == 'issue' and item.get('pull_request'):
            kind = 'pull_request'
            item, _ = self._object('pull-body', self.base + '/pulls/' + str(number))
        self.github.update(object_kind=kind, number=number, object_id=item.get('id'),
            updated_at=item.get('updated_at'), source_mutability='mutable_discussion',
            snapshot_atomic=False, head_commit=(item.get('head') or {}).get('sha'), base_commit=(item.get('base') or {}).get('sha'))
        result = {'status': 'ok', 'url': item.get('html_url', self.request.url), 'title': item.get('title', ''),
            'content': item.get('body') or '', 'content_format': 'markdown', 'body_complete': True,
            'author': (item.get('user') or {}).get('login', ''), 'published_at': item.get('created_at', ''),
            'updated_at': item.get('updated_at'), 'comments': [], 'comments_complete': True}
        if not self.request.options.get('include_comments'):
            result.pop('comments_complete')
            return result
        streams = {'issue_comments': self.base + '/issues/' + str(number) + '/comments'}
        if kind == 'pull_request':
            streams.update(reviews=self.base + '/pulls/' + str(number) + '/reviews',
                           review_comments=self.base + '/pulls/' + str(number) + '/comments')
        selected = self.options.comment_stream
        if selected != 'all':
            if selected not in streams:
                raise GitHubError('INVALID_COMMENT_STREAM', 'This object only supports: ' + ', '.join(streams))
            streams = {selected: streams[selected]}
        coverage = {}
        common=self.request.options.get('comments') or {}
        total_limit=common.get('total_limit',500)
        reply_counts={}
        for name, url in streams.items():
            pages, exhausted, next_page = [], False, self.options.page
            stream_budget=False
            root_count=0
            root_window=False
            try:
                if len(result['comments'])>=total_limit:
                    coverage[name]={'pages':[],'requested_window_complete':False,'all_comments_exhausted':False,'next_page':self.options.page,'stop_reason':'total_budget'}
                    result.update(status='partial',comments_complete=False)
                    continue
                for page in range(self.options.page, self.options.page + self.options.max_pages):
                    data, headers = self._object(name + ':' + str(page), url,
                                                 {'page': page, 'per_page': self.options.per_page})
                    if not isinstance(data, list):
                        raise GitHubError('SCHEMA_CHANGED', 'GitHub comment page is not an array.')
                    pages.append(page)
                    for row in data:
                        parent=row.get('in_reply_to_id') if name=='review_comments' else None
                        parent_key=(name,str(parent))
                        if parent and reply_counts.get(parent_key,0)>=common.get('reply_limit',100):
                            continue
                        if not parent and root_count>=common.get('root_limit',500):
                            root_window=True
                            break
                        if len(result['comments'])>=total_limit:
                            stream_budget=True
                            break
                        if parent:
                            reply_counts[parent_key]=reply_counts.get(parent_key,0)+1
                        else:
                            root_count+=1
                        result['comments'].append({'comment_id': str(row['id']), 'discussion_kind': name,
                            'body': row.get('body') or '', 'author': (row.get('user') or {}).get('login', ''),
                            'created_at': row.get('created_at') or row.get('submitted_at'), 'updated_at': row.get('updated_at'),
                            'url': row.get('html_url', ''),
                            **{k: row[k] for k in ('in_reply_to_id', 'pull_request_review_id', 'path', 'line', 'start_line',
                               'original_line', 'commit_id', 'original_commit_id', 'diff_hunk', 'state') if k in row}})
                    more = bool(re.search(r'<[^>]+>;\s*rel="next"', headers.get('link', '')))
                    exhausted, next_page = not more, page + 1 if more else None
                    if exhausted or stream_budget or root_window:
                        break
                if root_window:
                    exhausted=False;next_page=page
                if stream_budget:
                    exhausted=False;next_page=page
                    result.update(status='partial',comments_complete=False)
                coverage[name] = {'pages': pages, 'requested_window_complete': not stream_budget,
                                  'stop_reason':'comment_scope_budget_mid_page' if stream_budget else 'requested_root_window' if root_window else 'requested_pages',
                                  'all_comments_exhausted': exhausted, 'next_page': next_page,
                                  'per_page': self.options.per_page}
            except GitHubError as exc:
                coverage[name] = {'pages': pages, 'requested_window_complete': False,
                                  'all_comments_exhausted': False, 'next_page': next_page, 'error': exc.result()['error']}
                result.update(status='partial', comments_complete=False)
        result['comments_pagination'] = coverage
        result['comments_coverage'] = 'observed_zero_comments' if not result['comments'] and all(
            r['requested_window_complete'] and r['all_comments_exhausted'] for r in coverage.values()) and self.options.page == 1 else 'requested_window'
        result['comments_complete_meaning'] = 'requested_page_window_only; see all_comments_exhausted for each stream'
        self.github['discussion_window'] = {'started_at': self.state['acquired_at'], 'finished_at': utc_now_iso(),
                                           'pagination': coverage}
        return result


def collect(request, *, client=None, assets=None):
    try:
        return GitHubReader(request, client=client, assets=assets).read()
    except GitHubError as exc:
        return {**exc.result(), 'url': request.url, 'content': '', 'body_complete': False}
