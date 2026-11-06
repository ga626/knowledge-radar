"""微信双来源发现：配置、任务与证据共用；浏览器结果不跨账号缓存。"""
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from collectors.platform import wechat, weread
from runtime.execution_context import check_execution, current_execution, ExecutionStopped
from runtime.search_configuration import configuration_scope
from search_providers.models import WebSearchRequest


def queries(query, account_hint=""):
    terms = " ".join((str(account_hint).strip(), " ".join(str(query).split()))).strip()
    return [f"site:mp.weixin.qq.com/s {terms}", f"site:mp.weixin.qq.com {terms}"] if query.strip() else []


def relevance(item, query):
    text = " ".join(str(item.get(k) or "") for k in ("title", "snippet", "summary")).lower()
    text = re.sub(r"https?://\S+|no information is available for this page\.?", "", text)
    terms = re.findall(r"[a-z][a-z0-9_-]{2,}", query.lower())
    terms += [chunk[i:i+2] for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", query) for i in range(len(chunk)-1)]
    return "metadata_match" if any(t in text for t in terms if t not in {"api","http","https","and","the"}) else "needs_detail_review"


def execute(query, *, cache, record_evidence, limit=10, freshness="", account_hint="", source="auto",
            page=1, timeout_s=40, web_execute=None, weread_execute=None):
    from use_cases.web_search import execute as public_execute
    if source == "sogou":
        return {"platform":"微信", "status":"failed", "items":[],
                "error":{"type":"source_retired", "source":"sogou", "allowed_sources":["auto","web","weread"],
                         "message":"搜狗已退役；请选择公共搜索 web 或微信读书 weread。", "retryable":False}}
    if source not in {"auto","web","weread"} or page != 1 or not 1 <= limit <= 20 or not 1 <= timeout_s <= 120:
        raise ValueError("wechat_search_options_invalid: source=auto|web|weread; page=1; limit=1..20; timeout_s=1..120")
    clean = " ".join(str(query).split())
    if not clean:
        return {"platform":"微信", "items":[], "status":"failed", "error":{"type":"bad_request","message":"query is required"}}
    deadline = time.monotonic()+timeout_s
    public, browser = web_execute or public_execute, weread_execute or weread.search
    plan = queries(clean, account_hint) if source in {"auto","web"} else []
    names = ["web","weread"] if source == "auto" else [source]
    from runtime.tool_trace import current_tool_trace, set_current_tool_trace
    trace = current_tool_trace()
    with configuration_scope() as config:
        execution = current_execution() or {}
        authority = execution.get("KR_RESEARCH_TASK_ID") or execution.get("KR_DURABLE_TASK_ID","interactive")
        def public_batch():
            attempts, candidates = [], []
            for q in plan:
                check_execution()
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    break
                result = public(WebSearchRequest(query=q,limit=min(limit,10),freshness=freshness,
                    options={"timeout_s":remaining,"wechat_contract":wechat.CONTRACT}), cache=cache,record_evidence=lambda *a:None)
                attempts.append({"query":q,"provider":result.get("provider"),"error":result.get("error"),
                                 "attempted_providers":result.get("attempted_providers",[]),"metadata":result.get("metadata",{})})
                candidates.extend(result.get("acquired_items") or result.get("items") or [])
                if sum(wechat.article_url(r.get("url","")) for r in candidates) >= limit:
                    break
            errors = [r["error"] for r in attempts if r.get("error")]
            return {"provider":"public_search","items":candidates,"acquired_items":candidates,
                    "status":"partial" if candidates and errors else "ok" if candidates else "failed" if errors or not attempts else "empty",
                    "error":{"type":"public_sources_failed","sources":errors} if errors else None,
                    "attempted_providers":list(dict.fromkeys(p for r in attempts for p in r["attempted_providers"])),
                    "metadata":{"queries":attempts,"constraint_application":{"freshness":freshness},"deadline_reached":time.monotonic()>=deadline}}
        def run(name):
            previous_trace = current_tool_trace()
            set_current_tool_trace(trace)
            try:
                check_execution()
                result = public_batch() if name == "web" else browser(" ".join((account_hint,clean)).strip(),
                    limit=limit,freshness=freshness,timeout_s=max(.1,deadline-time.monotonic()))
                check_execution()
                return result
            except ExecutionStopped:
                raise
            except Exception as exc:
                return {"items":[],"status":"failed","error":{"type":"source_execution_failed","exception_type":type(exc).__name__,"retryable":False}}
            finally:
                set_current_tool_trace(previous_trace)
        # 两个独立来源同时取得预算。等待已发送的有界只读调用清理自有页，不遗留无主线程。
        with ThreadPoolExecutor(max_workers=len(names),thread_name_prefix="wechat-discovery") as pool:
            futures = {name:pool.submit(copy_context().run,run,name) for name in names}
            results = {name:futures[name].result() for name in names}
        outcomes, seen = [], {}
        for name, result in results.items():
            outcomes.append({"source":name,"provider":result.get("provider",name),"status":result.get("status"),
                             "error":result.get("error"),"metadata":result.get("metadata",{}),"attempted_providers":result.get("attempted_providers",[])})
            for item in result.get("acquired_items") or result.get("items") or []:
                url = str(item.get("url") or "")
                resolved = wechat.article_url(url)
                if not resolved and not (name == "weread" and item.get("candidate_ref")):
                    continue
                key = wechat.article_identity(url) if resolved else "weread:"+str(item["candidate_ref"])
                provider = item.get("source_provider") or ("web" if name == "web" else "weread_sosou")
                if key in seen:
                    seen[key]["discovery_sources"] = list(dict.fromkeys([*seen[key]["discovery_sources"],provider]))
                    continue
                seen[key] = {**item,"platform":"微信","source_type":"wechat_public_account_article",
                    "source_presentation":wechat.PRESENTATION,"discovery_query":clean,"discovery_sources":[provider],
                    "discovery_channel":"browser" if name == "weread" else "public_index",
                    "relevance_status":relevance(item,clean),"verification_required":True,
                    "detail_tool_supported":resolved,"detail_supported":resolved,
                    "recommended_extract_tool":"get_content_detail" if resolved else None,"detail_tool":"get_content_detail" if resolved else None,
                    "detail_mode":"wechat_readonly","body_fetched":False,"resolution_status":"resolved" if resolved else "article_url_unresolved"}
        all_items = list(seen.values())
        shown = sorted(all_items,key=lambda r:r["relevance_status"]!="metadata_match")[:limit]
        incomplete = [r for r in outcomes if r.get("error") or r.get("status") in {"partial","failed","waiting_for_user"}]
        status = "partial" if all_items and incomplete else "ok" if all_items else "failed" if incomplete else "empty"
        result = {"query":clean,"platform":"微信","provider":"wechat_discovery","status":status,
            "items":shown,"acquired_items":all_items,"total":len(shown),"fallback_used":False,
            "attempted_providers":list(dict.fromkeys(p for r in outcomes for p in (r.get("attempted_providers") or [r["provider"]]))),
            "metadata":{"actual_mcp_tool":"search_wechat_articles","integration_level":"wechat_readonly_v2",
                "detail_tool_supported":True,"recommended_detail_tool":"get_content_detail","queries":plan,
                "source_outcomes":outcomes,"configuration_generation":config.generation,"authority_scope":authority,
                "account_scope":{"mode":"hint","value":account_hint,"strict":False,"relaxed":False},
                "unverified_candidate_count":sum(r["relevance_status"]!="metadata_match" for r in all_items),
                "article_bodies_fetched":False,"coverage":"observed_queries_not_full_wechat",
                "relevance_judgement_by":"Codex", "relevance_boundary":"KR只做可解释的基础过滤；是否相关及证据充分性由Codex判断。",
                "deadline_reached":time.monotonic()>=deadline,"deadline_policy":"cooperative_bounded_inflight_cleanup",
                "continuations":{r["source"]:r["metadata"].get("continuation") for r in outcomes}}}
        if not all_items and incomplete:
            result["error"] = {"type":"search_sources_failed","retryable":False,"sources":incomplete}
    record_evidence("微信",clean,result)
    return result
