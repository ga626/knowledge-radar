"""V2EX HTTP讨论源：节点发现、帖子正文和回复；没有全站关键词API。"""
from __future__ import annotations
import os
import re
import json
import time
import hashlib
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urljoin
from typing import Dict, List
import httpx
from bs4 import BeautifulSoup
from runtime.proxy_config import get_httpx_proxy
from runtime.paths import runtime_state_dir
from runtime.file_write_lock import file_write_lock
BASE='https://www.v2ex.com'
HEADERS={'User-Agent':'KnowledgeRadar/1.0','Accept':'application/json,text/html;q=0.9'}


def _client():
    return httpx.Client(timeout=httpx.Timeout(12,connect=5),proxy=get_httpx_proxy(),headers=HEADERS,follow_redirects=False)


def _failure(kind, status=None):
    return {'platform':'V2EX','status':'failed','items':[],'failure_type':kind,
        'error':{'type':kind,'failure_type':kind,'http_status':status},'manual_action_required':False,
        'retryable':False,'metadata':{'scope':'jobs_node','open_index_eligible':kind in {'http_error','access_denied','schema_changed','transport_error'}}}


class RemoteQuota(ValueError):
    def __init__(self, retry_after):
        self.retry_after = retry_after


def _quota_path(token=''):
    identity = hashlib.sha256(token.encode()).hexdigest()[:16] if token else 'public'
    return runtime_state_dir() / 'http_quotas' / ('v2ex-' + identity + '.json')


def _rate_headers(response):
    headers=response.headers
    now=time.time()
    retry=headers.get('retry-after','')
    try: until=now+max(0,float(retry)) if retry else 0
    except ValueError:
        try: until=parsedate_to_datetime(retry).timestamp()
        except (ValueError,TypeError): until=0
    remaining=headers.get('x-rate-limit-remaining',headers.get('x-ratelimit-remaining'))
    reset=headers.get('x-rate-limit-reset',headers.get('x-ratelimit-reset'))
    try: reset_at=float(reset or 0)
    except ValueError: reset_at=0
    if response.status_code==429 or remaining=='0':until=max(until,reset_at,now+60 if not until and not reset_at else 0)
    return {'remaining':remaining,'reset_at':reset_at,'blocked_until':until,'observed_at':now}


def _get(client, path, *, token=''):
    headers={'Authorization':'Bearer '+token} if token else {}
    state_path=_quota_path(token)
    with file_write_lock(state_path):
        try: quota=json.loads(state_path.read_text(encoding='utf-8'))
        except FileNotFoundError: quota={}
        # Corrupt rate state is not permission to issue more requests.
        remaining=float(quota.get('blocked_until',0))-time.time()
        if remaining>0:raise RemoteQuota(round(remaining)+1)
        response=client.get(BASE+path,headers=headers)
        quota=_rate_headers(response)
        from runtime.state_file import write_state
        write_state(state_path,quota)
    response.raise_for_status()
    if response.is_redirect:raise ValueError('unexpected_redirect')
    return response


def _error(exc):
    if isinstance(exc,RemoteQuota):
        return {**_failure('rate_limited'), 'retry_after_s':exc.retry_after,'request_executed':False}
    if isinstance(exc,httpx.HTTPStatusError):
        status=exc.response.status_code
        result=_failure({401:'authorization_required',403:'access_denied',404:'not_found',429:'rate_limited'}.get(status,'http_error'),status)
        if status==429:result['retry_after_s']=max(1,round(_rate_headers(exc.response)['blocked_until']-time.time()))
        return result
    return _failure('transport_error' if isinstance(exc,httpx.RequestError) else 'schema_changed')

def _city_matches(text: str, city: str = "") -> bool:
    city_norm = (city or "").strip().removesuffix("市")
    if not city_norm:
        return True
    haystack = str(text or "")
    remote_markers = ("远程", "remote", "Remote", "REMOTE")
    return city_norm in haystack or any(marker in haystack for marker in remote_markers)


def _parse_v2ex_jobs_html(html: str, base_url: str, *, keyword: str = "", city: str = "", limit: int = 20) -> List[Dict]:
    soup = BeautifulSoup(html, "lxml")
    items: List[Dict] = []
    kw = (keyword or "").lower()
    root = soup.select_one('#TopicsNode') or soup.select_one('#Main') or soup
    for title_node in root.select('span.item_title a'):
        cell = title_node.find_parent('div', class_='cell')
        if cell is None: continue
        title = title_node.get_text(" ", strip=True)
        href = title_node.get("href") or ""
        target = urlsplit(urljoin(base_url, href))
        if target.hostname not in {'www.v2ex.com','v2ex.com','hk.v2ex.com'} or not re.fullmatch(r'/t/\d+/?',target.path):
            continue
        url = BASE + target.path.rstrip('/')
        content = cell.get_text(" ", strip=True)
        if kw and kw not in title.lower() and kw not in content.lower():
            continue
        if not _city_matches(f"{title}\n{content}", city):
            continue
        author_node = cell.select_one("strong a[href^='/member/']")
        reply_node = cell.select_one("a.count_livid")
        items.append({
            "title": title,
            "desc": content[:200],
            "url": url,
            "author": author_node.get_text(strip=True) if author_node else "",
            "replies": int(reply_node.get_text(strip=True)) if reply_node and reply_node.get_text(strip=True).isdigit() else 0,
            "platform": "V2EX",
            "source": "web_fallback",
        })
        if len(items) >= limit:
            break
    return items




