"""本地授权、固定版本清单与create-only交付；不调用Obsidian或其他知识产品。"""
from __future__ import annotations
from runtime.research_storage import guarded_transaction
from contextlib import contextmanager, ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import tempfile
import time
from urllib.parse import quote
from typing import Annotated, Literal
from pydantic import Field
from kr_core.evidence_assets import EvidenceAssets
from runtime.file_write_lock import file_write_lock
from runtime.paths import runtime_state_dir
from runtime.execution_context import check_execution, ExecutionStopped
from runtime.knowledge import Contract, encoded, guarded_path, register_report
from runtime.research_archive import BindContext, SubmitReport, ArchiveTask, ArchiveStatus, RetryArchive, ResearchArchive, RebindReportOutput, PlanArchiveLayout, ApplyArchiveLayout, stable_target, DISPLAY_LAYOUT, task_display_title, calendar_layout
from runtime.research_backup import SnapshotResearch, RestoreResearch


class RegisterReport(Contract):
    action: Literal["register_report"]
    input_path: str
    approved_input_root: str
    input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    project_id: str = Field(min_length=1,max_length=200)
    task_id: str = Field(min_length=1,max_length=96)
    report_id: str = Field(min_length=1,max_length=200)
    title: str = Field(min_length=1,max_length=200)
    derived_from: list[str] = Field(default_factory=list,max_length=100)


class Authorize(Contract):
    action: Literal["authorize_destination"]
    approved_root: str
    project_id: str = Field(min_length=1,max_length=200)


class Revoke(Contract):
    action: Literal["revoke_destination"]
    grant_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class Deliver(Contract):
    action: Literal["deliver"]
    grant_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    project_id: str = Field(min_length=1,max_length=200)
    task_id: str = Field(min_length=1,max_length=96)
    version_ids: list[str] = Field(min_length=1,max_length=100)
    project_name: str = Field(default='',max_length=80,description='可读项目名；省略时复用任务元数据，不用项目ID命名目录。')
    task_title: str = Field(default='',max_length=80,description='可读任务名；省略时复用任务元数据。')


class DeliveryStatus(Contract):
    action: Literal["delivery_status"]
    delivery_id: str = Field(pattern=r"^[a-f0-9]{64}$")


class Reindex(Contract):
    action: Literal["upgrade_index"]


ArtifactRequest = Annotated[RegisterReport | Authorize | Revoke | Deliver | DeliveryStatus | Reindex | BindContext | SubmitReport | ArchiveTask | ArchiveStatus | RetryArchive | RebindReportOutput | PlanArchiveLayout | ApplyArchiveLayout | SnapshotResearch | RestoreResearch, Field(discriminator="action")]


def identity(path):
    s=Path(path).stat()
    return [s.st_dev,s.st_ino]


def slug(value, prefix):
    import re
    name=re.sub(r'[^\w\-]', '_', value, flags=re.UNICODE).strip('_')[:32]
    return prefix+name+'--'+hashlib.sha256(value.encode()).hexdigest()[:12]


@contextmanager
def pin_directory(path):
    """Windows禁止目录被改名/删除；避免校验后根目录被junction替换。"""
    if os.name!='nt':
        yield
        return
    import ctypes
    from ctypes import wintypes
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    api.CreateFileW.restype=wintypes.HANDLE
    api.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=api.CreateFileW(str(path),0,3,None,3,0x02000000|0x00200000,None)
    if handle==wintypes.HANDLE(-1).value: raise ctypes.WinError(ctypes.get_last_error())
    try: yield
    finally: api.CloseHandle(handle)


def read_observed(path):
    if os.name!='nt':return path.read_bytes()
    import ctypes,msvcrt
    from ctypes import wintypes
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    api.CreateFileW.restype=wintypes.HANDLE
    handle=api.CreateFileW(str(path),0x80000000,7,None,3,0x00200000,None)
    if handle==wintypes.HANDLE(-1).value:raise ctypes.WinError(ctypes.get_last_error())
    try:descriptor=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
    except BaseException:
        api.CloseHandle.argtypes=[wintypes.HANDLE]
        api.CloseHandle(handle)
        raise
    with os.fdopen(descriptor,'rb') as stream:return stream.read()


