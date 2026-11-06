"""知乎只读采集：借用 Browser Link，不导出凭据或持有浏览器进程。"""
from __future__ import annotations
from dataclasses import asdict, dataclass
import hashlib
import re
from urllib.parse import urlencode, urlsplit

ADAPTER_VERSION = 'zhihu-dom-v1'
HOME = 'https://www.zhihu.com/'
HOSTS = {'zhihu.com', 'www.zhihu.com', 'zhuanlan.zhihu.com'}

@dataclass(frozen=True)
class ZhihuTarget:
    kind: str
    identifier: str
    url: str
    question_id: str = ''

def parse_target(url: str) -> ZhihuTarget:
    """ID保持字符串；查询参数和尾斜杠不属于实体身份。"""
    p = urlsplit(url)
    if (p.scheme not in {'https', 'http'} or p.hostname not in HOSTS or p.username or p.password
            or p.port not in {None, 443 if p.scheme == 'https' else 80}):
        raise ValueError('invalid_zhihu_url')
    path = p.path.rstrip('/')
    if p.hostname == 'zhuanlan.zhihu.com':
        m = re.fullmatch(r'/p/([0-9]+)', path)
        if m: return ZhihuTarget('article', m[1], 'https://zhuanlan.zhihu.com/p/' + m[1])
    else:
        m = re.fullmatch(r'/question/([0-9]+)/answer/([0-9]+)', path)
        if m: return ZhihuTarget('answer', m[2], f'{HOME}question/{m[1]}/answer/{m[2]}', m[1])
        m = re.fullmatch(r'/(answer|question|zvideo)/([0-9]+)', path)
        if m: return ZhihuTarget(m[1], m[2], f'{HOME}{m[1]}/{m[2]}', m[2] if m[1] == 'question' else '')
    raise ValueError('unsupported_zhihu_target')

def same_target(requested, actual):
    return (requested.kind == actual.kind and requested.identifier == actual.identifier
            and (not requested.question_id or requested.question_id == actual.question_id))

def _site():
    from runtime.native_site import NativeSite
    return NativeSite('zhihu', '知乎', HOME, HOME + 'signin', _authenticated)

def _authenticated(data):
    return bool(data.get('authenticated_ui') and not data.get('risk') and not data.get('login'))

def runtime_identity(): return {**_site().runtime_identity(), 'adapter_version': ADAPTER_VERSION}
def interaction_status(): return _site().interaction_status()
def request_login(): return _site().request_login()
def complete_login(): return _site().complete_login()
def cancel_login(): return _site().cancel_login()
def probe_auth(): return _site().probe_auth()

def failure(kind, *, url='', message='', **extra):
    manual = kind in {'login_required', 'anti_bot_verification', 'request_outcome_unknown'}
    messages = {
        'access_forbidden': '知乎拒绝访问（403）；没有足够证据判定登录失效，不自动重试或换通道。',
        'anti_bot_verification': '知乎要求安全验证或暂时限制访问；本人处理后显式继续。',
        'rate_limited': '知乎返回限流；停止该支线，不自动重放。',
        'login_required': '知乎明确要求登录；请在已配对的日常浏览器中登录后确认继续。',
        'legal_restriction': '知乎返回法律/地区访问限制。',
        'not_found': '该知乎内容已删除或不存在。',
        'content_restricted': '该内容有付费或权限限制，未取得完整正文。',
        'page_identity_changed': '页面实体与请求ID不一致，停止取数。',
        'page_structure_changed': '未识别目标正文或明确空结果，可能是页面结构变化。',
        'zhihu_adapter_incompatible': 'Browser Link 扩展尚不支持当前知乎适配器；需要与候选程序匹配的扩展版本。',
    }
    message = message or messages.get(kind, '知乎采集未完成：' + kind)
    return {'platform': '知乎', 'url': url, 'status': 'needs_interaction' if manual else 'failed',
            'failure_type': kind, 'content': '', 'items': [], 'total': 0,
            'error': {'type': kind, 'error': message, 'message': message, 'failure_type': kind,
                      'retryable': False, 'manual_action_required': manual},
            'manual_action_required': manual, 'retryable': False,
            'resume_policy': 'stop_no_auto_resume' if kind in {'anti_bot_verification', 'rate_limited', 'request_outcome_unknown'} else 'explicit_retry',
            'recommended_action': "health_check(mode='request_browser_interaction:zhihu:manual_login')" if manual else '核对页面与通道状态后显式重试。', **extra}