def _topic_item(topic):
    if not isinstance(topic,dict) or not topic.get('id') or not topic.get('title'):raise ValueError('topic_schema')
    return {'id':str(topic['id']),'title':str(topic['title']),'desc':str(topic.get('content') or '')[:500],
        'url':BASE+'/t/'+str(topic['id']),'author':(topic.get('member') or {}).get('username',''),
        'replies':topic.get('replies',0),'created':topic.get('created'),'platform':'V2EX',
        'content_type':'community_discussion_candidate','source':'official_api','job_verified':False}


def discover(keyword='',limit=10,city='',*,page=1,pages=2,offset=0):
    token=os.environ.get('KR_V2EX_TOKEN','')
    page=max(1,int(page));offset=max(0,int(offset));limit=max(1,min(100,int(limit)))
    items=[];seen=set();scanned=0;has_more=False;next_page=None;next_offset=0
    try:
        with _client() as client:
            for number in range(max(1,page),max(1,page)+max(1,min(2,pages))):
                if token:
                    body=_get(client,f'/api/v2/nodes/jobs/topics?p={number}',token=token).json()
                    if not isinstance(body,dict) or body.get('success') is not True or not isinstance(body.get('result'),list):raise ValueError('node_schema')
                    rows=[_topic_item(t) for t in body['result']]
                    has_more=len(rows)>=20
                else:
                    response=_get(client,f'/go/jobs?p={number}')
                    soup=BeautifulSoup(response.text,'lxml')
                    if not soup.select_one('#TopicsNode,.cell.item,.node-header,.node_header'):
                        if soup.select_one('input[name="once"],form[action="/signin"]'):return _failure('authorization_required')
                        raise ValueError('node_schema')
                    rows=_parse_v2ex_jobs_html(response.text,BASE+'/go/jobs',limit=100)
                    for row in rows:row.update(content_type='community_discussion_candidate',job_verified=False)
                    has_more=bool(soup.select_one('a.page_normal[href*="p='+str(number+1)+'"],a[rel="next"]'))
                scanned+=1
                next_page=number+1 if has_more else None
                next_offset=0
                start_offset=offset if number==page else 0
                for index,row in enumerate(rows):
                    if index<start_offset:continue
                    text=str(row.get('title',''))+' '+str(row.get('desc',''))
                    if keyword and keyword.casefold() not in text.casefold():continue
                    if not _city_matches(text,city) or row['url'] in seen:continue
                    seen.add(row['url']);items.append(row)
                    if len(items)>=limit:
                        next_page=number if index+1<len(rows) else number+1 if has_more else None
                        next_offset=index+1 if index+1<len(rows) else 0
                        break
                if len(items)>=limit or not has_more:break
                next_page=number+1
    except (httpx.HTTPError,ValueError,TypeError,KeyError) as exc:
        error=_error(exc)
        if not items:return error
        return {'platform':'V2EX','status':'partial','items':items[:limit],'metadata':{'partial_failure':error,'pagination':{'pages_scanned':scanned,'complete':False}}}
    return {'platform':'V2EX','status':'ok' if items else 'empty','items':items[:limit],'total':min(len(items),limit),
        'empty_reason':'no_match_in_scanned_node_pages' if not items else '',
        'metadata':{'scope':'jobs_node','keyword_scope':'visible_title_excerpt' if not token else 'node_title_content',
            'pagination':{'first_page':page,'pages_scanned':scanned,'next_page':next_page if len(items)>=limit else page+scanned if has_more else None,
                'next_offset':next_offset,'complete':not has_more and next_page is None,
                'continuation':{'page':next_page,'offset':next_offset} if next_page else None,
                'boundary':'mutable_node_pages_not_a_snapshot'},
            'open_index_eligible':not bool(items),'city_filter':{'applied':bool(city),'city':city}}}


def legacy_search_v2ex(keyword='',limit=10,city='',*,page=1,pages=2,offset=0):
    from runtime.recruitment_governance import execute_operation
    return execute_operation('v2ex','search',lambda:discover(keyword,limit,city,page=page,pages=pages,offset=offset),keyword=keyword,city=city,resource='http')