def publish_directory(staging, target):
    if os.name=='nt':
        os.rename(staging,target)  # Windows: existing destination fails, never replace.
    elif os.name=='posix':
        import ctypes
        libc=ctypes.CDLL(None,use_errno=True)
        rename=getattr(libc,'renameat2',None)
        if rename is None: raise OSError('atomic_noreplace_not_supported')
        if rename(-100,os.fsencode(staging),-100,os.fsencode(target),1)!=0:
            code=ctypes.get_errno()
            raise OSError(code,os.strerror(code))
    else: raise OSError('atomic_noreplace_not_supported')


class DeliveryStore:
    def __init__(self, root=None, assets=None):
        self.root=Path(root or runtime_state_dir())/'knowledge_delivery'
        self.db=self.root/'delivery.sqlite3'
        self.assets=assets or EvidenceAssets()

    @guarded_transaction
    @contextmanager
    def connection(self):
        self.root.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(self.db,timeout=10)) as c, c:
            c.executescript("CREATE TABLE IF NOT EXISTS grants(id TEXT PRIMARY KEY,root TEXT,identity TEXT,project TEXT,active INTEGER); CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,payload TEXT);")
            yield c

    def status(self, delivery_id):
        if not self.db.is_file():return {"status":"not_found","delivery_id":delivery_id}
        with closing(sqlite3.connect(self.db.resolve().as_uri()+'?mode=ro',uri=True)) as c:
            row=c.execute('SELECT payload FROM deliveries WHERE id=?',(delivery_id,)).fetchone()
        return json.loads(row[0]) if row else {"status":"not_found","delivery_id":delivery_id}

    def for_task(self,task_id):
        if not self.db.is_file():return []
        with closing(sqlite3.connect(self.db.resolve().as_uri()+'?mode=ro',uri=True)) as c:
            rows=c.execute("SELECT payload FROM deliveries WHERE json_extract(payload,'$.task_id')=? ORDER BY rowid DESC LIMIT 20",(task_id,)).fetchall()
        fields={'delivery_id','status','path','verified_at','obsidian_observed','remote_synced','error_code'}
        return [{k:v for k,v in json.loads(row[0]).items() if k in fields} for row in rows]

    def save(self, receipt):
        with self.connection() as c:
            c.execute('INSERT INTO deliveries VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',(receipt['delivery_id'],encoded(receipt)))

    def authorize(self, request):
        root=Path(request.approved_root)
        guarded_path(root,root/'__kr_scope_probe__')
        grant=secrets.token_hex(16)
        with file_write_lock(self.db),self.connection() as c:
            c.execute('INSERT INTO grants VALUES(?,?,?,?,1)',(grant,str(root.resolve()),encoded(identity(root)),request.project_id))
        return {"status":"authorized","grant_id":grant,"project_id":request.project_id,"root":str(root.resolve()),"scope":"create_only_files_and_directories"}

    def revoke(self, grant_id):
        with file_write_lock(self.db),self.connection() as c:
            changed=c.execute('UPDATE grants SET active=0 WHERE id=?',(grant_id,)).rowcount
        return {"status":"revoked" if changed else "not_found","grant_id":grant_id}

    def _grant(self, request):
        with self.connection() as c:
            row=c.execute('SELECT root,identity,project,active FROM grants WHERE id=?',(request.grant_id,)).fetchone()
        if not row or not row[3] or row[2]!=request.project_id:raise ValueError('destination_not_authorized')
        root=Path(row[0])
        guarded_path(root,root/'__kr_scope_probe__')
        if encoded(identity(root))!=row[1]:raise ValueError('destination_identity_changed')
        return root

    def _verify(self, target, files, root):
        hashes={}
        for name,raw in files.items():
            check_execution()
            path=guarded_path(root,target/name,require_file=True)
            observed=read_observed(path)
            if observed!=raw:raise ValueError('destination_modified_preserved')
            hashes[name]=hashlib.sha256(observed).hexdigest()
        return hashes

    def deliver(self, request):
        for attempt in range(3):
            check_execution()
            receipt=self._deliver_once(request)
            if receipt.get('winerror') not in {32,33} or attempt==2:
                return receipt
            # Only an observed sharing/lock violation permits bounded local retry.
            time.sleep((0.05,0.15)[attempt])
        return receipt

    def _deliver_once(self, request):
        # Lock is local/disk-only; no browser/model/network lease is held here.
        with file_write_lock(self.db):
            root=self._grant(request)
            versions=sorted(set(request.version_ids))
            key=hashlib.sha256(encoded([stable_target(root,request.project_id),request.project_id,request.task_id,versions]).encode()).hexdigest()
            previous=self.status(key)
            if not previous.get('path') or previous.get('display_layout')==DISPLAY_LAYOUT:
                return self._deliver_readable(request,root,key,previous)
            tracker=ResearchArchive(self)
            legacy=bool(previous.get('path') and previous.get('display_layout')!='human-v1')
            if previous.get('path'):
                target=Path(previous['path'])
            else:
                metadata=tracker._ledger(request.task_id)
                parent=tracker._display_path(root,root,['project',request.project_id],request.project_name or metadata.get('project_name') or '未命名项目',directory=True)
                parent=tracker._display_path(root,parent,['task',request.project_id,request.task_id],request.task_title or metadata.get('display_title') or '未命名调研',directory=True)
                target=tracker._display_path(root,parent,['selection',key],'选取资料')
            guarded_path(root,target)
            if previous.get("identity"):
                try:target=tracker._find_committed(root,previous)
                except ValueError as exc:
                    previous.update(status="conflict",error_code=str(exc));self.save(previous);return previous
            receipt={"delivery_id":key,"status":"preparing","path":str(target),"version_ids":versions,"project_id":request.project_id,"task_id":request.task_id,"obsidian_observed":"unknown","remote_synced":"unknown","attempted_at":time.time(),"display_layout":"legacy" if legacy else "human-v1"}
            files={}; entries=[]
            if not legacy:tracker._display_path(root,target,['selection-directory',key],'资料目录',suffix='.md')
            for version in versions:
                check_execution()
                with self.assets.connection() as c:
                    row=c.execute('SELECT * FROM assets WHERE version_id=?',(version,)).fetchone()
                    if row is None:raise ValueError('version_not_found_or_withdrawn')
                    row=dict(row)
                    self.assets.assert_available(c,row)
                    allowed=c.execute('SELECT 1 FROM asset_scopes WHERE version_id=? AND project_id=? AND task_id=?',(version,request.project_id,request.task_id)).fetchone()
                    if not allowed:raise ValueError('selected_version_outside_project_task')
                    if sum(map(len,files.values()))+row['size_bytes']>64*1024*1024:raise ValueError('delivery_too_large')
                    # One verified read per original, not a full-file hash for every 24K page.
                    raw=self.assets._verified_text(row).encode('utf-8')
                    asset=self.assets._ref(row)
                check_execution()
                name=(('report-' if asset['kind']=='report' else 'evidence-')+version[:24]+'.md') if legacy else tracker._display_path(root,target,['selected-version',key,version],asset['title'] or ('总报告' if asset['kind']=='report' else '未命名资料'),suffix='.md').name
                files[name]=raw
                entries.append({"file":name,"sha256":hashlib.sha256(raw).hexdigest(),"asset":asset,"locator_target":"original_utf8_code_points"})
                if sum(map(len,files.values()))>64*1024*1024:raise ValueError('delivery_too_large')
            if legacy:
                files['index.md']=('# 研究交付\n\n'+ '\n'.join(f"- [{x['asset']['title'].replace('[','').replace(']','').replace(chr(10),' ')}]({x['file']})" for x in entries)).encode('utf-8')
                files['manifest.json']=encoded({"schema":"kr-delivery/v1","delivery_id":key,"project_id":request.project_id,"task_id":request.task_id,"entries":entries}).encode()
                files['delivery-receipt.json']=encoded({"delivery_id":key,"state":"published_payload","local_verification":"query_KR_delivery_status","obsidian_observed":"unknown","remote_synced":"unknown"}).encode()
            else:
                files['资料目录.md']=('# 调研资料\n\n'+ '\n'.join(f"- [{x['asset']['title'].replace('[','').replace(']','').replace(chr(10),' ') or '未命名资料'}]({quote(x['file'])}) · 来源：<{x['asset']['source_url']}>" for x in entries)+'\n').encode('utf-8')
                receipt['entries']=entries
            self.save(receipt)
            staging=None
            try:
                with ExitStack() as pins:
                    pins.enter_context(pin_directory(root))
                    self._grant(request)
                    parent=root
                    for part in target.relative_to(root).parts[:-1]:
                        parent=parent/part
                        guarded_path(root,parent)
                        parent.mkdir(exist_ok=True)
                        guarded_path(root,parent)
                        pins.enter_context(pin_directory(parent))
                        guarded_path(root,parent)
                    if not target.exists():
                        # Prefer private same-volume staging. Otherwise a scoped Vault staging directory;
                        # external synchronization is explicitly not atomic.
                        stage_root=self.root if identity(self.root)[0]==identity(root)[0] else target.parent
                        staging=Path(tempfile.mkdtemp(prefix='.kr-delivery-',dir=stage_root))
                        for name,raw in files.items():
                            with (staging/name).open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
                        check_execution()
                        self._grant(request)
                        # Revalidate withdrawn sources immediately before committing a visible copy.
                        for version in versions:self.assets.fetch(version,max_chars=1)
                        try:publish_directory(staging,target)
                        except FileExistsError:pass
                    hashes=self._verify(target,files,root)
                    receipt.update(status='local_committed',file_hashes=hashes,identity=identity(target),verified_at=time.time(),sync_atomicity='not_claimed')
                    self.save(receipt)
                    return receipt
            except ExecutionStopped as exc:
                receipt.update(status='interrupted',error_code=exc.error_code,next_action='inspect_then_repeat_same_request')
                self.save(receipt)
                return receipt
            except (OSError,ValueError) as exc:
                retryable=isinstance(exc,OSError) and not isinstance(exc,FileExistsError)
                receipt.update(status='retryable_failure' if retryable else 'conflict',error_code=str(exc) if isinstance(exc,ValueError) else type(exc).__name__,retryable=retryable,winerror=getattr(exc,'winerror',None),next_action='repeat_same_request_to_reconcile' if retryable else 'preserve_destination_and_inspect')
                self.save(receipt)
                return receipt
            finally:
                if staging and staging.exists():
                    allowed=self.root.resolve() if staging.is_relative_to(self.root) else target.parent.resolve()
                    if staging.resolve().is_relative_to(allowed) and not staging.is_symlink():shutil.rmtree(staging)

    def _deliver_readable(self,request,root,key,previous):
        """与自动全文归档共用命名、正文与创建式发布，不再复制一套选取原文。"""
        tracker=ResearchArchive(self);tracker._ensure()
        versions=sorted(set(request.version_ids))
        with self.assets.connection() as c:
            size=0
            for version in versions:
                row=c.execute('SELECT * FROM assets WHERE version_id=?',(version,)).fetchone()
                if row is None:raise ValueError('version_not_found_or_withdrawn')
                self.assets.assert_available(c,dict(row));self.assets._verified_text(dict(row));size+=row['size_bytes']
                if not c.execute('SELECT 1 FROM asset_scopes WHERE version_id=? AND project_id=? AND task_id=?',(version,request.project_id,request.task_id)).fetchone():
                    raise ValueError('selected_version_outside_project_task')
            if size>64*1024*1024:raise ValueError('delivery_too_large')
        metadata=tracker._ledger(request.task_id)
        bound=tracker._context(request.task_id)
        # 绑定任务的旧布局保持原状；本次显式选取可在同任务目录中使用新规范。
        if bound and bound['project_id']!=request.project_id:raise ValueError('research_project_mismatch_use_new_task')
        if bound and bound.get('vault_grant_id') and stable_target(tracker._grant(bound['vault_grant_id'],request.project_id),request.project_id)==stable_target(root,request.project_id):
            context=bound.copy()
        else:
            title=request.task_title or metadata.get('display_title') or '未命名调研'
            context={'task_id':request.task_id,'project_id':request.project_id,'project_name':request.project_name or metadata.get('project_name') or '未命名项目',
                'task_title':title,'directory_title':task_display_title(metadata,title),'display_layout':DISPLAY_LAYOUT,
                'vault_grant_id':request.grant_id,'vault_project_dir':''}
            context.update(calendar_layout(metadata,title))
        context['vault_grant_id']=request.grant_id
        receipt={'delivery_id':key,'status':'preparing','version_ids':versions,'project_id':request.project_id,'task_id':request.task_id,
            'display_layout':DISPLAY_LAYOUT,'obsidian_observed':'unknown','remote_synced':'not_applicable','attempted_at':time.time()}
        with file_write_lock(tracker.root/('archive-'+hashlib.sha256(request.task_id.encode()).hexdigest())):
            with tracker._connection() as c:
                record=c.execute('SELECT payload FROM archive_rounds WHERE id=?',(key,)).fetchone()
                if not record:
                    c.execute('INSERT INTO archive_rounds VALUES(?,?,0,?)',(key,request.task_id,encoded({'status':'planned','count':len(versions),'watermark':0,'role':'selection'})))
                    c.executemany('INSERT INTO archive_members VALUES(?,?,?)',[(key,i,v) for i,v in enumerate(versions)])
            try:
                receipt['path']=str(tracker._task_directory(context));self.save(receipt)
                record=tracker._publish_flat(key,context)
                with tracker._connection() as c:
                    parts=[json.loads(row[0]) for row in c.execute('SELECT payload FROM archive_parts WHERE round_id=? ORDER BY ordinal',(key,))]
                entries=[]
                for part in parts:
                    entry={'file':part['file'],'asset':self.assets.fetch(part['version_id'],max_chars=1)['asset'],
                        'text_version_id':part.get('text_version_id',part['version_id']),'locator_target':'KR_fixed_version_unicode_code_points'}
                    if entry not in entries:entries.append(entry)
                receipt.update(status='local_committed',path=record['path'],identity=record['identity'],file_hashes=record['file_hashes'],
                    entries=entries,navigation=record['navigation'],excluded_assets=record.get('skipped_assets',{}),verified_at=time.time())
            except ExecutionStopped as exc:
                receipt.update(status='interrupted',error_code=exc.error_code,next_action='inspect_then_repeat_same_request')
            except (OSError,ValueError) as exc:
                retryable=isinstance(exc,OSError) and not isinstance(exc,FileExistsError)
                receipt.update(status='retryable_failure' if retryable else 'conflict',error_code=str(exc) if isinstance(exc,ValueError) else type(exc).__name__,
                    retryable=retryable,winerror=getattr(exc,'winerror',None),next_action='repeat_same_request_to_reconcile' if retryable else 'preserve_destination_and_inspect')
            self.save(receipt)
            return receipt


