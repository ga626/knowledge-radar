"""复用已审阅组件的包目录，在唯一解释器执行；不启动组件Python。"""
from importlib import invalidate_caches
from importlib.metadata import distributions
from pathlib import Path
import re
import sys
import threading

_LOCK = threading.RLock()


def _key(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def component_environment(component, receipt, *, activate=False):
    """核心已有包优先；精确主依赖冲突时报错，绝不热替换已加载的库。"""
    from runtime.model_roles import COMPONENTS
    python = Path(receipt['python']).resolve()
    root = python.parent.parent if python.parent.name.lower() in {'scripts', 'bin'} else python.parent
    site = root / ('Lib/site-packages' if sys.platform == 'win32' else f'lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages')
    with _LOCK:
        current = {}
        for dist in distributions():
            if dist.metadata.get('Name'):
                current.setdefault(_key(dist.metadata['Name']), dist)
        available = {_key(dist.metadata['Name']): dist for dist in distributions(path=[str(site)]) if dist.metadata.get('Name')}
        # 只追加经receipt/license/lock验证的目录，禁止改变核心包的解析优先级。
        selected = {**available, **current}
        for requirement in COMPONENTS[component]['requirements']:
            name, expected = requirement.split('==', 1)
            name = name.split('[', 1)[0]
            if _key(name) not in selected or selected[_key(name)].version != expected:
                raise ValueError('inprocess_component_dependency_conflict:' + name)
        # 按实际要启用的extras校验传递依赖；不靠“主包版本正确”冒充共存成功。
        from packaging.requirements import Requirement
        pending = [(name.split('==', 1)[0], set(COMPONENTS[component].get('extras', [])))
                   for name in COMPONENTS[component]['requirements']]
        visited = set()
        while pending:
            name, extras = pending.pop()
            key = (_key(name), tuple(sorted(extras)))
            if key in visited:
                continue
            visited.add(key)
            dist = selected.get(_key(name))
            if dist is None:
                raise ValueError('inprocess_component_dependency_missing:' + name)
            for raw in dist.requires or []:
                requirement = Requirement(raw)
                if requirement.marker and not any(requirement.marker.evaluate({'extra': extra}) for extra in extras | {''}):
                    continue
                dependency = selected.get(_key(requirement.name))
                if dependency is None or not requirement.specifier.contains(dependency.version, prereleases=True):
                    raise ValueError('inprocess_component_dependency_conflict:' + requirement.name)
                pending.append((requirement.name, set(requirement.extras)))
        if activate and str(site) not in sys.path:
            sys.path.append(str(site))
            invalidate_caches()
        return {'status': 'ready', 'execution': 'inprocess', 'component': component}
