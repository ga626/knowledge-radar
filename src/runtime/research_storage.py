"""研究持久状态的短事务屏障和磁盘准入；不包围网络或模型执行。"""
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
import shutil
import os
import sqlite3
import uuid
from contextlib import closing
import threading
from runtime.file_write_lock import file_write_lock
from runtime.paths import runtime_state_dir

_HELD = threading.local()

@contextmanager
def storage_guard(root=None):
    path = Path(root or runtime_state_dir()).resolve() / '.research-storage'
    key = str(path).casefold()
    held = getattr(_HELD, 'keys', set())
    if key in held:
        yield
        return
    with file_write_lock(path):
        _HELD.keys = held | {key}
        try:
            yield
        finally:
            _HELD.keys = held

def guarded_transaction(function):
    @wraps(function)
    @contextmanager
    def wrapped(*args, **kwargs):
        # 只读证据连接不创建屏障文件。
        owner = args[0] if args else None
        if getattr(owner, 'read_only', False):
            with function(*args, **kwargs) as value:
                yield value
        else:
            with storage_guard():
                with function(*args, **kwargs) as value:
                    yield value
    return wrapped

def admit_bytes(path, size, *, reserve=16*1024*1024):
    """暂存与最终发布同卷；预留系统余量，ENOSPC仍按可恢复失败处理。"""
    root = Path(path)
    while not root.exists():
        root = root.parent
    if shutil.disk_usage(root).free < int(size) + reserve:
        raise OSError(28, 'storage_space_insufficient')

@contextmanager
def reserve_bytes(path, size, *, category='archive'):
    """跨进程磁盘预留；死亡进程按PID代际回收，隔离区仍计媒体容量。"""
    from runtime.process_identity import process_identity
    root=Path(path)
    while not root.exists():root=root.parent
    device=str(root.stat().st_dev)
    database=runtime_state_dir()/'storage-reservations.sqlite3'
    database.parent.mkdir(parents=True,exist_ok=True)
    token=uuid.uuid4().hex
    with file_write_lock(database),closing(sqlite3.connect(database,timeout=10)) as c,c:
        c.execute('CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY,device TEXT,size INTEGER,pid INTEGER,generation TEXT,category TEXT)')
        for key,pid,generation in c.execute('SELECT id,pid,generation FROM reservations').fetchall():
            if not process_identity(pid) or process_identity(pid)!=generation:c.execute('DELETE FROM reservations WHERE id=?',(key,))
        pending=c.execute('SELECT COALESCE(SUM(size),0) FROM reservations WHERE device=?',(device,)).fetchone()[0]
        admit_bytes(root,int(size)+pending)
        quota=os.environ.get('KR_MEDIA_CAPACITY_BYTES','') if category=='media' else ''
        if quota:
            from runtime.paths import runtime_media_cache_dir
            bases=[runtime_media_cache_dir(),Path(os.environ.get('KR_DATA_ROOT') or runtime_state_dir().parent)/'quarantine']
            used=0
            for base in bases:
                if not base.exists():continue
                for parent,dirs,files in os.walk(base,followlinks=False):
                    dirs[:]=[name for name in dirs if not (Path(parent)/name).is_symlink() and not (hasattr(Path(parent)/name,'is_junction') and (Path(parent)/name).is_junction())]
                    used+=sum((Path(parent)/name).stat().st_size for name in files if not (Path(parent)/name).is_symlink())
            media_pending=c.execute("SELECT COALESCE(SUM(size),0) FROM reservations WHERE category='media'").fetchone()[0]
            if used+media_pending+int(size)>int(quota):raise ValueError('media_capacity_exceeded_cache_and_quarantine_counted')
        c.execute('INSERT INTO reservations VALUES(?,?,?,?,?,?)',(token,device,int(size),os.getpid(),process_identity(),category))
    try:
        yield
    finally:
        with file_write_lock(database),closing(sqlite3.connect(database,timeout=10)) as c,c:
            c.execute('DELETE FROM reservations WHERE id=?',(token,))
