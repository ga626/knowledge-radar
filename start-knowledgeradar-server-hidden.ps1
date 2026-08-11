$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$logDir = Join-Path $root "runtime\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

if (-not $env:KR_LOG_DIR) { $env:KR_LOG_DIR = $logDir }
if (-not $env:KR_STATE_DIR) { $env:KR_STATE_DIR = Join-Path $root "runtime" }
if (-not $env:KR_MCP_TRANSPORT) { $env:KR_MCP_TRANSPORT = "streamable-http" }
if (-not $env:KR_MCP_HOST) { $env:KR_MCP_HOST = "127.0.0.1" }
if (-not $env:KR_MCP_PORT) { $env:KR_MCP_PORT = "18765" }

$bundledPython = Join-Path $root ".python312\python.exe"
$python = if (Test-Path -LiteralPath $bundledPython) { $bundledPython } else { "python" }
$server = Join-Path $root "src\server.py"
$stdout = Join-Path $logDir "knowledgeradar-hidden.out.log"
$stderr = Join-Path $logDir "knowledgeradar-hidden.err.log"

Start-Process -FilePath $python `
  -ArgumentList @("-X", "utf8", $server) `
  -WorkingDirectory $root `
  -WindowStyle Hidden `
  -RedirectStandardOutput $stdout `
  -RedirectStandardError $stderr

Write-Host "KnowledgeRadar MCP server started in hidden mode."
Write-Host "Logs: $logDir"
