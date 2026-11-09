"""同源生成工具合同、展开手册与来源指南；生成和check均不启动执行内核。"""
from pathlib import Path
import sys
import json
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def render_manual(data):
    lines = ['# KnowledgeRadar 工具参考（自动生成）', '',
             '来源：入口中立的类型化应用合同。禁止手改；MCP可能包result，表中是解包后的业务合同。',
             '错误信封独立列出；CLI按需暴露同源能力。', '', f"合同指纹：`{data['contract_hash']}`", '']
    for tool in data['tools']:
        lines.extend(['## ' + tool['name'], '', tool.get('usage', tool['purpose']), '', '```json',
                      json.dumps({'input': tool['input_schema'], 'output': tool['output_schema'], 'error': tool['error_schema']},
                                 ensure_ascii=False, sort_keys=True, indent=2), '```', ''])
    return '\n'.join(lines)


def render_source_guide(declaration, data):
    lines = ['# 来源能力指南（自动生成）', '',
             '由 scripts/generate_tool_manual.py 从同一生态声明和工具合同生成；禁止手改。',
             '目录仅帮助Agent选择来源；注册、启用、账号就绪和真实验收分开，来源数量不决定证据饱和。',
             f"工具合同指纹：`{data['contract_hash']}`", '']
    for key, item in declaration['ecologies'].items():
        lines.extend(['## ' + item['label'], '', f"生态：`{key}`", '',
                      item['declared_boundary'], '',
                      '候选工具：' + '、'.join('`' + name + '`' for name in item['candidate_tools']), '',
                      '内容：' + '、'.join(item['content_forms']), '',
                      '边界：' + '；'.join(item['known_limits']), ''])
        for stage in item.get('workflows', []):
            lines.extend(['步骤：' + stage['stage'], '', '```kr-tool-example',
                          json.dumps({'tool': stage['tool'], 'arguments': stage['arguments']}, ensure_ascii=False, indent=2), '```',
                          '绑定：' + json.dumps(stage.get('bindings', {}), ensure_ascii=False), ''])
    return '\n'.join(lines)


def generate(check=False):
    from runtime import composition  # noqa: F401
    from runtime.application import catalog
    from capability_manifest.ecologies import source_ecology_manifest
    data = catalog(full=True)
    # Runtime artifact identity is observed from installed bytes, not a portable schema.
    data.pop("research_workflow", None)
    base = ROOT / 'config/codex-product'
    artifacts = {base / 'tool-contracts.json': json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + '\n',
                 base / 'TOOL_REFERENCE.md': render_manual(data),
                 ROOT / 'docs/CAPABILITIES.md': render_source_guide(source_ecology_manifest(), data)}
    for path, text in artifacts.items():
        if check:
            if not path.is_file() or path.read_text(encoding='utf-8') != text:
                raise RuntimeError('generated_manual_drift:' + path.name)
        else:
            path.write_text(text, encoding='utf-8')
    return {'status': 'PASS', 'tool_count': data['tool_count'], 'contract_hash': data['contract_hash']}


if __name__ == '__main__':
    print(json.dumps(generate('--check' in sys.argv), ensure_ascii=False))
