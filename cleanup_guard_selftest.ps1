# ============================================================================
#  cleanup_guard_selftest.ps1 - regression test for cleanup_guard.ps1
#  ============================================================================
#  Run whenever cleanup_guard.ps1, kill_port.ps1 or kill_zombie_pytest.ps1
#  changes:  powershell -NoProfile -ExecutionPolicy Bypass -File .\cleanup_guard_selftest.ps1
#
#  What it verifies (each leftover class the guard must find and remove):
#    CASE A  fake BACKEND   python -m uvicorn app.main:app on a temp port
#    CASE B  fake FRONTEND  python -m http.server with the toolbox root path
#                           serialized in argv (a vite-less stand-in)
#    CASE C  fake PYTEST    python -c "...#py.test" looking like a test worker
#    CASE D  fake STUB      python -c "import urllib.request...urlopen..."
#
#  Sequence: cast a Check (must report exit 2 WITHOUT killing), then a full
#  Shutdown (must kill all four + exit 0).
#
#  SAFETY: refuses to run while the REAL toolbox ports (8000/5175) are being
#  listened on - a live app would otherwise be swept by the Shutdown phase.
#
#  Windows only. ASCII-only output (see kill_port.ps1).
# ============================================================================

param(
    [int]    $Port   = 8173,
    [string] $Python = ''
)

$ErrorActionPreference = 'SilentlyContinue'
$script:OutLines = @()
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
$helper = Join-Path $root 'cleanup_guard.ps1'
$failures = @()
$spawned = @()
$tmpDir = Join-Path $env:TEMP ("cleanup_guard_selftest_{0}" -f (Get-Random))

if (-not (Test-Path $helper)) {
    Write-Host "FATAL: $helper not found"
    exit 1
}

if (-not $Python) {
    $Python = 'python'
    foreach ($cand in @('python', 'py')) {
        & $cand -c 'import uvicorn' 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { $Python = $cand; break }
    }
}

function Is-Alive([int]$procId) {
    return [bool](Get-CimInstance Win32_Process -Filter "ProcessId=$procId")
}

function Remove-Spawned {
    foreach ($p in $spawned) {
        if ((Is-Alive $p)) { taskkill /PID $p /T /F | Out-Null }
    }
}

function Invoke-Guard([string[]]$Arguments) {
    $script:OutLines = @()
    # NOTE: splatting variable must NOT be named `Args` - @args is the automatic
    # unbound-arguments array, so a parameter of the same name silently splats an
    # EMPTY array and every guard call falls back to Mode Check (proven in CI).
    $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $helper @Arguments 2>&1
    foreach ($l in $out) { $script:OutLines += "$l" }
    $script:GuardExit = $LASTEXITCODE
}

# --- preconditions: the real toolbox must not be running -------------------
$live = Get-NetTCPConnection -LocalPort 8000, 5175 -ErrorAction SilentlyContinue
if ($live) {
    Write-Host "FATAL: the real toolbox is listening on 8000/5175 - stop it first (stop_all.bat),"
    Write-Host "       then re-run this selftest. The Shutdown phase would kill a live app."
    exit 1
}

Write-Host ("=== cleanup_guard.ps1 selftest (python={0}, port={1}) ===" -f $Python, $Port)

# clean slate first
Invoke-Guard @('-Mode', 'Shutdown', '-Ports', "$Port", '-SkipTempSweep', '-Quiet')
# ------------------------------------------------------------------ setup ---
New-Item -ItemType Directory -Path (Join-Path $tmpDir 'app') -Force | Out-Null
Set-Content -Path (Join-Path $tmpDir 'app\__init__.py') -Value '' -Encoding ASCII
@'
"""Minimal ASGI app for cleanup_guard_selftest.ps1 (fake backend)."""
async def app(scope, receive, send):
    body = b"ok"
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
'@ | Set-Content -Path (Join-Path $tmpDir 'app\main.py') -Encoding ASCII

