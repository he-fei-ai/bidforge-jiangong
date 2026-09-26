
# ============================================================================
#  start_backend.ps1 - start the FastAPI backend in SINGLE-PROCESS mode and
#  self-verify with a timed /api/v1/health probe.
#  ============================================================================
#  WHY SINGLE-PROCESS (no --reload)
#  `uvicorn --reload` runs a reloader PARENT that creates the :8000 listen
#  socket and a worker CHILD that inherits a handle to it. On Windows this
#  causes two problems we hit repeatedly:
#    * parent/child accept() competition on the same socket,
#    * when the parent dies the orphan worker keeps the port LISTENing while
#      netstat still names the (dead) parent PID as owner -> "port in use by a
#      process that doesn't exist" and reload storms.
#  Starting WITHOUT --reload gives exactly ONE python process that owns the
#  socket. Reload-based dev is still available via start_all.bat; this script
#  is the clean single-process path.
#
#  WHAT IT DOES
#    1. free port 8000 (kill_port.ps1) + sweep leftover zombie pytest,
#    2. locate an interpreter that can import the app,
#    3. start `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000`
#       as ONE detached process (no --reload),
#    4. print PID / listening port / health URL / clickable frontend URL,
#    5. poll health, print measured latency; exit 1 on failure.
#
#  Exit code: 0 = backend up & healthy, 1 = startup/health check failed.
#  NOTE: keep output ASCII-only (see kill_port.ps1).
# ============================================================================

param(
    [int] $Port = 8000,
    [int] $HealthTimeoutSec = 60,
    [string] $FrontendUrl = "http://localhost:5175",
    [switch] $SkipZombieSweep
)

$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
$backendDir = Join-Path $root 'backend'

function Die($msg) {
    [Console]::Error.WriteLine("[start_backend] FAILED: $msg")
    exit 1
}

# ---- 1. free the port + clean zombie pytest -------------------------------
Write-Output "[start_backend] step 1/5: releasing port $Port and cleaning zombie pytest..."
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'kill_port.ps1') -Ports $Port -WaitSec 8 -Quiet
if (-not $SkipZombieSweep) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'kill_zombie_pytest.ps1') -Quiet
}

# still busy? hard fail with the current owner spelled out.
$owner = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
         Select-Object -First 1
if ($owner) { Die "port $Port is still held by PID $($owner.OwningProcess) after cleanup." }

# ---- 2. locate a usable interpreter ---------------------------------------
Write-Output "[start_backend] step 2/5: locating Python interpreter..."
$importCheck = 'import fastapi,uvicorn,aiosqlite,pydantic'
$PY = $null
$candidates = @()
if ($env:PYTHON_EXE -and (Test-Path $env:PYTHON_EXE)) { $candidates += $env:PYTHON_EXE }
$candidates += (Get-Command python -ErrorAction SilentlyContinue).Source
foreach ($c in @("$env:LOCALAPPDATA\Programs\Python\Python314\python.exe",
                 "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
                 "C:\Program Files\Python314\python.exe",
                 "C:\Program Files\Python312\python.exe")) {
    if (Test-Path $c) { $candidates += $c }
}
foreach ($c in ($candidates | Where-Object { $_ } | Select-Object -Unique)) {
    & $c -c $importCheck 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { $PY = $c; break }
}
if (-not $PY) { Die "no Python interpreter can import the app deps. Run: pip install -r backend\requirements.txt" }
$pyVer = (& $PY --version 2>&1)
Write-Output "  interpreter: $pyVer  ($PY)"

# ---- 3. start single-process uvicorn (NO --reload) ------------------------
Write-Output "[start_backend] step 3/5: starting uvicorn (single process, no --reload)..."
$uvArgs = @('-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', "$Port")
$proc = Start-Process -FilePath $PY -ArgumentList $uvArgs `
        -WorkingDirectory $backendDir -WindowStyle Hidden -PassThru
if (-not $proc) { Die "Start-Process failed." }
$backendPid = $proc.Id
Write-Output "  backend PID = $backendPid"

# ---- 4. print access info -------------------------------------------------
$healthUrl = "http://127.0.0.1:$Port/api/v1/health"
Write-Output "[start_backend] step 4/5: endpoints"
Write-Output "  PID        : $backendPid"
Write-Output "  listen     : 0.0.0.0:$Port"
Write-Output "  health     : $healthUrl"
Write-Output "  api docs   : http://localhost:$Port/docs"
Write-Output "  frontend   : $FrontendUrl"

# ---- 5. timed health probe ------------------------------------------------
Write-Output "[start_backend] step 5/5: waiting for health (max $HealthTimeoutSec s)..."
$deadline = (Get-Date).AddSeconds($HealthTimeoutSec)
$ok = $false
$latencyMs = -1
while ((Get-Date) -lt $deadline) {
    if (-not (Get-Process -Id $backendPid -ErrorAction SilentlyContinue)) {
        Die "backend PID $backendPid exited during startup; check logs/backend.log"
    }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $resp = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
        $code = $resp.StatusCode
    } catch {
        $code = 0
    }
    $sw.Stop()
    if ($code -eq 200) {
        $latencyMs = [int]$sw.ElapsedMilliseconds
        $ok = $true
        break
    }
    Start-Sleep -Milliseconds 500
}

if (-not $ok) {
    Write-Output "[start_backend] health check did NOT pass within $HealthTimeoutSec s."
    Write-Output "  last error: $($_.Exception.Message)"
    Die "backend not healthy; PID $backendPid may still be booting or crashed - inspect logs/backend.log"
}

Write-Output ("[start_backend] OK - backend healthy in {0} ms (PID {1}, port {2})." -f $latencyMs, $backendPid, $Port)

# open the frontend for convenience (non-fatal if it fails)
try { Start-Process $FrontendUrl } catch {}
exit 0