def _reader_comments(**payload):
    """Normal reader controls through the existing OpenCLI driver; never API fallback."""
    import json
    from pathlib import Path
    from runtime.opencli_bridge import invoke_page
    requested = parse_target(payload.get('url', ''))
    if requested.kind != 'answer':
        return {'ok': False, 'code': 'comments_target_unsupported'}
    script = Path(__file__).resolve().parents[3] / 'browser_link' / 'extension' / 'sites' / 'zhihu.js'
    source = script.read_text(encoding='utf-8')
    expression = ('(async()=>{globalThis.KRSites={};'
        'globalThis.KRWaitForChange=async(signal,ms)=>new Promise(resolve=>setTimeout(resolve,ms));'
        + source + ';return await KRSites.zhihu.extract("comments",'
        + json.dumps({**payload, 'target':asdict(requested)}, ensure_ascii=False)
        + ',new AbortController().signal)})()')
    return invoke_page(session='zhihu', operation='comments', url=requested.url,
        expression=expression, wait_selector='.AnswerItem', isolated_work=True)


def _invoke(operation, **payload):
    from runtime.execution_context import check_execution
    check_execution()
    response = _reader_comments(**payload) if operation == 'comments' else _site().invoke(operation, **payload)
    check_execution()
    if not response.get('ok'):
        kind = response.get('failure_type') or response.get('code') or 'browser_link_failed'
        if kind in {'unsupported_platform', 'unsupported_operation', 'invalid_site_url'}:
            kind = 'zhihu_adapter_incompatible'
        return {**response, **failure(kind), 'metadata': response.get('metadata', {}),
                'request_executed': response.get('request_executed')}
    data = response.get('data')
    if not isinstance(data, dict) or data.get('adapter_version') != ADAPTER_VERSION:
        return failure('zhihu_adapter_incompatible')
    if data.get('failure_type') and not (operation=='comments' and isinstance(data.get('comments'),list) and data['comments']):
        diagnostic = {key: data[key] for key in ('comment_diagnostics', 'auth_diagnostics') if isinstance(data.get(key), dict)}
        return failure(data['failure_type'], url=data.get('url', ''), http_status=data.get('http_status'), request_executed=True,
                       metadata={**response.get('metadata', {}), **diagnostic})
    return response

def legacy_search_zhihu(keyword: str, limit: int = 10):
    keyword = keyword.strip()
    if not keyword or not 1 <= limit <= 20:
        return failure('invalid_request', message='keyword不能为空；limit必须在1到20之间。')
    url = HOME + 'search?' + urlencode({'type': 'content', 'q': keyword})
    response = _invoke('search', url=url, keyword=keyword, limit=limit)
    if not response.get('ok'): return response
    data = response['data']
    rows, seen = [], set()
    items = data.get('items') or []
    if not isinstance(items, list): return failure('invalid_adapter_payload')
    for row in items:
        if not isinstance(row, dict): continue
        try: target = parse_target(row.get('url', ''))
        except (ValueError, TypeError): continue
        key = (target.kind, target.identifier)
        if key in seen or not row.get('title'): continue
        seen.add(key)
        rows.append({'title': str(row['title']), 'url': target.url, 'author': str(row.get('author') or ''),
                     'desc': str(row.get('summary') or ''), 'content_type': target.kind, 'platform': '知乎',
                     'metadata': {'target': asdict(target), 'body_verified': False, 'discovery_source': 'visible_search_dom'}})
    if not rows and not data.get('empty_confirmed'): return failure('page_structure_changed')
    return {'platform': '知乎', 'items': rows[:limit], 'total': min(len(rows), limit),
            'metadata': {**response.get('metadata', {}), 'runtime_identity': runtime_identity(),
                         'coverage': 'visible_results_bounded', 'empty_confirmed': not rows}}