function Wait-Port([int]$p, [int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

# CASE A: fake backend = uvicorn app.main:app on $Port
$srvA = Start-Process -FilePath $Python -ArgumentList @('-m', 'uvicorn', 'app.main:app',
        '--host', '127.0.0.1', '--port', "$Port") `
    -WorkingDirectory $tmpDir -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $tmpDir 'uvicorn.out') `
    -RedirectStandardError (Join-Path $tmpDir 'uvicorn.err')
$spawned += $srvA.Id
if (-not (Wait-Port $Port 60)) {
    Write-Host '[CASE A] FAIL - fake backend never bound the port; skipping'
    $failures += 'A(skip)'
    Get-Content (Join-Path $tmpDir 'uvicorn.err') -ErrorAction SilentlyContinue | Select-Object -Last 10
    $srvA = $null
}

# CASE B: fake frontend = node image with the toolbox root serialized on the
# command line (this is how a real vite child looks before it binds a port,
# so the guard must classify it via Test-FrontendCmd / root-path match)
$srvB = Start-Process -FilePath 'node' -ArgumentList @('-e', 'setTimeout(function(){},240000)', $root) `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru
$spawned += $srvB.Id
Start-Sleep -Milliseconds 600

# CASE C: fake pytest worker - a script literally named py.test.py under %TEMP%
Set-Content -Path (Join-Path $tmpDir 'py.test.py') -Value 'import time; time.sleep(300)' -Encoding ASCII
$srvC = Start-Process -FilePath $Python -ArgumentList @((Join-Path $tmpDir 'py.test.py')) `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru
$spawned += $srvC.Id

# CASE D: fake timeout leftover (health-probe stub signature). The code is
# SINGLE-quoted inside python and contains NO spaces on the outer command line
# on purpose: Start-Process strips embedded double quotes (proven) and splits
# at spaces (no auto-quoting), so a naive `import urllib.request` would arrive
# as `import` + `urllib.request` and die instantly with a SyntaxError.
$srvD = Start-Process -FilePath $Python -ArgumentList @('-c', "__import__('urllib.request').request.urlopen;__import__('time').sleep(300)") `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru
$spawned += $srvD.Id

# all fakes are parked in time.sleep(300); make sure the stub is beyond -StubMinAgeSec 2
Start-Sleep -Seconds 3

# ------------------------------------------------------- CASE 1: Check -----
Write-Host ''
Write-Host '[CASE 1] Check mode must REPORT leftovers and NOT kill anything'
$aliveBefore = @($srvA.Id, $srvB.Id, $srvC.Id, $srvD.Id) | Where-Object { $_ -and (Is-Alive $_) }
Invoke-Guard @('-Mode', 'Check', '-Ports', "$Port", '-StubMinAgeSec', '2')
$script:OutLines | ForEach-Object { Write-Host "  $_" }
$aliveAfter = @($srvA.Id, $srvB.Id, $srvC.Id, $srvD.Id) | Where-Object { $_ -and (Is-Alive $_) }
if ($script:GuardExit -eq 2 -and $aliveBefore.Count -eq 4 -and $aliveAfter.Count -eq 4) {
    Write-Host '[CASE 1] PASS'
} else {
    $why = if ($script:GuardExit -ne 2) { "check exit=$($script:GuardExit) (want 2)" }
           elseif ($aliveBefore.Count -ne 4) { "only $($aliveBefore.Count) fakes spawned" }
           else { "Check killed fakes: alive after = $($aliveAfter -join ',')" }
    Write-Host "[CASE 1] FAIL - $why"
    $failures += '1'
}

# ---------------------------------------------------- CASE 2: Shutdown -----
Write-Host ''
Write-Host '[CASE 2] Shutdown mode must KILL all four fakes and exit 0'
Invoke-Guard @('-Mode', 'Shutdown', '-Ports', "$Port", '-StubMinAgeSec', '2', '-SkipTempSweep')
$script:OutLines | ForEach-Object { Write-Host "  $_" }
$alive = @($srvA.Id, $srvB.Id, $srvC.Id, $srvD.Id) | Where-Object { $_ -and (Is-Alive $_) }
if ($script:GuardExit -eq 0 -and $alive.Count -eq 0) {
    Write-Host '[CASE 2] PASS'
} else {
    $why = if ($script:GuardExit -ne 0) { "shutdown exit=$($script:GuardExit) (want 0)" }
           else { "still alive: $($alive -join ',')" }
    Write-Host "[CASE 2] FAIL - $why"
    $failures += '2'
}

# ------------------------------------------------------------------result ---
Remove-Spawned
& powershell -NoProfile -ExecutionPolicy Bypass -File $helper -Ports "$Port" -Quiet -SkipTempSweep | Out-Null
Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue

Write-Host ''
if ($failures.Count -eq 0) {
    Write-Host '=== ALL CASES PASS ==='
    exit 0
}
Write-Host ("=== FAILED: {0} ===" -f ($failures -join ', '))
exit 1