def execute(request, store=None):
    store=store or DeliveryStore()
    archive=ResearchArchive(store)
    if isinstance(request,(PlanArchiveLayout,ApplyArchiveLayout)):
        from runtime.archive_layout import plan, apply
        return apply(archive,request) if isinstance(request,ApplyArchiveLayout) else plan(archive,request)
    if isinstance(request,BindContext):return archive.bind(request)
    if isinstance(request,SubmitReport):return archive.submit(request)
    if isinstance(request,RebindReportOutput):return archive.rebind_report_output(request)
    if isinstance(request,ArchiveStatus):return archive.status(request.task_id)
    if isinstance(request,(ArchiveTask,RetryArchive)):return archive.run(request.task_id,retry=isinstance(request,RetryArchive))
    if isinstance(request,SnapshotResearch):
        from runtime.research_backup import snapshot
        return snapshot(store,request)
    if isinstance(request,RestoreResearch):
        from runtime.research_backup import restore
        return restore(request)
    if isinstance(request,RegisterReport):
        bound=archive._context(request.task_id)
        if bound and bound['project_id']!=request.project_id:raise ValueError('research_project_mismatch_use_new_task')
        result=register_report(assets=store.assets,**request.model_dump(exclude={'action'}))
        if archive._context(request.task_id):
            archive.attach_registered(request.task_id,request.project_id,result['asset'],request.report_id)
            result['archive']=archive.run(request.task_id)
        return result
    if isinstance(request,Authorize):return store.authorize(request)
    if isinstance(request,Revoke):return store.revoke(request.grant_id)
    if isinstance(request,DeliveryStatus):return store.status(request.delivery_id)
    if isinstance(request,Reindex):
        with store.assets.connection() as c:
            c.execute("INSERT OR IGNORE INTO asset_scopes SELECT version_id,'',owner FROM asset_refs WHERE owner LIKE 'kr-research-%'")
            from runtime.research_workspace import ResearchRecords
            records=ResearchRecords(store.root.parent,assets=store.assets)
            for task_id, in c.execute("SELECT DISTINCT task_id FROM asset_scopes").fetchall():
                try:project=records.snapshot(task_id).get('project_id','')
                except ValueError:continue
                if project:c.execute("INSERT OR IGNORE INTO asset_scopes SELECT version_id,?,task_id FROM asset_scopes WHERE task_id=?",(project,task_id))
            c.execute("UPDATE knowledge_meta SET value=value+1 WHERE key='generation'")
        return {"status":"index_upgraded","content_reprocessed":False}
    return store.deliver(request)
