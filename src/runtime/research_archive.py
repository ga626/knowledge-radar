"""显式任务归属、增量全文归档与可恢复双端报告交付。无研究模型。"""
from __future__ import annotations
from contextlib import ExitStack, closing
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit
from typing import Literal
from pydantic import Field
from runtime.execution_context import check_execution
from runtime.file_write_lock import file_write_lock, read_observed
from runtime.knowledge import Contract, encoded, guarded_path
from runtime.research_storage import admit_bytes, reserve_bytes


class BindContext(Contract):
    action: Literal['bind_context']
    task_id: str = Field(min_length=1, max_length=96)
    project_id: str = Field(min_length=1, max_length=200)
    project_name: str = Field(min_length=1, max_length=80)
    task_title: str = Field(min_length=1, max_length=80)
    local_grant_id: str
    local_report_path: str
    vault_grant_id: str = ''
    vault_project_dir: str = Field(default='', description='Codex选择的Vault项目目录；同一项目跨任务复用。初次绑定遇已有目录时必须明确提供，不自动加序号新建项目。')
    adopt_legacy_scopes: bool = False


class SubmitReport(Contract):
    action: Literal['submit_report']
    task_id: str = Field(min_length=1, max_length=96)
    project_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=2*1024*1024)
    report_id: str = Field(default='final',min_length=1,max_length=200)
    report_role: Literal['final','stage'] = 'final'
    local_report_path: str = ''
    # 固定归档轮次可作为大批引用的句柄，不要求AI传巨型版本数组。
    source_round: str = ''


class ArchiveTask(Contract):
    action: Literal['archive_task']
    task_id: str = Field(min_length=1, max_length=96)


class ArchiveStatus(Contract):
    action: Literal['archive_status']
    task_id: str = Field(min_length=1, max_length=96)


class RetryArchive(Contract):
    action: Literal['retry_archive']
    task_id: str = Field(min_length=1, max_length=96)


class RebindReportOutput(Contract):
    action: Literal['rebind_report_output']
    task_id: str = Field(min_length=1,max_length=96)
    project_id: str = Field(min_length=1,max_length=200)
    local_report_path: str = ''
    vault_report_path: str = ''


class PlanArchiveLayout(Contract):
    action: Literal['plan_archive_layout']
    task_id: str = Field(min_length=1, max_length=96)
    project_id: str = Field(min_length=1, max_length=200)


class ApplyArchiveLayout(Contract):
    action: Literal['apply_archive_layout']
    task_id: str = Field(min_length=1, max_length=96)
    project_id: str = Field(min_length=1, max_length=200)
    plan_id: str = Field(min_length=64, max_length=64)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def stable_target(root, project):
    from runtime.knowledge_delivery import identity
    return sha(encoded([str(Path(root).resolve()).casefold(), identity(root), project]).encode())


def _save_file(path, raw):
    with reserve_bytes(Path(path).parent,len(raw)),Path(path).open('xb') as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())


def _stage_file(path, raw):
    """中断后的同一暂存计划可复用，内容变化仍拒绝覆盖。"""
    if Path(path).is_symlink() or (hasattr(Path(path),'is_junction') and Path(path).is_junction()):
        raise ValueError('staging_modified_preserved')
    if Path(path).exists():
        if Path(path).read_bytes()!=raw:raise ValueError('staging_modified_preserved')
    else:
        _save_file(path,raw)


def _safe_name(value, fallback='未命名资料'):
    import re
    import unicodedata
    value=unicodedata.normalize('NFC',str(value))
    value=re.sub(r'(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{10,}|\d{10,}', '', value)
    value = re.sub(r'[<>:"/\\|?*#\[\]^\x00-\x1f]', ' ', value)
    value=re.sub(r'\s+',' ',value).strip(' ._-')[:48].rstrip(' .')
    if re.fullmatch(r'(?i)(?:index|final[-_]report|round[-_]?\d*|texts?[-_]?\d*|manifest|sources)(?:\.md)?',value):value=''
    # 来源标题也可能带脚本式标签；不能借标题把机器词重新带进展示路径。
    for pattern,label in ((r'(?i)\bfinal[-_]report\b','总报告'),(r'(?i)\bround[-_]\d*','资料'),(r'(?i)\bindex\b','目录')):
        value=re.sub(pattern,label,value)
    if not value or value.upper().split('.')[0] in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}:
        value = fallback
    return value.strip(' ._-') or fallback


DISPLAY_LAYOUT = 'source-flat-v2'
SOURCE_LABELS = {
    'wechat':'微信', 'weixin':'微信', '微信公众号':'微信', '微信':'微信',
    'github':'GitHub', 'github_gh':'GitHub', 'github_rest':'GitHub',
    'web':'网页', 'generic_web':'网页', 'bilibili':'B站', 'b站':'B站',
    'xhs':'小红书', 'xiaohongshu':'小红书', 'youtube':'YouTube',
    'boss':'BOSS直聘', 'zhipin':'BOSS直聘', 'liepin':'猎聘', 'maimai':'脉脉',
    'v2ex':'V2EX', 'zhihu':'知乎', 'weibo':'微博', 'douyin':'抖音',
    'academic':'学术', 'openalex':'学术', 'crossref':'学术', 'arxiv':'学术',
    'europepmc':'学术', 'pubmed':'学术', 'pubscholar':'学术',
    'semantic_scholar':'学术', 'semanticscholar':'学术', 'unpaywall':'学术',
    'twitter':'推特', 'x':'推特', '推特':'推特',
    '小红书':'小红书', 'boss直聘':'BOSS直聘', '猎聘':'猎聘', '脉脉':'脉脉',
    '知乎':'知乎', '微博':'微博', '抖音':'抖音', '网页':'网页', '学术':'学术',
}
SOURCE_HOSTS = {
    'mp.weixin.qq.com':'微信', 'weixin.sogou.com':'微信',
    'github.com':'GitHub', 'raw.githubusercontent.com':'GitHub',
    'bilibili.com':'B站', 'b23.tv':'B站', 'xiaohongshu.com':'小红书', 'xhslink.com':'小红书',
    'youtube.com':'YouTube', 'youtu.be':'YouTube', 'zhipin.com':'BOSS直聘',
    'liepin.com':'猎聘', 'maimai.cn':'脉脉', 'v2ex.com':'V2EX', 'zhihu.com':'知乎',
    'weibo.com':'微博', 'douyin.com':'抖音', 'arxiv.org':'学术', 'pubmed.ncbi.nlm.nih.gov':'学术',
    'europepmc.org':'学术', 'pubscholar.cn':'学术', 'doi.org':'学术',
    'twitter.com':'推特', 'x.com':'推特',
}


def source_label(row):
    """来源平台优先于抓取器：同一B站资料不因走不同工具而改分类。"""
    meta = json.loads(row.get('metadata') or '{}')
    presentation = meta.get('source_presentation') or {}
    # 新平台由适配器提供一次展示合同；网页/学术不会按工具、站点裂变。
    if presentation.get('group') in {'web','academic'}:
        return {'web':'网页','academic':'学术'}[presentation['group']]
    if presentation.get('group') == 'platform' and presentation.get('label'):
        return _safe_name(str(presentation['label']), '其他平台')[:24]
    if row['kind'].startswith('academic') or row['kind']=='document_text':
        return '学术'
    provenance = meta.get('provenance') or {}
    if str(provenance.get('provider_id') or '').lower() in SOURCE_LABELS and SOURCE_LABELS[str(provenance['provider_id']).lower()]=='学术':
        return '学术'
    host = (urlsplit(row['source_url']).hostname or '').lower()
    for domain, label in SOURCE_HOSTS.items():
        if host == domain or host.endswith('.'+domain):
            return label
    for value in (meta.get('source_platform'), meta.get('platform'), provenance.get('provider_id')):
        if str(value or '').lower() in SOURCE_LABELS:
            return SOURCE_LABELS[str(value).lower()]
    if row['kind'].startswith('academic') or row['kind']=='document_text':
        return '学术'
    return '网页' if host else '本地资料'


