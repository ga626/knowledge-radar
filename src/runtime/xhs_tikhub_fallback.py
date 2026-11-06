"""付费补缺：固定App V2，单账本金额/次数授权；未知结果不重发。"""
from __future__ import annotations
import hashlib
import json
import os
import re
import uuid
from urllib.parse import urlsplit
import httpx
from .xhs_tikhub_usage import claim_paid_send, settle_paid_send, tikhub_usage_summary
from .xhs_tikhub_adapter import normalize_tikhub_xhs_detail_response, normalize_tikhub_xhs_search_response, normalize_tikhub_xhs_comments

TIKHUB_XHS_APP_V2_SEARCH_ENDPOINT = "/api/v1/xiaohongshu/app_v2/search_notes"
TIKHUB_XHS_APP_V2_DETAIL_ENDPOINT = "/api/v1/xiaohongshu/app_v2/get_image_note_detail"
ENDPOINTS = {"search":"search_notes","image_detail":"get_image_note_detail",
    "video_detail":"get_video_note_detail","comments":"get_note_comments","replies":"get_note_sub_comments"}
TRIGGERS = {"AUTH_REQUIRED","SECURITY_BLOCK","CAPTCHA_REQUIRED","RATE_LIMITED",
    "anti_bot_verification","signed_link_expired_search_again","empty_detail","dead_link",
    "empty_results","EMPTY_RESULT","COMMENTS_NOT_LOADED","browser_disconnected","opencli_caller_timeout"}
BREAK_GLASS_DRY_RUN_ENV = "KR_XHS_TIKHUB_BREAK_GLASS_DRY_RUN"


def _api_key():
    return os.environ.get("TIKHUB_API_KEY","").strip()


def _base_url():
    return os.environ.get("TIKHUB_BASE_URL","").strip() or "https://api.tikhub.io"


def _blocked(reason, **extra):
    return {"ok":False,"status":"blocked","reason_code":reason,"api_call_count":0,**extra}


def _send(operation, params, task_id):
    if operation not in ENDPOINTS:
        return _blocked("TIKHUB_UNSUPPORTED_OPERATION")
    base = _base_url()
    parsed = urlsplit(base)
    if parsed.scheme != "https" or parsed.hostname not in {"api.tikhub.io","api.tikhub.dev"} or parsed.username or parsed.password:
        return _blocked("TIKHUB_ENDPOINT_REJECTED")
    if not _api_key():
        return _blocked("TIKHUB_API_KEY_MISSING")
    reservation_id = hashlib.sha256(json.dumps([task_id,operation,params],sort_keys=True).encode()).hexdigest()
    claim = claim_paid_send(operation,reservation_id=reservation_id,task_id=task_id)
    if not claim.get("send"):
        cached = claim.get("cached_result")
        return {**cached,"api_call_count":0,"reused_result":True} if cached else _blocked(claim["reason_code"])
    endpoint = "/api/v1/xiaohongshu/app_v2/"+ENDPOINTS[operation]
    try:
        with httpx.Client(timeout=20,follow_redirects=False) as client:
            response = client.get(base.rstrip("/")+endpoint, params=params,
                headers={"Authorization":"Bearer "+_api_key(),"Accept":"application/json"})
        payload = response.json()
        api_code = payload.get("code") if isinstance(payload,dict) else None
        if response.status_code != 200 or api_code not in {None,0,200,"0","200"}:
            result = {"ok":False,"status":"failed","reason_code":"TIKHUB_API_FAILED",
                "http_status":response.status_code,"api_call_count":1}
        else:
            if operation == "search":
                data = normalize_tikhub_xhs_search_response(payload,keyword=params["keyword"],limit=20).to_mcp_dict()["items"]
            elif operation in {"comments","replies"}:
                data = normalize_tikhub_xhs_comments(payload,is_reply=operation=="replies")
            else:
                data = normalize_tikhub_xhs_detail_response(payload,note_id=params["note_id"])
            valid = bool(data) or (operation in {"comments", "replies"} and isinstance(data, list))
            result = {"ok":valid,"status":"ok" if valid else "empty","data":data,
                "api_call_count":1,"reason_code":"OK" if data else "TIKHUB_EMPTY"}
    except (ValueError, TypeError, KeyError):
        result = {"ok":False,"status":"failed","reason_code":"TIKHUB_RESPONSE_UNRECOGNIZED","api_call_count":1}
    except Exception:
        # Do not expose response/request exception strings containing URLs or bearer headers.
        result = {"ok":False,"status":"failed","reason_code":"TIKHUB_OUTCOME_UNKNOWN","api_call_count":1}
    result.update(reservation_id=reservation_id,reserved_upper_cost_usd=claim["reserved_usd"],provider="tikhub")
    settle_paid_send(reservation_id,result)
    return result


def execute_paid_operation(operation, payload, *, task_id="", trigger=""):
    if trigger not in TRIGGERS:
        return _blocked("TIKHUB_PRIMARY_FAILURE_NOT_ELIGIBLE")
    if not tikhub_usage_summary().get("enabled"):
        return _blocked("TIKHUB_FALLBACK_DISABLED")
    if os.environ.get(BREAK_GLASS_DRY_RUN_ENV,"0").lower() not in {"0","false","no","off"}:
        return _blocked("TIKHUB_DRY_RUN")
    if not task_id:
        return _blocked("TIKHUB_TASK_ID_REQUIRED")
    if operation == "search":
        return _send("search",{"keyword":payload["keyword"],"page":1,"sort_type":"general",
            "note_type": {"image":"普通笔记","video":"视频笔记"}.get(payload.get("search_type"),"不限")},task_id)
    note_id = str(payload.get("note_id") or "")
    if not re.fullmatch("[a-f0-9]{24}",note_id):
        return _blocked("TIKHUB_NOTE_ID_REQUIRED")
    if operation in {"comments","replies"}:
        params={"note_id":note_id,"cursor":payload.get("cursor",""),"index":1 if operation=="replies" else 0}
        if operation=="replies":
            if not payload.get("comment_id"):
                return _blocked("TIKHUB_COMMENT_ID_REQUIRED")
            params["comment_id"]=payload["comment_id"]
        return _send(operation,params,task_id)
    data = dict(payload.get("partial") or {})
    calls = []
    if "media" not in data.get("completed_stages",[]):
        note_type = payload.get("note_type") or data.get("note_type")
        # No paid guessing between two detail endpoints.
        if note_type not in {"image","video"}:
            return _blocked("TIKHUB_NOTE_TYPE_REQUIRED")
        body = _send(note_type+"_detail",{"note_id":note_id},task_id)
        calls.append(body)
        if not body.get("ok"):
            return body
        data.update(body["data"])
        data["completed_stages"]=["body","media"]
    if payload.get("include_comments") and "comments" not in data.get("completed_stages",[]):
        comments=_send("comments",{"note_id":note_id,"cursor":"","index":0},task_id)
        calls.append(comments)
        if not comments.get("ok"):
            return {**comments,"data":data}
        data["comments"]=comments["data"]
        data["comments_coverage"]="first_api_page_with_embedded_replies"
        data.setdefault("completed_stages",[]).append("comments")
    return {"ok":True,"status":"ok","data":data,"provider":"tikhub",
        "api_call_count":sum(r["api_call_count"] for r in calls)}
