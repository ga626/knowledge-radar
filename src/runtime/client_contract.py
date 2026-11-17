"""安装器与转发端共用的纯合同摘要；不启动运行时或读取用户数据。"""
import hashlib
import json
from pathlib import Path

PROTOCOL = 1
CLIENT_COMPATIBILITY = 1  # 不兼容的错误/回执语义变化必须递增。


def forwarding_contract(program=None):
    program = Path(program or Path(__file__).resolve().parents[2])
    data = json.loads((program/'config/codex-product/tool-contracts.json').read_text(encoding='utf-8'))
    manifest = json.loads((program/'config/project-structure.manifest.json').read_text(encoding='utf-8'))
    if not data.get('tools'):
        raise ValueError('forwarding_contract_missing')
    version = manifest.get('runtime_lifecycle_contract', {}).get('client_compatibility_version', CLIENT_COMPATIBILITY)
    cards = [{k: tool[k] for k in ('name', 'input_schema', 'output_schema', 'error_schema')} for tool in data['tools']]
    return hashlib.sha256(json.dumps({'version': version, 'ipc': PROTOCOL, 'receipt': 'kr-operation-reply/v1',
        'tools': sorted(cards, key=lambda t: t['name'])}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
