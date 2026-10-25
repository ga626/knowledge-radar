"""Windows notification delivery with a receipt, independent of an open website."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
import threading
import uuid
from urllib.parse import quote
from .paths import runtime_state_dir
from .process import silent_subprocess_popen

SCRIPT = r'''
$ErrorActionPreference = 'Stop'
try {
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$krNotice = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:KR_ACCOUNT_NOTICE)) | ConvertFrom-Json
$krIcon = New-Object System.Windows.Forms.NotifyIcon
$krIcon.Icon = [System.Drawing.SystemIcons]::Warning
$krIcon.Text = 'KnowledgeRadar'
$krIcon.Visible = $true
$krIcon.BalloonTipTitle = [string]$krNotice.title
$krIcon.BalloonTipText = [string]$krNotice.message
function Write-KrNoticeReceipt([string]$krStatus) {
  $krReceipt = @{status=$krStatus; user_observed=$false; helper_pid=$PID; deep_link_target=[string]$krNotice.url; observed_at=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json
  [IO.File]::WriteAllText($env:KR_ACCOUNT_NOTICE_RECEIPT, $krReceipt, [Text.UTF8Encoding]::new($false))
}
$krIcon.Add_BalloonTipShown({ Write-KrNoticeReceipt 'shown' })
$krIcon.Add_BalloonTipClicked({ Start-Process -FilePath ([string]$krNotice.url); Write-KrNoticeReceipt 'activated' })
$krIcon.Add_Click({ Start-Process -FilePath ([string]$krNotice.url); Write-KrNoticeReceipt 'activated' })
Write-KrNoticeReceipt 'display_requested'
$krIcon.ShowBalloonTip(30000)
$krTimer = New-Object System.Windows.Forms.Timer
$krTimer.Interval = 60000
$krTimer.Add_Tick({ $krTimer.Stop(); $krIcon.Dispose(); [System.Windows.Forms.Application]::Exit() })
$krTimer.Start()
[System.Windows.Forms.Application]::Run()
} catch {
  $krReceipt = @{status='failed'; reason=$_.Exception.GetType().Name; user_observed=$false} | ConvertTo-Json
  [IO.File]::WriteAllText($env:KR_ACCOUNT_NOTICE_RECEIPT, $krReceipt, [Text.UTF8Encoding]::new($false))
  exit 1
}
'''


def _watch_delivery(process, receipt, store, event_id, result):
    """有限等待helper回执；不把启动成功当作通知已送达。"""
    deadline = time.monotonic() + 65
    last_status = ""
    while time.monotonic() < deadline:
        if store.notification_is_resolved(event_id):
            if process.poll() is None:
                process.terminate()  # 仅本次创建的通知helper；不涉及浏览器/宿主。
            return
        try:
            value = json.loads(receipt.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            value = {}
        status = value.get('status')
        if status in {'failed', 'display_requested', 'shown', 'activated'} and status != last_status:
            store.delivery(event_id, 'dispatched' if status == 'display_requested' else status,
                           {**result, 'helper_result': value, 'delivery_confirmed': status in {'shown', 'activated'}, 'user_observed': False})
            last_status = status
            if status == 'failed':
                return
        if process.poll() is not None:
            break
        time.sleep(0.5)
    if last_status not in {"shown", "activated", "failed"}:
        store.delivery(event_id, 'failed', {**result, 'reason': 'helper_receipt_unavailable', 'delivery_confirmed': False})


def drain_notification_outbox():
    """重放事务已提交但尚未投递的故障；不打开浏览器或制造失效。"""
    if os.environ.get("KR_CONSOLE_READ_ONLY") == "1" or os.environ.get("KR_DESKTOP_NOTIFICATIONS_DISABLED") == "1":
        return []
    from .account_observations import AccountObservationStore
    from .account_alerts import account_identity_fields
    from .profile_registry import profile_registry_internal
    registry = profile_registry_internal()
    profiles = (registry.get("raw") or {}).get("profiles") or registry.get("profiles") or []
    registered = {row["profile_id"]: row for row in profiles if row.get("profile_id")}
    results = []
    for observation in AccountObservationStore().pending_notifications():
        profile = registered.get(observation["profile_id"])
        if not profile:
            continue
        fields = account_identity_fields(profile)
        label = fields["display_label"]
        results.append(notify_account_failure({**fields, "event_id": observation["event_id"], "profile_id": observation["profile_id"],
                        "human_message": f"{profile.get('platform', '')}：{label}（{fields['slot_label']}，登记数字 {fields['account_number'] or '未登记'}）需要恢复，请点击定位账号卡。"}))
    return results


def start_notification_outbox_worker():
    stop = threading.Event()
    def pump():
        while not stop.is_set():
            try:
                drain_notification_outbox()
            except Exception:
                # 保留pending，下一轮重试；不把重放故障改写为账号失败。
                pass
            stop.wait(30)
    thread = threading.Thread(target=pump, name="kr-notification-outbox", daemon=True)
    thread.start()
    return stop


def notify_account_failure(manual_action: dict) -> dict:
    if os.environ.get("KR_CONSOLE_READ_ONLY") == "1" or os.environ.get("KR_DESKTOP_NOTIFICATIONS_DISABLED") == "1":
        return {"status": "disabled"}
    executable = shutil.which("powershell.exe") if os.name == "nt" else None
    if not executable:
        return {"status": "unavailable", "reason": "windows_notification_host_missing"}
    from .account_observations import AccountObservationStore
    store = AccountObservationStore()
    event_id = str(manual_action.get("event_id") or uuid.uuid4().hex)
    if not store.claim_notification(event_id):
        return {"status": "deduplicated", "event_id": event_id}
    label = str(manual_action.get("display_label") or manual_action.get("profile_id") or "账号")
    payload = {"title": f"KnowledgeRadar：{label}需要处理"[:63],
               "url": "http://127.0.0.1:18882/#accounts?profile=" + quote(str(manual_action.get("profile_id") or ""), safe=""),
               "message": str(manual_action.get("human_message") or "请打开服务页恢复账号。")[:240]}
    env = dict(os.environ)
    receipt = runtime_state_dir() / "account-notifications" / (uuid.uuid4().hex + ".json")
    receipt.parent.mkdir(parents=True, exist_ok=True)
    try:
        previous = sorted((path for path in receipt.parent.glob("*.json") if len(path.stem) == 32 and all(char in "0123456789abcdef" for char in path.stem)), key=lambda path: path.stat().st_mtime, reverse=True)
        now = time.time()
        for index, path in enumerate(previous):
            age = now - path.stat().st_mtime
            if age > 120 and (index >= 200 or age > 7 * 86400):
                path.unlink(missing_ok=True)
    except OSError:
        pass
    env["KR_ACCOUNT_NOTICE_RECEIPT"] = str(receipt)
    env["KR_ACCOUNT_NOTICE"] = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
    command = [executable, "-NoProfile", "-NonInteractive", "-EncodedCommand", base64.b64encode(SCRIPT.encode("utf-16-le")).decode("ascii")]
    try:
        process = silent_subprocess_popen(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        result = {"status": "dispatched", "event_id": event_id, "pid": process.pid, "delivery_confirmed": False,
                  "target": payload["url"], "helper_receipt": str(receipt)}
        store.delivery(event_id, "dispatched", result)
        threading.Thread(target=_watch_delivery, args=(process, receipt, store, event_id, result), daemon=True).start()
        return result
    except OSError as error:
        store.delivery(event_id, "failed", {"reason": type(error).__name__})
        return {"status": "failed", "reason": type(error).__name__}