def display_subject(row):
    """只复用元数据和原题，不翻译、不调用模型、不从正文猜主题。"""
    meta = json.loads(row.get('metadata') or '{}')
    title = next((str(meta[key]) for key in ('display_title_zh','title_zh','display_title')
                  if isinstance(meta.get(key),str) and meta[key].strip()), row['title'])
    host = (urlsplit(row['source_url']).hostname or '').lower()
    presentation = meta.get('source_presentation') or {}
    sites = ['Obsidian Help','Developer Documentation','哔哩哔哩_bilibili','哔哩哔哩','小红书','YouTube','BOSS直聘','猎聘','脉脉',
             meta.get('site_name'), presentation.get('site'), source_label(row)]
    if host.endswith('.readthedocs.io'):sites.append(host.split('.')[0])
    if host=='docs.python.org':sites.append('Python')
    # 仅剥离已知站点尾缀，不把文章副标题当网站名删除。
    for site in filter(None, sites):
        title = re.sub(r'\s+(?:[-|–—]+)\s*'+re.escape(str(site))+r'(?:\s+\d+(?:\.\d+){1,3})?(?:\s*(?:documentation|docs|文档))?\s*$', '', title, flags=re.I)
    # 文档题名常用“模块 --- 中文主题”；模块及完整原题仍在证据元数据中。
    title = re.sub(r'^[A-Za-z_][\w.]{0,63}\s+---\s+(?=[\u3400-\u9fff])', '', title)
    title = re.sub(r'\s+[-–—|]+\s+', '：', title)
    return _safe_name(title, (urlsplit(row['source_url']).hostname or '未命名资料'))


def task_display_title(metadata, title):
    """首次创建日固定；重复交付不使用当前日期重命名。"""
    created = metadata.get('created_at')
    try:
        date = datetime.fromisoformat(str(created).replace('Z','+00:00')).astimezone().date().isoformat()
    except (ValueError, TypeError):
        date = datetime.now().astimezone().date().isoformat()
    clean = _safe_name(title,'未命名调研')
    return clean if clean.startswith(date+' ') else date+' '+clean


def calendar_layout(metadata, title):
    """按首次创建时间固定本地日历；全角冒号可作为Windows文件名。"""
    try:
        created=datetime.fromisoformat(str(metadata.get('created_at')).replace('Z','+00:00')).astimezone()
    except (ValueError,TypeError):
        created=datetime.now().astimezone()
    return {'directory_layout':'project-year-task-v1', 'directory_year':f'{created.year}年',
        'directory_title':f'{created:%m}月{created:%d}日 {created:%H}：{created:%M} '+_safe_name(title,'未命名调研')}


def _readable_structure(kind, text, meta):
    locations = meta.get('locators') or []
    try:
        json.loads(text)
        blocks=[text]
    except ValueError:
        blocks = [text[item['start']:item['end']] for item in locations] if locations else [text]
    entries = []
    for block in blocks:
        try:
            value = json.loads(block)
        except (ValueError, TypeError):
            raise ValueError('structured_evidence_not_renderable') from None
        entries.extend(value if isinstance(value,list) else [value])
    output=[]
    for i, entry in enumerate(entries,1):
        if not isinstance(entry,dict):
            raise ValueError('structured_evidence_not_renderable')
        if kind=='comments':
            body=next((str(entry[k]) for k in ('content','text','body','message','value') if entry.get(k)), '')
            if not body:raise ValueError('comment_body_missing')
            author=entry.get('author') or entry.get('user') or entry.get('user_name') or '匿名'
            if isinstance(author,dict):author=author.get('name') or author.get('nickname') or '匿名'
            output.append(f'### 评论 {i} · {_safe_name(author,"匿名")}\n')
            # 评论原生身份和回复关系只在正文出现，不进入文件名。
            for key,label in (('comment_id','评论编号'),('id','原始编号'),('parent_comment_id','回复对象'),('parent_id','回复对象'),('created_at','原始时间')):
                if entry.get(key) is not None:output.append(f'{label}：{entry[key]}  ')
            output.extend(['',body,''])
        else:
            rows=entry.get('rows') or entry.get('data') or entry.get('cells')
            columns=entry.get('columns') or entry.get('headers')
            if isinstance(rows,list) and rows:
                if isinstance(rows[0],dict):
                    columns=columns or list(dict.fromkeys(k for row in rows for k in row))
                    rows=[[row.get(k,'') for k in columns] for row in rows]
                if not all(isinstance(row,list) for row in rows):raise ValueError('table_shape_not_renderable')
                width=max(map(len,rows));columns=columns or [f'列 {n+1}' for n in range(width)]
                width=max(width,len(columns));grid=[columns]+rows
                lines=['| '+' | '.join(str(x).replace('|',r'\|').replace('\n','<br>') for x in row+['']*(width-len(row)))+' |' for row in grid]
                lines.insert(1,'| '+' | '.join(['---']*width)+' |');output.extend(lines+[''])
            elif any(isinstance(entry.get(k),str) for k in ('markdown','text','content','value')):
                output.extend([next(entry[k] for k in ('markdown','text','content','value') if isinstance(entry.get(k),str)),''])
            else:
                # 不丢失未知表格字段，降级为可读字段表；机器原件仍保留。
                output.extend([f'### 表格 {i}',*['- '+str(k)+'：'+str(v) for k,v in entry.items()],''])
    return '\n'.join(output).strip()


def readable_asset(assets, row):
    """人读派生件保留父版本；机器资产从展示排除，不能改扩展名冒充正文。"""
    kind=row['kind'];meta=json.loads(row['metadata']);text=assets._verified_text(row)
    if kind in {'transcript_map','images','media_manifest'}:
        return None, '仅供机器定位或媒体引用'
    if kind not in {'content','transcript','ocr_text','pdf_text','document_text','academic_fulltext','readable_text','comments','tables','report'}:
        return None, '未知资料类型，需显式提供正文转换'
    rendered=text
    if kind in {'comments','tables'}:
        rendered=_readable_structure(kind,text,meta)
    elif meta.get('content_format')=='html' or re.match(r'\s*(?:<!doctype\s+html|<html\b)',text,re.I):
        from generic_web.collector import _collect_from_html, GenericWebError
        from generic_web.models import GenericWebRequest
        try:
            rendered=_collect_from_html(GenericWebRequest(url=row['source_url']),html=text,final_url=row['source_url'],status_code=200,content_type='text/html',collector='archive_html').content
        except GenericWebError as exc:
            raise ValueError('html_body_unavailable') from exc
    if rendered==text:return row, ''
    derived=assets.put(row['source_url'],rendered,kind='readable_text',title=row['title'],metadata={
        **{k:v for k,v in meta.items() if k!='locators'},'parent_version_id':row['version_id'],
        'original_kind':kind,'original_captured_at':meta.get('original_captured_at') or meta.get('read_at') or row['captured_at'],
        'source_extractor':meta.get('source_extractor') or meta.get('extractor',''),
        'extractor':'readable-archive/v2','derived_from':[row['version_id']]})
    with assets.connection() as c:
        return dict(c.execute('SELECT * FROM assets WHERE version_id=?',(derived['version_id'],)).fetchone()), ''


def processing_description(row, meta):
    kind=meta.get('original_kind') or row['kind']
    method=meta.get('processing_method')
    if method:return str(method).replace('\n',' ')[:160]
    if kind=='transcript':
        source=str(meta.get('transcript_source') or '')
        if source in {'bilibili_subtitle','youtube_transcript_api','youtube_transcript_api_fallback','youtube_captions'}:
            return '读取平台字幕（'+source+'）'
        if any(word in source.lower() for word in ('asr','sherpa','whisper','qwen3')):
            return '音频语音转写（'+source+'）'
        return '字幕或语音转写（具体方式未记录）'
    extractor=str(meta.get('source_extractor') or meta.get('extractor') or '')
    if kind=='ocr_text':return '画面文字识别（'+extractor+'）' if extractor else '画面文字识别（引擎未记录）'
    if kind in {'pdf_text','document_text','academic_fulltext'}:return '文档正文抽取'+('（'+extractor+'）' if extractor else '')
    return '正文抽取'+('（'+extractor+'）' if extractor else '（工具未记录）')


