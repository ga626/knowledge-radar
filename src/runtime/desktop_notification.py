"""Windows notification delivery with a receipt, independent of an open website."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
import uuid
from .paths import runtime_state_dir

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
$krIcon.Add_BalloonTipClicked({ Start-Process -FilePath 'http://127.0.0.1:18882/#services' })
$krIcon.Add_Click({ Start-Process -FilePath 'http://127.0.0.1:18882/#services' })
$krIcon.ShowBalloonTip(30000)
$krReceipt = @{status='display_requested'; user_observed=$false; helper_pid=$PID} | ConvertTo-Json
[IO.File]::WriteAllText($env:KR_ACCOUNT_NOTICE_RECEIPT, $krReceipt, [Text.UTF8Encoding]::new($false))
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


def notify_account_failure(manual_action: dict) -> dict:
    if os.environ.get("KR_CONSOLE_READ_ONLY") == "1" or os.environ.get("KR_DESKTOP_NOTIFICATIONS_DISABLED") == "1":
        return {"status": "disabled"}
    executable = shutil.which("powershell.exe") if os.name == "nt" else None
    if not executable:
        return {"status": "unavailable", "reason": "windows_notification_host_missing"}
    label = str(manual_action.get("display_label") or manual_action.get("profile_id") or "账号")
    payload = {"title": f"KnowledgeRadar：{label}需要处理"[:63],
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
        process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"status": "dispatched", "pid": process.pid, "delivery_confirmed": False,
                "target": "services", "helper_receipt": str(receipt)}
    except OSError as error:
        return {"status": "failed", "reason": type(error).__name__}
