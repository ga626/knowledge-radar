"""纯文档CI的轻量检查；只用标准库，不运行平台或导入执行内核。"""
from pathlib import Path
import importlib.util
import json
import re
import sys
ROOT = Path(__file__).resolve().parents[1]


def check(root=ROOT):
    from scripts.generate_tool_manual import render_manual, render_source_guide
    root = Path(root)
    data = json.loads((root / 'config/codex-product/tool-contracts.json').read_text(encoding='utf-8'))
    spec = importlib.util.spec_from_file_location('kr_docs_ecologies', root / 'src/capability_manifest/ecologies.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    expected = {'config/codex-product/TOOL_REFERENCE.md': render_manual(data),
                'docs/CAPABILITIES.md': render_source_guide(mod.source_ecology_manifest(), data)}
    issues = [name + ': generated documentation drift' for name, text in expected.items()
              if not (root / name).is_file() or (root / name).read_text(encoding='utf-8') != text]
    cards = {c['name']: c for c in data['tools']}
    files = [*root.glob('*.md'), *(root / 'docs').rglob('*.md')]
    for path in files:
        text = path.read_text(encoding='utf-8-sig')
        if '\x00' in text:
            issues.append(str(path.relative_to(root)) + ': invalid NUL')
        # Historical reports stay snapshots; formal current-document examples are executable contracts.
        if 'reference' in path.relative_to(root).parts:
            continue
        for block in re.findall(r'```kr-tool-example\s*\n(.*?)```', text, re.S):
            try:
                sample = json.loads(block)
                schema = cards[sample['tool']]['input_schema']
                arguments = sample['arguments']
                if not isinstance(arguments, dict):
                    raise ValueError('arguments must be object')
                unknown = set(arguments) - set(schema['properties'])
                missing = set(schema.get('required', [])) - set(arguments)
                if unknown or missing:
                    raise ValueError('unknown or missing input fields')
            except (ValueError, KeyError, TypeError) as exc:
                issues.append(f'{path.relative_to(root)}: invalid tool example: {exc}')
    return {'status': 'FAIL' if issues else 'PASS', 'issues': issues,
            'scope': 'generated text and current formal tool references; full value validation runs in schema contracts'}


if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))
    value = check()
    print(json.dumps(value, ensure_ascii=False))
    raise SystemExit(value['status'] != 'PASS')