def asset_header(row, *, include_title=False):
    meta=json.loads(row['metadata'])
    original=meta.get('original_captured_at') or meta.get('fetched_at') or meta.get('read_at')
    if isinstance(original,(int,float)):
        original=datetime.fromtimestamp(original,timezone.utc).isoformat()
    kind=meta.get('original_kind') or row['kind']
    labels={'content':'正文','transcript':'转写或字幕','ocr_text':'识图文字','comments':'评论','tables':'表格','pdf_text':'论文正文','document_text':'论文正文','academic_fulltext':'论文正文','readable_text':'整理正文'}
    coverage={'complete':'完整','partial':'部分','unknown':'未核验','complete_selectable_text':'完整可选文字'}
    observed_coverage=meta.get('coverage')
    if isinstance(observed_coverage,dict):
        # 评论等公共合同提供结构化覆盖，不能把dict当作映射键。
        coverage_label='；'.join(str(key)+'：'+str(value) for key,value in observed_coverage.items()) or '未核验'
    else:
        coverage_label=coverage.get(str(observed_coverage),'未核验')
    site=(meta.get('source_presentation') or {}).get('site') or meta.get('site_name') or urlsplit(row['source_url']).hostname or '本地资料'
    clean=lambda value: str(value).replace('\n',' ').replace('\r',' ')
    title=f'## {display_subject(row)}\n\n' if include_title else ''
    provenance=meta.get('provenance') or {}
    attribution='；'.join(label+'：'+clean(provenance[key]) for key,label in
        (('publisher','发布者'),('author','署名'),('published_at','原文时间')) if provenance.get(key))
    return (title+'> [!info] 资料摘要\n'
            f'> 来源网站：{clean(site)} · [原文](<{row["source_url"].replace(">","%3E").replace(chr(10),"")}>)\n'
            + (f'> {attribution}\n' if attribution else '') +
            f'> 处理方式：{clean(processing_description(row,meta))}\n'
            f'> 资料内容：{labels.get(kind,kind)}；语言：{clean(meta.get("language") or "未标注，保持原文")}；完整性：{clean(coverage_label)}\n'
            f'> 采集时间：{clean(original or "未记录")}\n\n---\n\n')