def _public_detail(html,topic_id,page=1):
    soup=BeautifulSoup(html,'lxml')
    body=soup.select_one('.topic_content')
    title=soup.select_one('h1')
    if not body or not title:
        if soup.select_one('form[action="/signin"]'):return _failure('authorization_required')
        return _failure('schema_changed')
    replies=[]
    for el in soup.select('[id^="r_"]'):
        content=el.select_one('.reply_content');author=el.select_one('a[href^="/member/"]')
        if content:replies.append({'id':el.get('id',''),'content':content.get_text('\n',strip=True),'author':author.get_text(strip=True) if author else ''})
    next_page=soup.select_one('a[rel="next"]')
    pages=[int(a.get_text()) for a in soup.select('a.page_normal') if a.get_text().isdigit()]
    return {'title':title.get_text(strip=True),'content':body.get_text('\n',strip=True),'comments':replies,
        'comments_complete':not next_page and max(pages,default=page)<=page,
        'next_page':page+1 if next_page or max(pages,default=page)>page else None}


def detail(url,*,include_comments=False,comment_limit=50,comment_page=1,comment_offset=0):
    comment_page=max(1,int(comment_page));comment_offset=max(0,int(comment_offset))
    parsed=urlsplit(url)
    if parsed.scheme!='https' or parsed.hostname not in {'v2ex.com','www.v2ex.com','hk.v2ex.com'} or not re.fullmatch(r'/t/\d+/?',parsed.path):
        return {**_failure('unsupported_url'),'request_executed':False}
    identifier=parsed.path.strip('/').split('/')[1];canonical=BASE+'/t/'+identifier
    def read():
        token=os.environ.get('KR_V2EX_TOKEN','')
        try:
            with _client() as client:
                if token:
                    body=_get(client,'/api/v2/topics/'+identifier,token=token).json()
                    if not isinstance(body,dict) or body.get('success') is not True or not isinstance(body.get('result'),dict):raise ValueError('detail_schema')
                    topic=body['result']
                    result={'title':str(topic.get('title') or ''),'content':str(topic.get('content') or ''),'author':(topic.get('member') or {}).get('username','')}
                    if not result['title'] or not result['content']:raise ValueError('empty_topic')
                    if include_comments:
                        collected=[];complete=False;next_page=comment_page;failure=None
                        expected=int(topic['replies']) if topic.get('replies') is not None else None
                        for page in range(comment_page,comment_page+2):
                            try:
                                payload=_get(client,f'/api/v2/topics/{identifier}/replies?p={page}',token=token).json()
                                if not isinstance(payload,dict) or payload.get('success') is not True or not isinstance(payload.get('result'),list):raise ValueError('replies_schema')
                                rows=payload['result'];start=comment_offset if page==comment_page else 0
                                collected.extend({'id':r.get('id'),'content':r.get('content',''),'author':(r.get('member') or {}).get('username','')} for r in rows[start:])
                                next_page=page+1
                                if not rows or (comment_page==1 and expected is not None and len(collected)+comment_offset>=expected):complete=True;break
                                if len(collected)>=comment_limit:break
                            except (httpx.HTTPError,ValueError,TypeError) as exc:
                                next_page=page;failure=_error(exc);break
                        result.update(comments=collected[:comment_limit],comments_complete=complete and len(collected)<=comment_limit,
                            comments_status='request_failed' if failure else 'collected',comments_failure=failure,
                            comments_next_page=next_page if not complete or failure else None,
                            comments_truncated=len(collected)>comment_limit,comments_total=expected)
                        if len(collected)>comment_limit:
                            result.update(comments_next_page=page,comments_next_offset=start+len(rows)-(len(collected)-comment_limit))
                        else:result['comments_next_offset']=0
                else:
                    result=_public_detail(_get(client,'/t/'+identifier+('?p='+str(comment_page) if include_comments else '')).text,identifier,comment_page)
                    if result.get('error'):return result
                    if not include_comments:
                        for key in ('comments','comments_complete','next_page'):result.pop(key,None)
                    else:
                        rows=result.get('comments',[]);result['comments']=rows[comment_offset:comment_offset+comment_limit]
                        result['comments_complete']=result['comments_complete'] and len(rows)<=comment_offset+comment_limit
                        result['comments_next_page']=comment_page if len(rows)>comment_offset+comment_limit else result.pop('next_page',None)
                        result['comments_next_offset']=comment_offset+comment_limit if len(rows)>comment_offset+comment_limit else 0
                return {**result,'platform':'V2EX','url':canonical,'status':'ok','content_type':'community_discussion',
                    'body_verified':True,'job_verified':False,'comments_collected':bool(result.get('comments')),
                    'metadata':{'transport':'http','authentication':'personal_token' if token else 'public','pagination_bounded':True}}
        except (httpx.HTTPError,ValueError,TypeError,KeyError) as exc:return {**_error(exc),'url':canonical}
    from runtime.recruitment_governance import execute_operation
    return execute_operation('v2ex','detail',read,resource='http')
