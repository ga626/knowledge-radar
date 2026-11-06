"""知乎阶段编排：采集先释放浏览器；公共媒体下载和OCR可断点恢复。"""
from __future__ import annotations
from urllib.parse import urlsplit
from kr_core import DetailResponse


def _checkpoint(request, identity=None):
    from collectors.platform.zhihu import runtime_identity
    from runtime.operation_checkpoint import OperationCheckpoint
    from runtime.paths import runtime_state_dir
    options = {key:request.options[key] for key in ('include_comments','comment_limit','comments','include_ocr','answer_limit') if key in request.options}
    identity = runtime_identity() if identity is None else identity
    # 模型变更不废弃已采集正文；模型缓存由公共processing_identity自行校验。
    return OperationCheckpoint(runtime_state_dir() / 'browser_link' / 'zhihu' / 'checkpoints',
        {'url':request.url,'options':options,'auto_multimodal':request.auto_multimodal,
         'enable_comment_filtering':request.enable_comment_filtering,'task_scope_id':request.task_scope_id,
         'collection_identity':identity}, identity)


def _with_browser(operation, call):
    from collectors.platform.zhihu import runtime_identity, failure
    from runtime.browser_channel import BorrowedBrowser
    from platform_adapters.common import defer_browser_busy
    identity = runtime_identity()
    browser = BorrowedBrowser(identity.get('profile','unbound'), 'zhihu', identity.get('generation',''), driver='content_script_native_messaging')
    try:
        with browser.lease(operation):
            data = None
            try:
                data = call()
                return data
            finally:
                # All body fragments/comments have completed; keep only a genuine
                # manual/risk hold, never a successful background work page.
                from collectors.platform.zhihu import _site
                try:
                    cleanup = _site().invoke('release',preserve_interaction=True)
                except Exception as exc:
                    cleanup = {'ok':False, 'code':type(exc).__name__}
                if isinstance(data, dict):
                    data['browser_cleanup'] = cleanup.get('data') if cleanup.get('ok') else {
                        'work_page_closed':False, 'cleanup_reason':cleanup.get('code','cleanup_not_confirmed')}
    except RuntimeError as exc:
        if str(exc) != 'browser_session_busy': raise
        defer_browser_busy('zhihu','platform_lease_busy',request_executed=False)
        return failure('platform_lease_busy',request_executed=False)


def collect(request):
    from collectors.platform.zhihu import collect_detail
    from runtime.borrowed_observations import observe_borrowed_result
    checkpoint = _checkpoint(request)
    previous = checkpoint.read() if request.options.get('resume_checkpoint') else {}
    if previous.get('body_verified') and previous.get('collection_identity') == checkpoint.identity:
        data = previous
    else:
        data = _with_browser('zhihu_detail', lambda: collect_detail(request))
        data = observe_borrowed_result('zhihu', data)
        checkpoint = _checkpoint(request)
    if data.get('error'): return data
    checkpoint.write(data)
    ocr = request.options.get('include_ocr', request.auto_multimodal or request.enable_deep_analysis)
    if ocr:
        data.update(status='partial', enhancement_status='queued', _enhancement_pending=True)
        checkpoint.write(data)
        if request.options.get('_detail_stage') == 'collection': return data
        return enhance_detail(request, data)
    return data


def _approved_image(url):
    p = urlsplit(url)
    host = p.hostname or ''
    return p.scheme == 'https' and not p.username and not p.password and p.port in {None,443} and (host == 'zhimg.com' or host.endswith('.zhimg.com'))