class ResearchArchive:
    def __init__(self, store):
        self.store = store
        self.assets = store.assets
        self.root = store.root

    def _connection(self):
        return self.store.connection()

    def _ensure(self):
        with self._connection() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS research_context(task_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS archive_rounds(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,watermark INTEGER NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS archive_members(round_id TEXT NOT NULL,ordinal INTEGER NOT NULL,version_id TEXT NOT NULL,PRIMARY KEY(round_id,ordinal));
            CREATE TABLE IF NOT EXISTS archive_parts(round_id TEXT NOT NULL,ordinal INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(round_id,ordinal));
            CREATE TABLE IF NOT EXISTS archive_display_names(owner TEXT PRIMARY KEY,parent TEXT NOT NULL,name_key TEXT NOT NULL,payload TEXT NOT NULL,UNIQUE(parent,name_key));
            CREATE INDEX IF NOT EXISTS archive_parts_version ON archive_parts(json_extract(payload,'$.version_id'));
            ''')

    def _owner_key(self,root,owner):
        from runtime.knowledge_delivery import identity
        root=Path(root);fingerprint=identity(root);canonical=str(root).casefold()
        with self._connection() as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='archive_root_aliases'").fetchone():
                row=c.execute('SELECT identity,canonical_path,canonical_identity FROM archive_root_aliases WHERE root=?',(str(root),)).fetchone()
                if row:
                    if json.loads(row[0])!=fingerprint:raise ValueError('destination_identity_changed')
                    canonical=row[1].casefold();fingerprint=json.loads(row[2])
        return sha(encoded([fingerprint,canonical,owner]).encode())

    def _display_path(self, root, parent, owner, title, *, directory=False, suffix='', explicit_directory=None):
        """自然名称只负责展示；持久占位和文件身份在KR内，重试不重新起名。"""
        from runtime.knowledge_delivery import identity
        self._ensure();root=Path(root);parent=Path(parent)
        guarded_path(root,parent/'__kr_display_scope__');parent=parent.resolve()
        project=owner[0]=='project'
        owner=self._owner_key(root,owner)
        explicit=guarded_path(root,explicit_directory) if explicit_directory is not None else None
        with file_write_lock(self.root/'archive-display-names'):
            with self._connection() as c:
                row=c.execute('SELECT payload FROM archive_display_names WHERE owner=?',(owner,)).fetchone()
            if row:
                record=json.loads(row[0]);path=Path(record['path'])
                if directory:
                    old_path=path
                    if project:record.setdefault('binding_path',str(path))
                    path=self._find_committed(root,record)
                    if explicit is not None and explicit not in {old_path,path,Path(record.get('binding_path') or old_path)}:raise ValueError('archive_project_target_changed')
                    with self._connection() as c:
                        c.execute('UPDATE archive_display_names SET payload=? WHERE owner=?',(encoded(record),owner))
                return guarded_path(root,path)
            if explicit is not None:
                # 目录归属由调用方明确给出；只绑定目录，不改已有文件。
                with _parents(root,explicit):pass
                if not explicit.is_dir():raise ValueError('archive_project_requires_directory')
                record={'path':str(explicit),'identity':identity(explicit)}
                with self._connection() as c:
                    # 同一状态内不同稳定项目不能悄悄认领同一路径。
                    if c.execute('SELECT 1 FROM archive_display_names WHERE parent=? AND name_key=?',(str(explicit.parent).casefold(),explicit.name.casefold())).fetchone():
                        raise ValueError('archive_project_directory_already_bound')
                    c.execute('INSERT INTO archive_display_names VALUES(?,?,?,?)',(owner,str(explicit.parent).casefold(),explicit.name.casefold(),encoded(record)))
                return explicit
            name=_safe_name(title,'未命名调研' if directory else '未命名资料')
            if suffix and name.casefold().endswith(suffix.casefold()):name=name[:-len(suffix)] or '资料'
            available=230-len(str(parent))-len(suffix)-12
            if available<8:raise ValueError('display_path_too_long_choose_shorter_destination')
            name=name[:min(48,available)].rstrip(' .')
            for number in range(1,10001):
                candidate=name+(f'（{number}）' if number>1 else '')+suffix
                path=parent/candidate;key=candidate.casefold()
                with self._connection() as c:
                    used=c.execute('SELECT 1 FROM archive_display_names WHERE parent=? AND name_key=?',(str(parent).casefold(),key)).fetchone()
                if path.exists() or used:
                    if project:raise ValueError('project_directory_exists_bind_context_with_vault_project_dir')
                    continue
                record={'path':str(path)}
                if directory:
                    with _parents(root,parent):
                        try:path.mkdir()
                        except FileExistsError:continue
                        record['identity']=identity(path)
                with self._connection() as c:
                    c.execute('INSERT INTO archive_display_names VALUES(?,?,?,?)',(owner,str(parent).casefold(),key,encoded(record)))
                return path
        raise ValueError('display_name_collision_limit_choose_clearer_title')

    def _task_directory(self, context):
        root=self._grant(context['vault_grant_id'],context['project_id'])
        if context.get('display_layout') not in {'human-v1',DISPLAY_LAYOUT}:return root/context['vault_relative']
        project=context.get('vault_project_dir','')
        parent=self._display_path(root,root,['project',context['project_id']],context['project_name'],directory=True,
            explicit_directory=root/project if project else None)
        if context.get('directory_layout')=='project-year-task-v1':
            parent=self._display_path(root,parent,['year',context['project_id'],context['directory_year']],context['directory_year'],directory=True)
        title=context.get('directory_title') or context['task_title']
        path=self._display_path(root,parent,['task',context['project_id'],context['task_id']],title,directory=True)
        context['vault_relative']=str(path.relative_to(root))
        return path

    def _context(self, task):
        # 查询只读，不建库、不修复、不排队。
        if not self.store.db.is_file():
            return None
        with closing(sqlite3.connect(self.store.db.resolve().as_uri()+'?mode=ro', uri=True)) as c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE name='research_context'").fetchone():
                return None
            row = c.execute('SELECT payload FROM research_context WHERE task_id=?', (task,)).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, context):
        with self._connection() as c:
            c.execute('INSERT INTO research_context VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET payload=excluded.payload', (context['task_id'], encoded(context)))

    def _grant(self, grant_id, project):
        from types import SimpleNamespace
        return self.store._grant(SimpleNamespace(grant_id=grant_id, project_id=project))

    def bind(self, request):
        self._ensure()
        with file_write_lock(self.root/('archive-'+sha(request.task_id.encode()))):
            local = self._grant(request.local_grant_id, request.project_id)
            target = guarded_path(local, request.local_report_path)
            if target.suffix.lower() != '.md':
                raise ValueError('local_report_must_be_markdown')
            vault = self._grant(request.vault_grant_id, request.project_id) if request.vault_grant_id else None
            if request.vault_project_dir:
                if not vault:
                    raise ValueError('vault_grant_required')
                guarded_path(vault, vault/request.vault_project_dir)
            context = request.model_dump(exclude={'action', 'adopt_legacy_scopes'})
            context.update(local_report_path=str(target), context_revision=1, summary='summary_pending', research='open', report=None,
                local={'status':'pending'}, vault={'status':'pending' if vault else 'not_configured'}, last_watermark=0,
                observed_revision=0, retry_at=0, attempts=0, error_code='', remote_synced='not_applicable',display_layout=DISPLAY_LAYOUT,
                directory_title=task_display_title(self._ledger(request.task_id),request.task_title))
            context.update(calendar_layout(self._ledger(request.task_id),request.task_title))
            context['vault_relative']=str(Path(request.vault_project_dir or _safe_name(request.project_name,'未命名项目'))/_safe_name(request.task_title,'未命名调研'))
            old = self._context(request.task_id)
            if old:
                if old['project_id'] != request.project_id:
                    raise ValueError('research_project_mismatch_use_new_task')
                prior_project=Path(old['vault_relative']).parent
                if old.get('directory_layout')=='project-year-task-v1':prior_project=prior_project.parent
                if request.vault_project_dir and str(Path(request.vault_project_dir)) not in {
                    str(prior_project), str(Path(old.get('vault_project_dir') or prior_project))}:
                    raise ValueError('archive_target_changed')
                context['vault_relative']=old['vault_relative']
                for field in ('local_report_path','vault_relative'):
                    if old[field] != context[field]:
                        raise ValueError('archive_context_immutable_use_explicit_relocation')
                # 再授权只更新授权句柄；输出身份和目录不随随机grant变化。
                for field in ('local_grant_id','vault_grant_id'):
                    if old.get(field):
                        prior_root = self._grant_root_even_revoked(old[field], request.project_id)
                        next_root = self._grant(context[field], request.project_id)
                        if stable_target(prior_root,request.project_id) != stable_target(next_root,request.project_id):
                            raise ValueError('archive_target_changed')
                old.update(project_name=request.project_name, task_title=request.task_title,
                    local_grant_id=request.local_grant_id, vault_grant_id=request.vault_grant_id,
                    vault_project_dir=request.vault_project_dir or old.get('vault_project_dir',''), context_revision=old['context_revision']+1)
                context = old
            self._bind_ledger(request,validate_only=True)
            if not old and vault:
                if request.vault_project_dir and any(_safe_name(part)!=part for part in Path(request.vault_project_dir).parts):
                    raise ValueError('vault_project_dir_requires_human_readable_names')
                self._task_directory(context)
            with self.assets.connection() as c:
                foreign=c.execute('SELECT DISTINCT project_id FROM asset_scopes WHERE task_id=? AND project_id!=?',(request.task_id,request.project_id)).fetchall()
                if foreign:
                    names={row[0] for row in foreign}
                    if not request.adopt_legacy_scopes or not names <= {'',request.project_name}:
                        raise ValueError('legacy_scope_requires_explicit_adoption')
                    c.execute('INSERT OR IGNORE INTO asset_scopes SELECT version_id,?,task_id FROM asset_scopes WHERE task_id=?',(request.project_id,request.task_id))
                    c.execute('DELETE FROM asset_scopes WHERE task_id=? AND project_id!=?',(request.task_id,request.project_id))
            self._bind_ledger(request)
            context.pop('restored_requires_reauthorization',None)
            self._save(context)
        self._wake()
        return self.status(request.task_id)

    def _grant_root_even_revoked(self, grant, project):
        with self._connection() as c:
            row = c.execute('SELECT root,identity,project FROM grants WHERE id=?',(grant,)).fetchone()
        from runtime.knowledge_delivery import identity
        if not row or row[2]!=project or encoded(identity(row[0]))!=row[1]:
            raise ValueError('destination_identity_changed')
        return Path(row[0])

    def _bind_ledger(self, request, *, validate_only=False):
        from runtime import research_ledger as ledger
        with ledger._ledger_guard(request.task_id):
            item = ledger._read(request.task_id)
            if not item or item.get('research_task_id')!=request.task_id:
                raise ValueError('research_task_required')
            if item.get('project_id') and item['project_id']!=request.project_id:
                raise ValueError('research_project_mismatch_use_new_task')
            # 没有ID的历史任务只能显式接纳，不从名字猜测。
            if not item.get('project_id') and (item.get('project_name') or '') and not request.adopt_legacy_scopes:
                raise ValueError('legacy_project_requires_explicit_adoption')
            if validate_only:return
            item.update(project_id=request.project_id,project_name=request.project_name,report_obligation='required')
            ledger._write(item)

    def status(self, task):
        context=self._context(task)
        if context is None:
            return {'status':'context_pending','task_id':task,'missing_fields':['project_id','local_grant_id','local_report_path'],
                'next_action':'bind_context_with_Codex_selected_project_and_targets'}
        rounds=[]
        with closing(sqlite3.connect(self.store.db.resolve().as_uri()+'?mode=ro',uri=True)) as c:
            pending=c.execute("SELECT COUNT(*) FROM archive_rounds WHERE task_id=? AND COALESCE(json_extract(payload,'$.role'),'archive')!='selection' AND (json_extract(payload,'$.status')!='local_committed' OR (? AND json_extract(payload,'$.target')='kr_evidence_store'))",(task,bool(context.get('vault_grant_id')))).fetchone()[0]
            conversion_gaps=sum(number for (raw,) in c.execute('SELECT payload FROM archive_rounds WHERE task_id=?',(task,)) for reason,number in json.loads(raw).get('skipped_assets',{}).items() if reason.startswith('正文转换失败'))
            for row in c.execute("SELECT id,payload FROM archive_rounds WHERE task_id=? AND COALESCE(json_extract(payload,'$.role'),'archive')!='selection' ORDER BY watermark DESC LIMIT 10",(task,)):
                item=json.loads(row[1]);rounds.append({'round_id':row[0],**{key:item.get(key) for key in ('status','count','path','watermark','navigation','skipped_assets')}})
        pending+=int(self._content_watermark(context)>context['last_watermark'])
        complete=context.get('summary')=='delivered' and not pending and not conversion_gaps
        return {key:context.get(key) for key in ('task_id','project_id','project_name','context_revision','summary','research','local','vault','report','error_code','failure_kind','retry_at','remote_synced')} | {'status':'tracked','rounds':rounds,'report_count':len(context.get('report_history',[]))+(1 if context.get('report') else 0),
            'completion':'complete' if complete else 'incomplete',
            'source_delivery':{'status':'pending' if pending else 'incomplete' if conversion_gaps else 'vault_committed' if context.get('vault_grant_id') else 'retained_in_kr','pending_rounds':pending,'conversion_gaps':conversion_gaps},
            'retryable':bool(context.get('error_code') and context.get('failure_kind')=='retryable'),
            'next_action':('resolve_lock_or_space_then_retry_archive_same_fixed_version' if context.get('failure_kind')=='retryable' else 'preserve_user_files_inspect_conflict_then_explicitly_rebind_report_output' if context.get('error_code') else 'submit_report' if context['summary']=='summary_pending' else 'read_fixed_report_or_continue_research'),
            'new_evidence_after_report':bool(context.get('report') and self._content_watermark(context)>context['report'].get('source_watermark',0)),'read_only':True}

    def _wake(self):
        # 使用已存在的内核维护线程；测试或独立库调用不隐式启动运行环境。
        from runtime.task_service import _MAINTENANCE, _MAINTENANCE_LOCK
        with _MAINTENANCE_LOCK:
            for state in _MAINTENANCE.values():
                state['keep_alive'] = True

    def attach_registered(self, task, project, ref, report_id):
        with file_write_lock(self.root/('archive-'+sha(task.encode()))):
            context=self._context(task)
            if not context or context['project_id']!=project:raise ValueError('research_project_mismatch_use_new_task')
            fixed={'version_id':ref['version_id'],'sha256':ref['raw_sha256'],'report_id':report_id,'source_watermark':self._content_watermark(context)}
            if context.get('vault_grant_id'):
                root=self._grant(context['vault_grant_id'],project)
                fixed['vault_name']=self._display_path(root,self._task_directory(context),['report',project,task,report_id],'总报告',suffix='.md').name
            if context.get('report'):
                if any(context['report'].get(key)!=fixed[key] for key in ('version_id','sha256','report_id')):raise ValueError('report_id_already_fixed_use_new_report_id_and_explicit_path')
                return
            context.update(report=fixed,summary='registered',retry_at=0)
            self._save(context)

    def rebind_report_output(self, request):
        """只有显式新目标才补交；撤销/删除或人工修改从不触发自动另存。"""
        with file_write_lock(self.root/('archive-'+sha(request.task_id.encode()))):
            context=self._context(request.task_id)
            if not context or context['project_id']!=request.project_id:raise ValueError('research_project_mismatch_use_new_task')
            if not context.get('report'):raise ValueError('fixed_report_required')
            if not request.local_report_path and not request.vault_report_path:raise ValueError('explicit_new_report_path_required')
            previous={key:context[key].copy() for key in ('local','vault')}
            targets=[]
            for kind,path,grant in [('local',request.local_report_path,context['local_grant_id']),('vault',request.vault_report_path,context['vault_grant_id'])]:
                if not path:continue
                root=self._grant(grant,request.project_id);target=guarded_path(root,path)
                if target.exists() or target.suffix.lower()!='.md':raise ValueError('new_markdown_destination_required')
                targets.append((kind,target))
            for kind,target in targets:
                context[kind]={'status':'pending'}
                context['report'][kind+'_report_path']=str(target)
            context.setdefault('previous_report_targets',[]).append(previous)
            context.update(error_code='',retry_at=0,summary='registered',context_revision=context['context_revision']+1)
            self._save(context)
        return self.run(request.task_id,retry=True)

    def submit(self, request):
        with file_write_lock(self.root/('archive-'+sha(request.task_id.encode()))):
            context=self._context(request.task_id)
            if not context:
                raise ValueError('bind_archive_context_first')
            if context['project_id']!=request.project_id:
                raise ValueError('research_project_mismatch_use_new_task')
            raw=request.content.encode('utf-8')
            if len(raw)>2*1024*1024:
                raise ValueError('report_too_large')
            parents=[]
            if request.source_round:
                with self._connection() as c:
                    round_row=c.execute('SELECT task_id FROM archive_rounds WHERE id=?',(request.source_round,)).fetchone()
                    if not round_row or round_row[0]!=request.task_id:
                        raise ValueError('source_round_outside_task')
                    # 句柄在服务端展开，不进入工具响应上下文。
                    parents=[r[0] for r in c.execute('SELECT version_id FROM archive_members WHERE round_id=? ORDER BY ordinal',(request.source_round,))]
            if context.get('report') and context['report']['report_id']==request.report_id:
                if context['report']['sha256']!=sha(raw):
                    raise ValueError('report_id_already_fixed_use_new_report_id_and_explicit_path')
                return self.status(request.task_id)
            history=context.get('report_history',[])
            if any(x['report']['report_id']==request.report_id for x in history):
                raise ValueError('report_id_already_fixed_use_new_report_id_and_explicit_path')
            local_path=request.local_report_path or (context['local_report_path'] if not context.get('report') else '')
            if not local_path:raise ValueError('new_report_requires_explicit_local_path')
            guarded_path(self._grant(context['local_grant_id'],request.project_id),local_path)
            if Path(local_path).suffix.lower()!='.md':raise ValueError('local_report_must_be_markdown')
            if context.get('report'):
                if context['local']['status']!='local_committed' or context['vault']['status'] not in {'local_committed','not_configured'}:
                    raise ValueError('previous_report_delivery_pending_retry_first')
                history.append({key:context[key] for key in ('report','local','vault')})
                context['report_history']=history
                context.update(local={'status':'pending'},vault={'status':'pending' if context['vault_grant_id'] else 'not_configured'})
            ref=self.assets.put('kr-report:'+sha(encoded([request.project_id,request.task_id,request.report_id]).encode()),request.content,kind='report',title=request.title,
                metadata={'project_id':request.project_id,'task_id':request.task_id,'report_id':request.report_id,'derived_from':parents,
                    'source_round':request.source_round,'extractor':'research-summary/v1:'+sha(encoded(parents).encode()),'coverage':'agent_summary'})
            self.assets.reference_many([ref['version_id']],request.task_id,project_id=request.project_id,task_id=request.task_id)
            vault_name='总报告.md'
            if context.get('vault_grant_id'):
                root=self._grant(context['vault_grant_id'],request.project_id)
                parent=self._task_directory(context)
                title='总报告' if request.report_role=='final' else '阶段报告 - '+_safe_name(request.title,'调研小结')
                vault_name=self._display_path(root,parent,['report',request.project_id,request.task_id,request.report_id],title,suffix='.md').name
            context.update(report={'version_id':ref['version_id'],'sha256':sha(raw),'report_id':request.report_id,'role':request.report_role,
                'local_report_path':str(Path(local_path).resolve()),'source_watermark':self._round_get(request.source_round)['watermark'] if request.source_round else self._content_watermark(context),'vault_name':vault_name},summary='registered',retry_at=0)
            self._save(context)
        self._wake()
        return self.run(request.task_id)

    def _round_plan(self, context):
        # 只把成员ID流入计划表；不把所有正文装进内存。
        with self._connection() as out:
            pending=out.execute("SELECT id FROM archive_rounds WHERE task_id=? AND (json_extract(payload,'$.status')!='local_committed' OR (? AND json_extract(payload,'$.target')='kr_evidence_store')) AND COALESCE(json_extract(payload,'$.role'),'archive')!='selection' ORDER BY watermark,rowid LIMIT 1",(context['task_id'],bool(context.get('vault_grant_id')))).fetchone()
            if pending:
                return pending[0]
        with self.assets.connection() as c:
            watermark=c.execute('SELECT COALESCE(MAX(s.rowid),0) FROM asset_scopes s JOIN assets a USING(version_id) WHERE s.task_id=? AND s.project_id=? AND a.kind!=?',(context['task_id'],context['project_id'],'report')).fetchone()[0]
            if watermark<=context['last_watermark']:
                return ''
            key=sha(encoded([context['task_id'],context['project_id'],context['last_watermark'],watermark]).encode())
            with self._connection() as out:
                count=0
                for row in c.execute('SELECT s.version_id FROM asset_scopes s JOIN assets a USING(version_id) WHERE s.task_id=? AND s.project_id=? AND s.rowid>? AND s.rowid<=? AND a.kind!=? ORDER BY s.rowid',
                    (context['task_id'],context['project_id'],context['last_watermark'],watermark,'report')):
                    out.execute('INSERT OR IGNORE INTO archive_members VALUES(?,?,?)',(key,count,row[0]));count+=1
                out.execute('INSERT OR IGNORE INTO archive_rounds VALUES(?,?,?,?)',(key,context['task_id'],watermark,encoded({'status':'planned','count':count,'watermark':watermark})))
        return key

    def _members(self, key):
        cursor=0
        while True:
            with self._connection() as c:
                rows=c.execute('SELECT ordinal,version_id FROM archive_members WHERE round_id=? AND ordinal>=? ORDER BY ordinal LIMIT 64',(key,cursor)).fetchall()
            if not rows:
                return
            for ordinal,version in rows:
                yield version
                cursor=ordinal+1

    def _round_get(self, key):
        with self._connection() as c:
            return json.loads(c.execute('SELECT payload FROM archive_rounds WHERE id=?',(key,)).fetchone()[0])

    def _round_save(self, key, payload):
        with self._connection() as c:
            c.execute('UPDATE archive_rounds SET payload=? WHERE id=?',(encoded(payload),key))

    def _build_volumes(self, staging, key, context):
        """每次最多一个原文(32MiB)和一个2MiB卷；索引与范围映射逐行写。"""
        hashes={};volume=0;body=bytearray();parts=0;written=0
        pending=[];ordinal=0
        with self._connection() as c:c.execute('DELETE FROM archive_parts WHERE round_id=?',(key,))
        def save_parts():
            if not pending:return
            with self._connection() as c:
                c.executemany('INSERT INTO archive_parts VALUES(?,?,?)',pending)
            pending.clear()
        def volume_name(number):return f'资料汇编（{number}）.md'
        def flush():
            nonlocal body,volume,parts
            if not body:return
            volume+=1;name=volume_name(volume);raw=bytes(body)
            admit_bytes(staging,len(raw));_save_file(staging/name,raw);hashes[name]=sha(raw);body=bytearray();parts=0
        for version in self._members(key):
            check_execution()
            with self.assets.connection() as c:
                row=c.execute('SELECT * FROM assets WHERE version_id=?',(version,)).fetchone()
                if not row:raise ValueError('archive_source_missing')
                row=dict(row);self.assets.assert_available(c,row)
                raw=self.assets._verified_text(row).encode('utf-8')
            start=0
            while start<len(raw):
                if parts>=100 or len(body)>=2*1024*1024-2048:flush()
                captured=datetime.fromtimestamp(row['captured_at'],timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
                header=('\n\n### '+_safe_name(row['title'],'未命名资料')+('（续）' if start else '')+'\n\n'+
                    '来源：<'+row['source_url'].replace('>','%3E').replace('\n','')+'>  \n采集时间：'+captured+'\n\n').encode()
                capacity=max(4,2*1024*1024-len(body)-len(header)-2)
                end=min(len(raw),start+capacity)
                while end<len(raw) and raw[end]&0xC0==0x80:end-=1
                body.extend(header);offset=len(body);body.extend(raw[start:end]);body.extend(b'\n\n')
                item={'version_id':version,'source_id':row['source_id'],'source_url':row['source_url'],'captured_at':row['captured_at'],'raw_sha256':row['raw_sha256'],
                    'raw_bytes':len(raw),'raw_start':start,'raw_end':end,'file':volume_name(volume+1),'export_start':offset,'export_end':offset+end-start,'part_sha256':sha(raw[start:end])}
                pending.append((key,ordinal,encoded(item)));ordinal+=1
                if len(pending)>=64:save_parts()
                parts+=1;written+=end-start;start=end
        flush();save_parts()
        _save_file(staging/'资料目录.md',('# 调研资料\n\n项目：'+context['project_name']+'\n\n任务：'+context['task_title']+'\n\n'+
            '\n'.join(f'- [资料汇编（{i}）]({quote(volume_name(i))})' for i in range(1,volume+1))+
            '\n\n每篇资料保留来源和采集时间。原文校验与精确定位由 KR 保存。\n').encode())
        hashes['资料目录.md']=_hash_file(staging/'资料目录.md')
        return hashes

    def _publish_round(self, key, context):
        record=self._round_get(key)
        if not context['vault_grant_id']:
            # KR原文库本身可完整检索；无Vault无需生成重复全文副本。
            record.update(status='local_committed',target='kr_evidence_store',path=str(self.assets.root))
            self._round_save(key,record)
            return record
        if record.get('target')=='kr_evidence_store':
            # 本地保留和Vault发布是不同目标。复用原成员/版本/水位，不能重采集。
            record={name:value for name,value in record.items() if name not in {'target','path','status'}}
            record['status']='planned'
            self._round_save(key,record)
        if context.get('display_layout')==DISPLAY_LAYOUT:
            return self._publish_flat(key,context)
        root=self._grant(context['vault_grant_id'],context['project_id'])
        if record.get('identity'):
            target=self._find_committed(root,record)
            _verify_files(target,record['file_hashes'],root)
            return record
        target=Path(record['path']) if record.get('path') else self._display_path(root,self._task_directory(context),['round',key],
            '资料' if not context['last_watermark'] else '补充资料')
        from runtime.knowledge_delivery import pin_directory,publish_directory,identity
        with _parents(root,target.parent),pin_directory(root):
            stage=Path(tempfile.mkdtemp(prefix='.kr-archive-',dir=target.parent))
            try:
                hashes=self._build_volumes(stage,key,context)
                # 在可见提交前保存确定性期望，进程退出后可对账而无需重新采集。
                record.update(status='publishing',path=str(target),file_hashes=hashes)
                self._round_save(key,record)
                self._grant(context['vault_grant_id'],context['project_id'])
                for version in self._members(key):self.assets.fetch(version,max_chars=1)
                check_execution()
                if not target.exists():
                    try:publish_directory(stage,target)
                    except FileExistsError:pass
                _verify_files(target,hashes,root)
                record.update(status='local_committed',identity=identity(target),verified_at=time.time())
                self._round_save(key,record)
                return record
            finally:
                if stage.exists():shutil.rmtree(stage)

    def _verify_flat(self, root, record):
        """已发布文件逐个记身份；用户改名/删除不被当成需要重新生成。"""
        from runtime.knowledge_delivery import identity
        folder=self._find_committed(root,record)
        for name,item in record.get('file_records',{}).items():
            candidate=folder/name
            if candidate.exists() and identity(candidate)==item.get('identity'):
                item['path']=str(candidate)
            actual=self._find_committed(root,item)
            if sha(_read_safe(root,actual))!=record['file_hashes'][name]:
                raise ValueError('destination_modified_preserved')
        # 若进程在“已提交”落盘后、finally前退出，也要回收原暂存目录。
        stage=Path(record['staging_path']) if record.get('staging_path') else None
        if stage is not None and stage.exists():
            if stage.parent.resolve() not in {self.root.resolve(),folder.parent.resolve()} or not stage.name.startswith('.kr-archive-') or stage.is_symlink() or (hasattr(stage,'is_junction') and stage.is_junction()):
                raise ValueError('staging_identity_changed')
            for item in stage.iterdir():
                if item.is_symlink() or not item.is_file() or item.name not in record['file_hashes'] or _hash_file(item)!=record['file_hashes'][item.name]:
                    raise ValueError('staging_modified_preserved')
            shutil.rmtree(stage)
        return folder

    def _existing_parts(self, context, version, exclude_key):
        with self._connection() as c:
            rows=c.execute("SELECT p.payload,r.payload FROM archive_parts p JOIN archive_rounds r ON r.id=p.round_id WHERE r.task_id=? AND p.round_id!=? AND json_extract(p.payload,'$.version_id')=? AND json_extract(r.payload,'$.status')='local_committed' AND json_extract(r.payload,'$.display_layout')=? ORDER BY r.rowid,p.ordinal",
                (context['task_id'],exclude_key,version,DISPLAY_LAYOUT)).fetchall()
        if not rows:return []
        result=[];seen=set();folder=self._task_directory(context)
        for part,record in rows:
            part=json.loads(part);record=json.loads(record)
            if record.get('path')!=str(folder):continue
            if part['raw_start'] in seen:continue
            file=record['file_records'][part['file']]
            root=self._grant(context['vault_grant_id'],context['project_id'])
            actual=self._find_committed(root,file)
            if sha(_read_safe(root,actual))!=record['file_hashes'][part['file']]:raise ValueError('destination_modified_preserved')
            part['file']=os.path.relpath(actual,folder);seen.add(part['raw_start']);result.append(part)
        return result

    def _build_flat(self, staging, key, context):
        """小任务每篇一文件；大任务按来源分卷，最多2MiB/100篇。"""
        root=self._grant(context['vault_grant_id'],context['project_id']);folder=self._task_directory(context)
        hashes={};ordinal=0;skipped={};buffer=bytearray();buffer_name='';buffer_label='';parts=0;volume=0
        pending=[];count=self._round_get(key)['count'];batch=count>100
        with self._connection() as c:c.execute('DELETE FROM archive_parts WHERE round_id=?',(key,))
        def save_parts():
            if pending:
                with self._connection() as c:c.executemany('INSERT INTO archive_parts VALUES(?,?,?)',pending)
                pending.clear()
        def flush():
            nonlocal buffer,buffer_name,parts
            if not buffer:return
            _stage_file(staging/buffer_name,bytes(buffer));hashes[buffer_name]=sha(bytes(buffer));buffer=bytearray();buffer_name='';parts=0
        for version in self._members(key):
            check_execution()
            with self.assets.connection() as c:
                row=c.execute('SELECT * FROM assets WHERE version_id=?',(version,)).fetchone()
                if row is None:raise ValueError('archive_source_missing')
                row=dict(row);self.assets.assert_available(c,row)
            reused=self._existing_parts(context,version,key)
            if not reused and row['kind']=='report' and (context.get('report') or {}).get('version_id')==version and context.get('vault',{}).get('status')=='local_committed':
                original_report=context['vault'].copy();actual=self._find_committed(root,original_report)
                raw=self.assets._verified_text(row).encode('utf-8')
                if sha(_read_safe(root,actual))!=sha(raw):raise ValueError('destination_modified_preserved')
                reused=[{'version_id':version,'text_version_id':version,'source_id':row['source_id'],'source_url':row['source_url'],
                    'captured_at':row['captured_at'],'raw_sha256':sha(raw),'original_sha256':sha(raw),'raw_bytes':len(raw),'raw_start':0,'raw_end':len(raw),
                    'file':os.path.relpath(actual,folder),'export_start':0,'export_end':len(raw),'part_sha256':sha(raw),'source_label':'报告','title':row['title']}]
            if reused:
                for item in reused:
                    name=item['file'];hashes[name]=sha(_read_safe(root,folder/name))
                    pending.append((key,ordinal,encoded(item)));ordinal+=1
                    if len(pending)>=64:save_parts()
                continue
            try:readable,reason=readable_asset(self.assets,row)
            except ValueError as exc:
                readable=None;reason='正文转换失败：'+str(exc)
            if readable is None:
                skipped[reason]=skipped.get(reason,0)+1
                continue
            raw=self.assets._verified_text(readable).encode('utf-8');start=0;part_number=0;label=source_label(readable)
            while start<len(raw):
                if not batch or (buffer and (buffer_label!=label or parts>=100 or len(buffer)>=2*1024*1024-4096)):flush()
                if not buffer_name:
                    volume+=1
                    title=(label+' - 资料汇编') if batch else ('总报告' if row['kind']=='report' else label+' - '+display_subject(readable))
                    owner=['source-volume',key,label,volume] if batch else ['source-text',context['project_id'],context['task_id'],version,part_number]
                    buffer_name=self._display_path(root,folder,owner,title,suffix='.md').name;buffer_label=label
                header=b'' if row['kind']=='report' else asset_header(readable,include_title=batch).encode('utf-8')
                if len(header)>16000:raise ValueError('evidence_header_too_large')
                capacity=2*1024*1024-len(buffer)-len(header)-2
                end=min(len(raw),start+capacity)
                while end<len(raw) and raw[end]&0xC0==0x80:end-=1
                buffer.extend(header);offset=len(buffer);buffer.extend(raw[start:end]);buffer.extend(b'\n\n')
                if row['kind']=='report':
                    # 已固定的综合报告与本地项目报告必须逐字节一致。
                    del buffer[-2:]
                item={'version_id':version,'text_version_id':readable['version_id'],'source_id':row['source_id'],'source_url':row['source_url'],
                    'captured_at':row['captured_at'],'raw_sha256':sha(raw),'original_sha256':row['raw_sha256'],'raw_bytes':len(raw),
                    'raw_start':start,'raw_end':end,'file':buffer_name,'export_start':offset,'export_end':offset+end-start,
                    'part_sha256':sha(raw[start:end]),'source_label':label,'title':display_subject(readable)}
                pending.append((key,ordinal,encoded(item)));ordinal+=1
                if len(pending)>=64:save_parts()
                parts+=1;start=end;part_number+=1
                if not batch or start<len(raw):flush()
        flush();save_parts()
        record=self._round_get(key);record['skipped_assets']=skipped;self._round_save(key,record)
        # 选取只生成链接清单，正文与全量归档共用同一份。
        nav_title='选取资料' if record.get('role')=='selection' else '资料导航'
        nav=self._display_path(root,folder,['navigation',key],nav_title,suffix='.md').name
        entries={}
        selected=set(self._members(key)) if record.get('role')=='selection' else None
        with self._connection() as c:
            for raw_part,raw_record,part_round in c.execute("SELECT p.payload,r.payload,p.round_id FROM archive_parts p JOIN archive_rounds r ON r.id=p.round_id WHERE r.task_id=? AND (p.round_id=? OR json_extract(r.payload,'$.status')='local_committed') ORDER BY r.rowid,p.ordinal",(context['task_id'],key)):
                part=json.loads(raw_part)
                if selected is not None and part['version_id'] not in selected:continue
                prior=json.loads(raw_record)
                if part_round!=key and (prior.get('display_layout')!=DISPLAY_LAYOUT or prior.get('path')!=str(folder)):continue
                if prior.get('file_records',{}).get(part['file']):
                    part['file']=os.path.relpath(prior['file_records'][part['file']]['path'],folder)
                entries.setdefault(part['file'],part)
        lines=[f'# {nav_title}', '', '项目：'+context['project_name'], '任务：'+context['task_title'], '',
            '以下为来源全文或可读转译，综合判断请看总报告。未被报告引用的资料同样保留。', '']
        for name,part in sorted(entries.items(),key=lambda item:(item[1].get('source_label',''),item[0])):
            lines.append(f'- [{name.removesuffix(".md")}]({quote(name)})')
        if skipped:
            lines.extend(['','未进入人读正文的资产（原件仍在 KR）：']+[f'- {reason}：{number} 项' for reason,number in skipped.items()])
        _stage_file(staging/nav,('\n'.join(lines)+'\n').encode());hashes[nav]=_hash_file(staging/nav)
        return hashes,nav

    def _publish_flat(self, key, context):
        from runtime.knowledge_delivery import identity
        root=self._grant(context['vault_grant_id'],context['project_id']);record=self._round_get(key)
        if record.get('status')=='local_committed':
            self._verify_flat(root,record);self._round_save(key,record);return record
        target=self._task_directory(context)
        # 同卷临时区只存于KR私密数据；若不同卷，隐藏暂存与任务同级，始终回收。
        stage_root=self.root if identity(self.root)[0]==identity(root)[0] else target.parent
        with _parents(root,target):
            stage=Path(record['staging_path']) if record.get('staging_path') else None
            if stage is not None and (stage.parent.resolve()!=stage_root.resolve() or not stage.name.startswith('.kr-archive-') or stage.is_symlink() or (hasattr(stage,'is_junction') and stage.is_junction())):
                raise ValueError('staging_identity_changed')
            if stage is None or not stage.exists():stage=Path(tempfile.mkdtemp(prefix='.kr-archive-',dir=stage_root))
            record['staging_path']=str(stage);self._round_save(key,record)
            preserve=False
            try:
                hashes,nav=self._build_flat(stage,key,context)
                if any(not p.is_file() or p.name not in hashes for p in stage.iterdir()):raise ValueError('staging_modified_preserved')
                record=self._round_get(key)
                if record.get('file_hashes') and record['file_hashes']!=hashes:raise ValueError('archive_fixed_plan_changed')
                record.update(status='publishing',path=str(target),identity=identity(target),file_hashes=hashes,display_layout=DISPLAY_LAYOUT,navigation=nav)
                self._round_save(key,record)
                for version in self._members(key):self.assets.fetch(version,max_chars=1)
                for name,digest in hashes.items():
                    check_execution();self._grant(context['vault_grant_id'],context['project_id'])
                    item=record.setdefault('file_records',{}).get(name)
                    if item:
                        actual=self._find_committed(root,item)
                    else:
                        actual=guarded_path(root,target/name)
                        if not actual.exists():
                            if not (stage/name).is_file():raise ValueError('archive_reused_file_missing')
                            try:os.link(stage/name,actual)
                            except FileExistsError:pass
                    if sha(_read_safe(root,actual))!=digest:raise ValueError('destination_modified_preserved')
                    record['file_records'][name]={'path':str(actual),'identity':identity(actual)}
                    # 断电后已发布件缺失也不会被重建；每个文件有独立回执。
                    self._round_save(key,record)
                record.update(status='local_committed',verified_at=time.time());self._round_save(key,record)
                return record
            except ValueError as exc:
                preserve=str(exc)=='staging_modified_preserved'
                raise
            finally:
                if not preserve and stage.exists():shutil.rmtree(stage)

    def _find_committed(self, root, record):
        from runtime.knowledge_delivery import identity
        path=Path(record['path']);expected=record.get('identity')
        if path.exists():
            guarded_path(root,path)
            if expected and identity(path)!=expected:raise ValueError('destination_replaced_preserved')
            return path
        # 只检查已授权KR子树；不遍历私人Vault，也不按标题猜测。
        matches=[];count=0
        for base,dirs,files in os.walk(root,followlinks=False):
            dirs[:]=[d for d in dirs if not (Path(base)/d).is_symlink() and not (hasattr(Path(base)/d,'is_junction') and (Path(base)/d).is_junction())]
            for name in dirs+files:
                count+=1
                if count>20000:raise ValueError('relocation_scan_limit_inspect_destination')
                item=Path(base)/name
                if item.is_symlink() or (hasattr(item,'is_junction') and item.is_junction()):continue
                if expected and identity(item)==expected:matches.append(item)
        if len(matches)!=1:
            raise ValueError('destination_missing_preserved_no_recreation')
        record['path']=str(matches[0]);record['relocated']=True
        return matches[0]

    def _report_endpoint(self, context, key, root, path, raw):
        from runtime.knowledge_delivery import identity,pin_directory
        record=context[key]
        if record.get('identity'):
            actual=self._find_committed(root,record)
            if sha(_read_safe(root,actual))!=sha(raw):raise ValueError('destination_modified_preserved')
            record.update(path=str(actual),status='local_committed')
            self._save(context)
            return
        guarded_path(root,path)
        with _parents(root,path.parent),pin_directory(root):
            admit_bytes(path.parent,len(raw))
            record.update(status='publishing',path=str(path),sha256=sha(raw))
            self._save(context)
            if not path.exists():
                temporary=Path(tempfile.mktemp(prefix='.kr-report-',dir=path.parent))
                try:
                    _save_file(temporary,raw)
                    check_execution()
                    # 同卷hardlink是create-only原子文件发布；不覆盖竞争写入。
                    try:os.link(temporary,path)
                    except FileExistsError:pass
                finally:temporary.unlink(missing_ok=True)
            if sha(_read_safe(root,path))!=sha(raw):raise ValueError('destination_modified_preserved')
            record.update(status='local_committed',identity=identity(path),verified_at=time.time())
            self._save(context)

    def run(self, task, *, retry=False):
        self._ensure()
        with file_write_lock(self.root/('archive-'+sha(task.encode()))):
            context=self._context(task)
            if not context:return self.status(task)
            with self._connection() as c:
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='archive_layout_transactions'").fetchone():
                    moving=c.execute("SELECT 1 FROM archive_layout_transactions WHERE task_id=? AND json_extract(payload,'$.status')!='committed' LIMIT 1",(task,)).fetchone()
                    if moving:return self.status(task) | {'error_code':'archive_migration_pending_resume_same_plan'}
            if context.get('restored_requires_reauthorization'):
                return self.status(task) | {'next_action':'bind_context_to_explicitly_reauthorize_restored_targets'}
            if not retry and context.get('retry_at',0)>time.time():return self.status(task)
            try:
                context['error_code']=''
                # 对已交付轮次做身份检查，移动/删除不能导致原位置被重建。
                with self._connection() as c:
                    done=[(r[0],json.loads(r[1])) for r in c.execute('SELECT id,payload FROM archive_rounds WHERE task_id=? AND json_extract(payload,\'$.status\')=?',(task,'local_committed'))]
                if context.get('vault_grant_id'):
                    root=self._grant(context['vault_grant_id'],context['project_id'])
                    for key,record in done:
                        if record.get('identity'):
                            if record.get('display_layout')==DISPLAY_LAYOUT:
                                self._verify_flat(root,record)
                            else:
                                actual=self._find_committed(root,record);_verify_files(actual,record['file_hashes'],root)
                            self._round_save(key,record)
                key=self._round_plan(context)
                if key:
                    record=self._publish_round(key,context)
                    context['last_watermark']=max(context['last_watermark'],record['watermark'])
                ledger=self._ledger(task)
                context['research']='stopped' if ledger and ledger.get('status')!='open' else 'open'
                if context.get('report'):
                    data=self.assets.fetch(context['report']['version_id'],max_chars=1)
                    with self.assets.connection() as c:
                        row=dict(c.execute('SELECT * FROM assets WHERE version_id=?',(context['report']['version_id'],)).fetchone())
                        raw=self.assets._verified_text(row).encode('utf-8')
                    root=self._grant(context['local_grant_id'],context['project_id'])
                    self._report_endpoint(context,'local',root,Path(context['report'].get('local_report_path') or context['local_report_path']),raw)
                    if context['vault_grant_id']:
                        vault=self._grant(context['vault_grant_id'],context['project_id'])
                        self._report_endpoint(context,'vault',vault,Path(context['report']['vault_report_path']) if context['report'].get('vault_report_path') else self._task_directory(context)/context['report'].get('vault_name','总报告.md'),raw)
                    context['summary']=('delivered' if context['report'].get('role','final')=='final' else 'summary_pending') if context['local']['status']=='local_committed' and context['vault']['status'] in {'local_committed','not_configured'} else 'registered'
                with self.assets.connection() as c:
                    rev=c.execute('SELECT revision FROM archive_dirty WHERE task_id=?',(task,)).fetchone()
                    context['observed_revision']=rev[0] if rev else 0
                context.update(retry_at=0,attempts=0)
            except (OSError,ValueError,TimeoutError) as exc:
                from runtime.execution_context import ExecutionStopped
                if isinstance(exc,ExecutionStopped):raise
                context['attempts']+=1
                context.update(error_code=str(exc) if isinstance(exc,ValueError) else ('storage_space_insufficient' if getattr(exc,'errno',0)==28 else type(exc).__name__),
                    retry_at=time.time()+min(3600,2**min(context['attempts'],10)),failure_kind='retryable' if isinstance(exc,(OSError,TimeoutError)) else 'conflict')
            self._save(context)
        return self.status(task)

    def maintain(self, *, limit=2):
        if not self.store.db.is_file():return 0
        if not self._has_contexts():return 0
        with self._connection() as c:
            # 行游标持久轮转，持续等待总结的旧任务不会饿死后续任务。
            c.execute('CREATE TABLE IF NOT EXISTS archive_maintenance(key TEXT PRIMARY KEY,value INTEGER)')
            cursor=(c.execute("SELECT value FROM archive_maintenance WHERE key='cursor'").fetchone() or [0])[0]
            rows=c.execute('SELECT rowid,task_id FROM research_context WHERE rowid>? ORDER BY rowid LIMIT ?',(cursor,limit)).fetchall()
            if not rows:rows=c.execute('SELECT rowid,task_id FROM research_context ORDER BY rowid LIMIT ?',(limit,)).fetchall()
            c.execute("INSERT INTO archive_maintenance VALUES('cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(rows[-1][0] if rows else 0,))
        for _,task in rows:
            context=self._context(task)
            if context.get('restored_requires_reauthorization'):continue
            if context.get('failure_kind')=='conflict' and context.get('error_code'):continue
            with self.assets.connection() as c:
                mark=c.execute('SELECT COALESCE(MAX(s.rowid),0) FROM asset_scopes s JOIN assets a USING(version_id) WHERE s.task_id=? AND s.project_id=? AND a.kind!=?',(task,context['project_id'],'report')).fetchone()[0]
            ledger=self._ledger(task)
            research='stopped' if ledger and ledger.get('status')!='open' else 'open'
            pending_report=context.get('report') and context.get('summary')!='delivered'
            with self._connection() as c:
                pending_round=c.execute("SELECT 1 FROM archive_rounds WHERE task_id=? AND COALESCE(json_extract(payload,'$.role'),'archive')!='selection' AND (json_extract(payload,'$.status')!='local_committed' OR (? AND json_extract(payload,'$.target')='kr_evidence_store')) LIMIT 1",(task,bool(context.get('vault_grant_id')))).fetchone()
            if mark>context['last_watermark'] or pending_round or pending_report or research!=context.get('research') or context.get('error_code'):
                self.run(task)
        return len(rows)

    def _content_watermark(self,context):
        from kr_core.evidence_assets import EvidenceAssets
        with EvidenceAssets(self.assets.root,read_only=True).connection() as c:
            return c.execute('SELECT COALESCE(MAX(s.rowid),0) FROM asset_scopes s JOIN assets a USING(version_id) WHERE s.task_id=? AND s.project_id=? AND a.kind!=?',
                (context['task_id'],context['project_id'],'report')).fetchone()[0]

    def _ledger(self,task):
        if not task or Path(task).name!=task or '/' in task or chr(92) in task:raise ValueError('invalid_research_task_id')
        path=self.root.parent/'research_tasks'/(task+'.json')
        if not path.is_file():return {}
        return json.loads(read_observed(path).decode('utf-8'))

    def _has_contexts(self):
        with closing(sqlite3.connect(self.store.db.resolve().as_uri()+'?mode=ro',uri=True)) as c:
            return bool(c.execute("SELECT 1 FROM sqlite_master WHERE name='research_context'").fetchone())


def _hash_file(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def _read_safe(root,path):
    from runtime.knowledge_delivery import read_observed
    return read_observed(guarded_path(root,path,require_file=True))


def _verify_files(target,hashes,root):
    for name,expected in hashes.items():
        check_execution()
        path=guarded_path(root,Path(target)/name,require_file=True)
        if _hash_file(path)!=expected:raise ValueError('destination_modified_preserved')


class _parents:
    """从授权根逐层建立并pin目录；不跟随替换后的junction。"""
    def __init__(self,root,target):self.root=Path(root);self.target=Path(target);self.stack=ExitStack()
    def __enter__(self):
        from runtime.knowledge_delivery import pin_directory
        try:
            self.stack.enter_context(pin_directory(self.root))
            parent=self.root
            for part in self.target.relative_to(self.root).parts:
                parent=parent/part;guarded_path(self.root,parent);parent.mkdir(exist_ok=True)
                self.stack.enter_context(pin_directory(parent));guarded_path(self.root,parent)
            return self
        except BaseException:
            self.stack.close();raise
    def __exit__(self,*args):return self.stack.__exit__(*args)
