"""有限公开GitHub候选验收：真实单内核、持久任务、证据、CLI/MCP；不安装或修改账号。"""
from pathlib import Path
import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--transport', choices=['rest', 'gh'], default='gh')
    parser.add_argument('--repository', default='psf/requests')
    parser.add_argument('--source-path', default='src/requests/__init__.py')
    parser.add_argument('--anonymous', action='store_true')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required for public network execution')
    if args.anonymous and args.transport != 'rest':
        parser.error('--anonymous requires --transport rest')
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    for key, name in {'KR_DATA_ROOT': 'data', 'KR_STATE_DIR': 'state', 'KR_LOG_DIR': 'logs',
                      'KR_EVIDENCE_ROOT': 'assets', 'KR_RUNTIME_LEASE_DB_PATH': 'leases.sqlite3',
                      'KR_OWNER_ROOT': 'owner', 'KR_TASK_DB_PATH': 'tasks.sqlite3',
                      'KR_PROFILE_REGISTRY_PATH': 'absent-profiles.json',
                      'KR_RUNTIME_ENV_PATH': 'absent.env'}.items():
        os.environ[key] = str(args.output / name)
    for key in ('KR_ACTIVE_INSTALL_ROOT', 'KR_RUNTIME_ROLE', 'KR_TASK_TEST_MODE'):
        os.environ.pop(key, None)
    os.environ.update(KR_GITHUB_TRANSPORT=args.transport, KR_GITHUB_ENABLED='1',
                      KR_GH_CLI_ENABLED='1', KR_DESKTOP_NOTIFICATIONS_DISABLED='1',
                      KR_DISABLE_MODEL_PREWARM='1', PYTHONIOENCODING='utf-8')
    if args.anonymous:
        for key in ('GH_TOKEN', 'GITHUB_TOKEN'):
            os.environ.pop(key, None)
    from runtime.execution_owner import OwnerClient, source_identity
    from kr_core.evidence_assets import EvidenceAssets
    from collectors.platform.github_api import GitHubClient
    from pydantic_core import to_jsonable_python
    owner = OwnerClient(timeout=90)
    receipt = {'schema': 'kr-github-candidate-live/v1', 'source_identity': source_identity(),
               'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
               'transport': args.transport, 'anonymous': args.anonymous,
               'scope': 'candidate_isolated_owner_not_installed_or_native_host', 'cases': []}

    def save(name, value):
        (args.output / (name + '.json')).write_text(
            json.dumps(to_jsonable_python(value), ensure_ascii=False, indent=2), 'utf-8')

    def call(name, tool, arguments):
        before = time.monotonic()
        result = owner.call(tool, arguments, request_id='github-live-' + uuid.uuid4().hex)
        save(name, {'arguments': arguments, 'result': result, 'execution_receipt': owner.last_receipt})
        if result.get('error'):
            raise AssertionError(name + ': ' + json.dumps(result['error'], ensure_ascii=False))
        receipt['cases'].append({'case': name, 'tool': tool, 'elapsed_s': round(time.monotonic() - before, 3)})
        return result

    def detail(name, url, github, **extra):
        submitted = call(name + '-submit', 'get_content_detail',
                         {'url': url, 'github': github, 'deadline_s': 180, **extra})
        assert submitted.get('task_id'), submitted
        end = time.monotonic() + 200
        while time.monotonic() < end:
            status = owner.call('get_task_status', {'task_id': submitted['task_id']})
            if status.get('status') not in {'queued', 'running'}:
                break
            time.sleep(.25)
        save(name, status)
        assert status.get('status') == 'completed', (name, status)
        result = status['result']
        refs = result.get('evidence_assets') or []
        assert any(r['kind'] == 'content' for r in refs), (name, result)
        readbacks = []
        for ref in refs:
            readback = call(name + '-read-' + ref['kind'], 'read_knowledge',
                            {'request': {'action': 'fetch', 'version_id': ref['version_id'], 'max_chars': 24000}})
            assert readback.get('text')
            readbacks.append({'kind': ref['kind'], 'version_id': ref['version_id'], 'chars': len(readback['text'])})
        receipt['cases'].append({'case': name, 'task_id': submitted['task_id'], 'readbacks': readbacks,
                                 'github': result.get('provenance', {}).get('github'),
                                 'comments_pagination': result.get('comments_pagination')})
        return result

    code = 1
    try:
        started = owner.start()
        assert started['status'] == 'ready', started
        receipt['owner'] = {k: started.get(k) for k in ('status', 'pid', 'generation', 'source_identity')}
        found = call('repository-search', 'search_github_repositories',
                     {'query': 'repo:' + args.repository, 'limit': 1, 'refresh': True})
        assert found.get('total') == 1
        empty = call('true-empty', 'search_github_repositories',
                     {'query': 'repo:' + args.repository + ' zzzzkrnomatch2026', 'limit': 1})
        assert empty['total'] == 0 and empty.get('status') == 'ok'
        url = 'https://github.com/' + args.repository
        detail('overview', url, {'kind': 'overview'})
        readme = detail('readme', url, {'kind': 'readme'})
        source = detail('source', url, {'kind': 'file', 'path': args.source_path})
        github = source['provenance']['github']
        ref = next(r for r in source['evidence_assets'] if r['kind'] == 'content')
        assert github['input_sha256'] == ref['raw_sha256']
        line = call('source-lines', 'read_knowledge', {'request': {'action': 'fetch',
                    'version_id': ref['version_id'], 'line_start': 2, 'line_end': 5}})
        exact = EvidenceAssets(read_only=True).fetch(ref['version_id'], max_chars=24000)['text']
        assert line['text'] == ''.join(exact.splitlines(keepends=True)[1:5])
        pinned = detail('pinned-cache', 'https://github.com/' + args.repository + '/blob/' + github['commit'] + '/' + args.source_path, {})
        assert pinned['provenance']['github']['commit'] == github['commit']
        assert pinned['provenance']['github'].get('file_network_reverified') is False
        # 只读API选择真实有评论样本；业务读取仍经上面的同一公开工具合同。
        api = GitHubClient.configured()
        for kind, query in [('issue', 'is:issue comments:>2'), ('pull_request', 'is:pr comments:>2')]:
            listing, _ = api.json('search/issues', params={'q': 'repo:' + args.repository + ' ' + query,
                                                         'per_page': 1, 'sort': 'updated'})
            assert listing.get('items'), (kind, 'no_public_sample')
            sample = listing['items'][0]['html_url']
            save(kind + '-sample-selection', {'url': sample, 'method': 'official_readonly_search_issues'})
            discussion = detail(kind, sample, {'kind': kind, 'per_page': 2, 'max_pages': 1}, include_comments=True)
            assert discussion.get('comments')
            if kind == 'issue' and discussion['comments_pagination']['issue_comments'].get('next_page'):
                detail('issue-page-two', sample, {'kind': kind, 'per_page': 2, 'page': 2}, include_comments=True)
        # 真CLI与MCP协议均连接上面的同一候选owner，未触碰Codex宿主原生卡片。
        source_input = args.output / 'cli-input.json'
        source_input.write_text(json.dumps({'action': 'fetch', 'version_id': ref['version_id'],
                                            'line_start': 2, 'line_end': 5}), 'utf-8')
        cli = subprocess.run([sys.executable, '-X', 'utf8', '-c',
            'import sys,runpy;sys.path.insert(0,sys.argv.pop(1));runpy.run_module("runtime.cli",run_name="__main__")',
            str(ROOT / 'src'), 'knowledge', 'read', '--input', str(source_input)],
            cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=40)
        save('cli', {'exit_code': cli.returncode, 'stdout': cli.stdout, 'stderr': cli.stderr})
        assert cli.returncode == 0 and json.loads(cli.stdout)['text'] == line['text']

        async def protocol():
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            params = StdioServerParameters(command=sys.executable,
                args=['-X', 'utf8', str(ROOT / 'src/server.py')], cwd=str(ROOT), env=dict(os.environ, KR_MCP_TRANSPORT='stdio'))
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    tool = next(t for t in tools.tools if t.name == 'get_content_detail')
                    assert 'github' in tool.inputSchema['properties']
                    result = await session.call_tool('read_knowledge', {'request': {
                        'action': 'fetch', 'version_id': ref['version_id'], 'line_start': 2, 'line_end': 5}})
                    assert not result.isError
                    data = result.structuredContent.get('result', result.structuredContent)
                    assert data['text'] == line['text']
                    save('mcp-protocol', {'result': data, 'receipt': result.meta,
                                          'github_schema_present': True, 'tool_count': len(tools.tools)})
        asyncio.run(protocol())
        assert owner.status()['status'] == 'ready'
        receipt['status'] = 'PASS'
        code = 0
    except Exception as exc:
        receipt.update(status='FAIL', error={'type': type(exc).__name__, 'message': str(exc)[:6000]})
    finally:
        receipt['cleanup'] = owner.stop()
        save('receipt', receipt)
        print(json.dumps({'status': receipt['status'], 'cases': len(receipt['cases']),
                          'error': receipt.get('error'), 'receipt': str(args.output / 'receipt.json')}, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
