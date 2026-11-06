"""只读查询本进程唯一ASR实例；不加载模型，不探测无关GPU。"""

import sys


def asr_lifecycle_summary():
    from runtime.model_roles import ASR_COMPONENT, ASR_ENGINE
    worker = sys.modules.get('runtime.optional_media_worker')
    return {
        'schema': 'knowledgeradar-asr-lifecycle/v2',
        'status': 'implemented_inprocess', 'component': ASR_COMPONENT, 'engine': ASR_ENGINE,
        'mode': 'single_cached_model', 'resident_model': bool(getattr(worker, '_ASR_MODEL', None)),
        'concurrency_resource': 'local_heavy',
        'release_policy': '最多复用一个CPU INT8实例；native返回前保留租约，取消后拒绝迟到写入',
        'default_device': 'cpu', 'compute_type': 'int8', 'batch_size': 1,
        'gpu': {'available': False, 'reason': 'CPU_INT8_engine'},
    }