def _html_to_markdown(html):
    from generic_web.collector import html_fragment_to_markdown
    return html_fragment_to_markdown(html)

def collect_detail(request):
    from runtime.execution_context import check_execution
    try: target = parse_target(request.url)
    except (ValueError, TypeError): return failure('invalid_zhihu_url', url=request.url)
    try:
        answer_limit = max(1, min(20, int(request.options.get('answer_limit', 10))))
        comment_scope=request.options.get('comments') or {}
        comment_limit=max(1,min(500,int(comment_scope.get('total_limit',request.options.get('comment_limit',20)))))
    except (ValueError, TypeError, OverflowError):
        return failure('invalid_request', url=request.url, message='answer_limit、comment_limit 必须为整数。')
    response = _invoke('detail', url=target.url, target=asdict(target), answer_limit=answer_limit)
    if not response.get('ok'): return {**response, 'url': request.url}
    data = dict(response['data'])
    try:
        observed = parse_target(data.get('url', ''))
        declared = data.get('target') or {}
        if not isinstance(declared, dict) or not same_target(target, observed) or declared.get('identifier') != target.identifier or declared.get('kind') != target.kind:
            return failure('page_identity_changed', url=request.url)
    except (ValueError, TypeError): return failure('page_identity_changed', url=request.url)
    chunks = [data.get('body_html', '')]
    parts = data.get('parts', 1)
    if type(parts) is not int or not 1 <= parts <= 64 or not isinstance(chunks[0], str):
        return failure('invalid_content_snapshot', url=request.url)
    for index in range(1, parts):
        check_execution()
        part = _invoke('detail', url=observed.url, target=asdict(observed), snapshot_id=data.get('snapshot_id'), read_part=index)
        if not part.get('ok'): return {**part, 'url': request.url, 'collected_parts': len(chunks), 'expected_parts': parts}
        row = part['data']
        if (row.get('snapshot_id') != data.get('snapshot_id') or row.get('part') != index
                or row.get('body_sha256') != data.get('body_sha256') or not isinstance(row.get('body_html'), str)):
            return failure('content_snapshot_changed', url=request.url)
        chunks.append(row['body_html'])
    html = ''.join(chunks)
    if hashlib.sha256(html.encode('utf-8')).hexdigest() != data.get('body_sha256'):
        return failure('content_snapshot_changed', url=request.url)
    text = _html_to_markdown(html)
    manifest = data.get('media_manifest') or []
    directory = data.get('answer_directory') or []
    if not isinstance(manifest, list) or not all(isinstance(row, dict) and isinstance(row.get('url'), str) for row in manifest):
        return failure('invalid_adapter_payload', url=request.url)
    if not isinstance(directory, list): return failure('invalid_adapter_payload', url=request.url)
    valid_directory = []
    for row in directory:
        if not isinstance(row, dict): return failure('invalid_adapter_payload', url=request.url)
        try: answer = parse_target(row.get('url', ''))
        except (ValueError, TypeError): return failure('page_identity_changed', url=request.url)
        if target.kind != 'question' or answer.kind != 'answer' or answer.question_id != target.identifier:
            return failure('page_identity_changed', url=request.url)
        if answer.identifier not in {r['identifier'] for r in valid_directory}:
            valid_directory.append({**row, **asdict(answer), 'body_verified': False})
    directory = valid_directory[:answer_limit]
    if not text and not manifest and target.kind != 'question': return failure('page_structure_changed', url=request.url)
    result = {'platform': '知乎', 'status': 'ok', 'url': observed.url, 'requested_url': request.url,
              'title': str(data.get('title') or ''), 'author': str(data.get('author') or ''),
              'published_at': data.get('published_at', ''), 'updated_at': data.get('updated_at', ''),
              'target': asdict(observed), 'content': text, 'content_type': target.kind, 'body_verified': True,
              'body_complete': data.get('body_complete') is True, 'body_source': data.get('body_source', 'visible_dom'),
              'media_manifest': manifest, 'images': [r['url'] for r in manifest if r.get('type') == 'image'],
              'answer_directory': directory, 'directory_coverage': data.get('directory_coverage', 'not_applicable'),
              'comments_collected': False, 'comments_status': 'not_requested',
              'collection_identity': runtime_identity(), 'metadata': response.get('metadata', {})}
    result['completed_stages'] = ['body', 'media']
    result['source_presentation'] = {'platform': '知乎', 'source_site': urlsplit(observed.url).hostname,
                                     'processing_method': result['body_source']}
    if target.kind == 'question':
        links = '\n'.join('- [' + str(row.get('title') or '回答') + '](' + row['url'] + ')'
                          for row in result['answer_directory'] if isinstance(row, dict) and row.get('url'))
        result['content'] = text + ('\n\n## 已加载回答目录\n\n' + links if links else '')
        if not result['content']:
            result['content_observation'] = 'observed_empty_original_text'
    if request.options.get('include_transcript'):
        has_av = target.kind == 'zvideo' or any(row.get('type') in {'audio', 'video'} for row in manifest)
        result['transcript_result'] = {'status':'missing','reason':'unsupported_zhihu_video'} if has_av else {'status': 'not_applicable', 'reason': 'no_video_in_observed_manifest'}
        if has_av: result['status'] = 'partial'
    if not result['body_complete']: result.update(status='partial', body_gap=data.get('body_gap') or 'visible_body_only')
    if target.kind == 'zvideo': result.update(status='partial', transcript_status='unsupported_zhihu_video', body_complete=False)
    if request.options.get('include_comments', request.enable_comment_filtering):
        comments = _invoke('comments', url=observed.url, target=asdict(observed), limit=comment_limit, scope=comment_scope)
        if not comments.get('ok'):
            result.update(status='partial', comments_status=comments.get('failure_type', 'comments_failed'),
                          comments_gap=comments.get('failure_type', 'comments_failed'), comments_failure=comments)
        else:
            c = comments['data']
            try:
                if not same_target(observed, parse_target(c.get('url', ''))): raise ValueError('identity')
            except (ValueError, TypeError): result.update(status='partial', comments_status='page_identity_changed')
            else:
                rows = c.get('comments') or []
                if not isinstance(rows, list) or not all(isinstance(row, dict) and isinstance(row.get('content'), str) for row in rows):
                    result.update(status='partial', comments_status='invalid_adapter_payload')
                    return result
                c['comments'] = rows[:comment_limit]
                satisfied = c.get('request_satisfied') is True
                complete = satisfied or bool(c.get('empty_confirmed'))
                result.update(comments=c.get('comments') or [], comments_collected=bool(c.get('comments') or c.get('empty_confirmed')),
                              comments_empty_confirmed=bool(c.get('empty_confirmed')),
                              comments_complete=complete,
                              comments_exhausted=c.get('exhausted') is True,
                              comments_source=c.get('comment_source', 'visible_dom'),
                              comments_status=c.get('failure_type') or ('empty_confirmed' if c.get('empty_confirmed') else 'bounded_request_satisfied' if satisfied else 'visible_comments_bounded'),
                              comments_coverage={**(c.get('coverage') or {}),
                                                 'scope': (c.get('coverage') or {}).get('scope', 'visible_roots_and_expanded_replies'),
                                                 'limit': c.get('limit'), 'request_satisfied': complete,
                                                 'has_more': c.get('has_more', True), 'exhausted': c.get('exhausted') is True,
                                                 'next': 'explicit_detail_request'},
                              comments_gap='' if complete else c.get('failure_type') or (c.get('coverage') or {}).get('stop_reason') or 'reader_window_not_exhausted',
                              comments_metadata={'gaps':[{'reason':c['failure_type']}] if c.get('failure_type') else [],
                                'actual_order':(c.get('coverage') or {}).get('actual_order','platform_default'),
                                'reader_diagnostic':c.get('comment_diagnostics') or {}, 'transport':comments.get('metadata') or {}})
                if not result['comments_complete']: result['status'] = 'partial'
    return result
