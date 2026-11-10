"""KR薄CLI：本机批量读取与组合，共用内核合同，不直接写证据库。"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from pydantic import TypeAdapter, ValidationError

from runtime.execution_owner import OwnerClient
from runtime.knowledge import ReadRequest


class CliArgumentsError(ValueError):
    def __init__(self, usage):
        super().__init__('invalid_cli_arguments')
        self.usage = usage


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise CliArgumentsError(self.format_usage())


def _json_file(path, *, max_bytes=1024 * 1024):
    with Path(path).open('rb') as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError('bounded_input_file_required')
    return json.loads(raw.decode('utf-8-sig'))


def _failed(value):
    return isinstance(value, dict) and value.get('status') == 'error' and 'error' in value


def _write_output(path, value):
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError('bounded_output_required')
    target = Path(path).resolve()
    # 先完成同目录暂存与fsync，再以不覆盖语义发布；中断不留下假成品。
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.kr-output-', suffix='.pending', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == 'nt':
            os.rename(temporary, target)
        else:
            os.link(temporary, target)
            temporary.unlink()
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {'status': 'written', 'path': str(Path(path).resolve()), 'bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(),
            **({'batch_status': value['status'], 'failed_indices': value['failed_indices']}
               if isinstance(value, dict) and value.get('schema') == 'kr-read-batch/v1' else {})}


def read_requests(requests, client):
    if not isinstance(requests, list) or not 1 <= len(requests) <= 100:
        raise ValueError('batch_requires_1_to_100_read_requests')
    # 整批先校验，错参不产生半批工作；不用另一份参数定义。
    adapter = TypeAdapter(ReadRequest)
    validated = [adapter.validate_python(item).model_dump(mode='json') for item in requests]
    return [client.call('read_knowledge', {'request': item}) for item in validated]


def parser():
    root = ArgumentParser(prog='knowledgeradar', description=__doc__)
    sub = root.add_subparsers(dest='command')
    sub.add_parser('mcp', help='启动薄MCP连接器，执行由唯一内核托管')
    capabilities = sub.add_parser('capabilities', help='读取同源生成合同，不启动或探测来源')
    capabilities.add_argument('--tool', default='')
    runtime = sub.add_parser('runtime', help='显式启动或只读诊断唯一执行内核')
    runtime_sub = runtime.add_subparsers(dest='action', required=True)
    for action in ('start', 'rearm'):
        recovery = runtime_sub.add_parser(action, help='恢复一次' if action == 'start' else '显式重新开放自动恢复预算')
        recovery.add_argument('--request-id', required=True)
        recovery.add_argument('--expected-source', required=True)
        recovery.add_argument('--expected-incident')
    runtime_status = runtime_sub.add_parser('status')
    runtime_status.add_argument('--wait', type=float, default=0, help='最多30秒只读等待')
    recovery_receipt = runtime_sub.add_parser('recovery-receipt')
    recovery_receipt.add_argument('request_id')
    receipts = runtime_sub.add_parser('receipts', help='有界回执目录，找回断连时未收到的request_id')
    receipts.add_argument('--limit', type=int, default=20)
    runtime_sub.add_parser('stop', help='仅停止空闲内核，忙时拒绝；不取消任务')
    receipt = runtime_sub.add_parser('receipt')
    receipt.add_argument('request_id')
    invoke = sub.add_parser('invoke', help='以同一应用合同程序组合；不另定义业务参数')
    invoke.add_argument('--tool', required=True)
    invoke.add_argument('--input', required=True)
    invoke.add_argument('--request-id', required=True, help='同一意图重试沿用此ID；不同意图用新ID')
    knowledge = sub.add_parser('knowledge', help='固定证据读取、批量筛选与版本比较')
    ks = knowledge.add_subparsers(dest='action', required=True)
    read = ks.add_parser('read', help='入参由capabilities --tool read_knowledge给出')
    read.add_argument('--input', required=True, help='UTF-8 JSON ReadRequest文件')
    read.add_argument('--output', help='可选的新输出文件；已存在则拒绝')
    batch = ks.add_parser('batch', help='最多100个ReadRequest，程序化执行，结果留在本机')
    batch.add_argument('--input', required=True, help='UTF-8 JSON请求数组')
    batch.add_argument('--output', required=True, help='新输出文件；已存在则拒绝')
    batch.add_argument('--retry-from', help='只重读上次失败项；成功项明确保留为先前观察，不冒充本次核验')
    compare = ks.add_parser('compare', help='比较两个固定版本，保留原文引用')
    compare.add_argument('left')
    compare.add_argument('right')
    compare.add_argument('--max-chars', type=int, default=8000)
    compare.add_argument('--output', required=True)
    return root


def execute(args, client):
    if getattr(args, 'output', None) and Path(args.output).exists():
        raise FileExistsError('output_exists_preserve_user_file')
    if args.command == 'capabilities':
        path = Path(__file__).resolve().parents[2] / 'config/codex-product/tool-contracts.json'
        data = _json_file(path)
        cards = data['tools']
        if args.tool:
            card = next((item for item in cards if item['name'] == args.tool), None)
            if card is None:
                raise ValueError('unknown_tool_use_catalog')
            return {'schema': 'kr-tool-contract/v1', 'contract_hash': data['contract_hash'], 'tool': card}
        return {**data, 'tools': [{k: v for k, v in item.items() if k not in {'input_schema', 'output_schema'}} for item in cards]}
    if args.command == 'runtime':
        if args.action == 'status':
            from runtime.runtime_control import runtime_snapshot
            return runtime_snapshot(client, wait_seconds=args.wait)
        if args.action == 'recovery-receipt':
            from runtime.runtime_control import recovery_receipt
            return recovery_receipt(client, args.request_id)
        if args.action == 'receipt':
            return client.receipt(args.request_id)
        if args.action == 'receipts':
            return client.receipts(args.limit)
        if args.action == 'stop':
            return client.stop()
        from runtime.runtime_control import request_recovery
        return request_recovery(client, request_id=args.request_id, expected_source=args.expected_source,
            expected_incident=args.expected_incident,
            action='recover_once' if args.action == 'start' else 'rearm_auto_recovery')
    if args.command == 'invoke':
        value = client.call(args.tool, _json_file(args.input), request_id=args.request_id)
        return {'result': value, 'execution_receipt': client.last_receipt,
                'status': 'error' if _failed(value) else 'ok',
                **({'error': value['error']} if _failed(value) else {})}
    if args.action in {'read', 'batch'}:
        requests = _json_file(args.input)
        if args.output and Path(args.output).exists():
            raise FileExistsError('output_exists_preserve_user_file')
        if args.action == 'batch':
            if not isinstance(requests, list) or not 1 <= len(requests) <= 100:
                raise ValueError('batch_requires_1_to_100_read_requests')
            adapter = TypeAdapter(ReadRequest)
            validated = [adapter.validate_python(item).model_dump(mode='json') for item in requests]
            identity = hashlib.sha256(json.dumps(validated, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            prior = _json_file(args.retry_from, max_bytes=16 * 1024 * 1024) if args.retry_from else None
            if prior and (prior.get('schema') != 'kr-read-batch/v1' or prior.get('batch_identity') != identity
                          or len(prior.get('results', [])) != len(validated)
                          or len(prior.get('outcomes', [])) != len(validated)
                          or not isinstance(prior.get('failed_indices'), list)
                          or any(type(i) is not int or not 0 <= i < len(validated) for i in prior['failed_indices'])
                          or set(prior['failed_indices']) != {i for i, v in enumerate(prior['results']) if _failed(v)}):
                raise ValueError('batch_resume_identity_mismatch')
            values, outcomes, interrupted = [], [], False
            for index, request in enumerate(validated):
                if prior and index not in prior['failed_indices']:
                    values.append(prior['results'][index])
                    outcomes.append({**prior['outcomes'][index], 'observation': 'previous_run_not_revalidated'})
                    continue
                receipt = None
                try:
                    if interrupted:
                        raise ValueError('batch_interrupted_not_executed')
                    value = client.call('read_knowledge', {'request': request})
                    receipt = client.last_receipt
                except KeyboardInterrupt:
                    from runtime.agent_contracts import error_result
                    interrupted = True
                    value = error_result(ValueError('batch_interrupted'), 'read_knowledge', layer='cli')
                except (OSError, ValueError, RuntimeError) as exc:
                    from runtime.agent_contracts import error_result
                    value = error_result(exc, 'read_knowledge', not_started=True, layer='cli')
                values.append(value)
                outcomes.append({'index': index, 'status': 'error' if _failed(value) else 'ok',
                                 'execution_receipt': receipt, 'observed_at': time.time()})
            failed = [index for index, value in enumerate(values) if _failed(value)]
            value = {'schema': 'kr-read-batch/v1', 'status': 'partial' if failed else 'ok',
                     'batch_identity': identity, 'results': values, 'outcomes': outcomes,
                     'count': len(values), 'failed_indices': failed}
            return _write_output(args.output, value)
        values = read_requests([requests], client)
        if any(_failed(value) for value in values):
            return next(value for value in values if _failed(value))
        value = values[0] if args.action == 'read' else {'results': values, 'count': len(values)}
    else:
        values = read_requests([{'action': 'fetch', 'version_id': version, 'max_chars': args.max_chars}
                                for version in (args.left, args.right)], client)
        if any(_failed(value) for value in values):
            return next(value for value in values if _failed(value))
        value = {'kind': 'version_comparison', 'left': values[0]['citation'], 'right': values[1]['citation'],
                 'coverage': 'partial' if any(item['truncated'] or item['next_start'] is not None for item in values) else 'full',
                 'diff': list(difflib.unified_diff(values[0]['text'].splitlines(), values[1]['text'].splitlines(),
                                                  fromfile=args.left, tofile=args.right, lineterm=''))}
    return _write_output(args.output, value) if args.output else value


def main(argv=None):
    command = parser()
    try:
        args = command.parse_args(argv)
    except CliArgumentsError as exc:
        from runtime.agent_contracts import error_result
        value = error_result(exc, '', not_started=True, layer='cli')
        value['error']['usage'] = exc.usage
        print(json.dumps(value, ensure_ascii=False))
        return 2
    if args.command in {None, 'mcp'}:
        import server
        server.main()
        return 0
    try:
        value = execute(args, OwnerClient())
    except (OSError, ValueError, ValidationError, RuntimeError) as exc:
        from runtime.agent_contracts import error_result
        value = error_result(exc, 'read_knowledge', not_started=True, layer='cli')
    print(json.dumps(value, ensure_ascii=False, separators=(',', ':')))
    if value.get('batch_status') == 'partial':
        return 3
    return 2 if _failed(value) or value.get('status') in {'unavailable', 'unreachable', 'incompatible', 'busy', 'unknown', 'rejected', 'failed'} else 0


if __name__ == '__main__':
    raise SystemExit(main())