def enhance_detail(request, collected):
    from runtime.execution_context import ExecutionStopped, check_execution
    from runtime.resource_concurrency import ResourceDeferred
    from runtime.media_acquisition import acquire_http_asset
    from runtime.media_execution import enhance_asset_batch
    from runtime.media_cache import media_cache_subdir, record_media_cache_entry
    from runtime.model_roles import CLOUD_OCR_COMPONENT
    identity = collected.get('collection_identity')
    if not isinstance(identity, dict) or not collected.get('body_verified'):
        return {**collected, '_enhancement_pending': False, 'status': 'partial',
                'enhancement_status': 'collection_snapshot_unverified',
                'enhancement_gap': '缺少已核验的采集快照；请显式重新采集。'}
    # 增强只读已授权的快照，不依赖当前浏览器连接；重新采集仍校验当前身份。
    checkpoint = _checkpoint(request, identity)
    previous = checkpoint.read()
    # 只恢复相同采集版本；增强接口绝不重新连接页面。
    if previous and previous.get('collection_identity') == collected.get('collection_identity'):
        data = {**collected, **previous}
    else: data = dict(collected)
    data.pop('_enhancement_pending', None)
    if not request.options.get('include_ocr',request.auto_multimodal or request.enable_deep_analysis):
        # 评论附件的公共下载不能顺便触发正文图片OCR。
        return {**data,'enhancement_status':'ok'}
    assets, gaps = [], []
    manifest = data.get('media_manifest') or []
    folder = media_cache_subdir('zhihu', content_id=(data.get('target') or {}).get('identifier','unknown'))
    for image_index, image in enumerate(manifest):
        check_execution()
        url = image.get('url','')
        if image.get('type') != 'image': continue
        if not _approved_image(url):
            gaps.append({'source_url':url,'reason':'image_source_not_supported','locator':image.get('locator')}); continue
        try:
            asset = acquire_http_asset('image',url,folder,previous=previous.get('downloaded_media') or [],headers={'Referer':'https://www.zhihu.com/'})
            asset['locator'] = image.get('locator')
            asset['image_index'] = image_index
            assets.append(asset)
            record_media_cache_entry(asset['path'],kind='image',source_url=url,metadata={'sha256':asset['sha256'],'platform':'知乎'})
        except (ExecutionStopped, ResourceDeferred): raise
        except Exception as exc:
            gaps.append({'source_url':url,'reason':getattr(exc,'error_code',type(exc).__name__),'locator':image.get('locator')})
        data.update(downloaded_media=list(assets),media_gaps=list(gaps)); checkpoint.write(data)
    old_results = (previous.get('ocr') or {}).get('results') or []
    def progress(rows):
        from kr_core.evidence_locators import document_text
        located=[{**row,'locator':assets[index].get('locator'),'image_index':assets[index]['image_index'],
                  'source_url':assets[index]['source_url']} for index,row in enumerate(rows)]
        data['ocr']={'status':'partial','results':located}
        blocks = [{'text':row['text'], 'kind':'ocr', 'image_index':row['image_index']}
                  for row in located if row.get('status') in {'ok','completed','success','acquired'} and row.get('text')]
        data['ocr_text'], data['ocr_text_locators'] = document_text(blocks)
        data['ocr_provenance'] = {'ocr_images':[{key:row[key] for key in
            ('image_index','source_url','input_sha256','model','provider','request_profile','page_metrics') if key in row}
            for row in located if row.get('status') in {'ok','completed','success','acquired'} and row.get('text')]}
        checkpoint.write(data)
    rows=enhance_asset_batch(assets,CLOUD_OCR_COMPONENT,previous=old_results,on_progress=progress)
    okay=not gaps and all(row.get('status') in {'ok','completed','success','acquired'} for row in rows)
    data.update(downloaded_media=assets,media_gaps=gaps,enhancement_status='ok' if okay else 'partial')
    data['ocr']={**data.get('ocr',{}),'status':('ok' if assets else 'not_applicable') if okay else 'partial',
                 'coverage':{'requested_images':sum(image.get('type')=='image' for image in manifest),'acquired_images':len(assets),'processed_images':len(rows)}}
    data['status']='ok' if (okay and data.get('body_complete')
        and (not request.options.get('include_comments',request.enable_comment_filtering) or data.get('comments_complete'))
        and (data.get('transcript_result') or {}).get('status') != 'missing') else 'partial'
    checkpoint.write(data)
    return data


class ZhihuDetailStrategy:
    platform = '知乎'
    def extract(self, request):
        from understanding.evidence import build_detail_evidence
        data = collect(request)
        return DetailResponse.from_legacy(self.platform,request.url,data,evidence=build_detail_evidence(request.url,self.platform,data),
                                          metadata={'strategy':'zhihu_browser_link'})
