"""安装器与转发端共用的纯合同摘要；不启动运行时或读取用户数据。"""
import hashlib
import json
from pathlib import Path

PROTOCOL = 1

def _versions(data):
    versions = data.get('runtime_lifecycle_contract', {})
    keys = ('client_compatibility_version', 'data_compatibility_version')
    if any(type(versions.get(key)) is not int or versions[key] < 1 for key in keys):
        raise ValueError('runtime_compatibility_versions_missing')
    return {key: versions[key] for key in keys}


def compatibility_versions(program):
    return _versions(json.loads((Path(program)/'config/codex-product/tool-contracts.json').read_text(encoding='utf-8')))


def forwarding_contract(program=None):
    program = Path(program or Path(__file__).resolve().parents[2])
    data = json.loads((program/'config/codex-product/tool-contracts.json').read_text(encoding='utf-8'))
    if not data.get('tools'):
        raise ValueError('forwarding_contract_missing')
    version = _versions(data)['client_compatibility_version']
    cards = [{k: tool[k] for k in ('name', 'input_schema', 'output_schema', 'error_schema')} for tool in data['tools']]
    return hashlib.sha256(json.dumps({'version': version, 'ipc': PROTOCOL, 'receipt': 'kr-operation-reply/v1',
        'tools': sorted(cards, key=lambda t: t['name'])}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
