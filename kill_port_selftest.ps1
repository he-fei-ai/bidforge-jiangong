# ============================================================================
#  kill_port_selftest.ps1 - regression test for kill_port.ps1
#  ============================================================================
#  Run it whenever kill_port.ps1 or the port-cleanup step of start_all.bat
#  changes:  powershell -NoProfile -ExecutionPolicy Bypass -File .\kill_port_selftest.ps1
#
#  CASE A  plain live listener (python -m http.server)  -> must be released
#  CASE B  uvicorn --reload with the reloader parent killed WITHOUT /T:
#          netstat keeps reporting the DEAD parent PID as socket owner while an
#          orphan worker holds the inherited handle. This is the real-world
#          failure that made start_all.bat abort with a bogus "run as admin"
#          hint. kill_port.ps1 must find and kill the orphan.
#
#  Windows only. Uses ASCII-only output (start_all.bat is GBK/CP936).
# ============================================================================

param(
    [int] $Port = 8123,
    [string] $Python = ''
)

$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$helper = Join-Path $root 'kill_port.ps1'
$failures = @()

if (-not (Test-Path $helper)) {
    Write-Host "FATAL: $helper not found"
    exit 1
}

if (-not $Python) {
    $Python = 'python'
    # Prefer the interpreter that can actually import uvicorn.
    foreach ($cand in @('python', 'py')) {
        & $cand -c 'import uvicorn' 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { $Python = $cand; break }
    }
}

function Get-Owner([int]$p) {
    $c = Get-NetTCPConnection -LocalPort $p -State Listen
    if ($c) { return @($c | ForEach-Object { $_.OwningProcess } | Select-Object -Unique) }
    return @()
}

function Wait-Owner([int]$p, [int]$seconds, [bool]$wantBusy) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        $busy = (Get-Owner $p).Count -gt 0
        if ($busy -eq $wantBusy) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

# The helper's stdout must NOT be captured into the return value: doing so
# gives back an array of log lines and the real exit code gets buried at the
# end of it. Output goes straight to the console, the code lands in
# $script:HelperExit.
function Invoke-Helper([int]$p, [switch]$Quiet) {
    if ($Quiet) {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $helper -Ports $p -Quiet | Out-Null
    } else {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $helper -Ports $p
    }
    $script:HelperExit = $LASTEXITCODE
}

Write-Host "=== kill_port.ps1 selftest (python=$Python, port=$Port) ==="

# Start from a clean slate: a leaked holder from an earlier run would otherwise
# turn CASE A into a false pass and CASE B into "never bound".
Invoke-Helper $Port -Quiet

# ---------------------------------------------------------------- CASE A -----
Write-Host ''
Write-Host '[CASE A] live listener must be released'
$srvA = Start-Process -FilePath $Python -ArgumentList '-m', 'http.server', "$Port" `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru
if (Wait-Owner $Port 10 $true) {
    Invoke-Helper $Port
    if (Wait-Owner $Port 10 $false) {
        Write-Host '[CASE A] PASS'
    } else {
        Write-Host '[CASE A] FAIL - port still busy'
        $failures += 'A'
    }
} else {
    Write-Host '[CASE A] FAIL - listener never bound'
    $failures += 'A'
}
if ($srvA -and (Get-Process -Id $srvA.Id)) { taskkill /PID $srvA.Id /T /F | Out-Null }

# ---------------------------------------------------------------- CASE B -----
Write-Host ''
Write-Host '[CASE B] uvicorn --reload orphan worker (dead parent PID owns the socket)'
$appDir = Join-Path $env:TEMP ("kill_port_selftest_{0}" -f (Get-Random))
New-Item -ItemType Directory -Path $appDir -Force | Out-Null
$appFile = Join-Path $appDir 'selftest_app.py'
@'
"""Minimal ASGI app used only by kill_port_selftest.ps1."""


async def app(scope, receive, send):
    body = b"ok"
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})
'@ | Set-Content -Path $appFile -Encoding ASCII

$logOut = Join-Path $appDir 'uvicorn.out'
$logErr = Join-Path $appDir 'uvicorn.err'
$srvB = Start-Process -FilePath $Python -ArgumentList '-m', 'uvicorn', 'selftest_app:app', `
    '--reload', '--host', '127.0.0.1', '--port', "$Port" `
    -WorkingDirectory $appDir -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $logOut -RedirectStandardError $logErr
if (-not (Wait-Owner $Port 60 $true)) {
    Write-Host '[CASE B] SKIP - uvicorn --reload never bound the port'
    $failures += 'B(skip)'
    Get-Content $logErr -ErrorAction SilentlyContinue | Select-Object -Last 15
    Get-Content $logOut -ErrorAction SilentlyContinue | Select-Object -Last 15
} else {
    $ownersBefore = Get-Owner $Port
    # Kill ONLY the reloader: the worker survives with the inherited socket.
    taskkill /PID $srvB.Id /F | Out-Null
    Start-Sleep -Seconds 2
    $parentAlive = [bool](Get-CimInstance Win32_Process -Filter "ProcessId=$($srvB.Id)")
    $ownersAfter = Get-Owner $Port
    Write-Host ("  reloader PID {0} alive after kill: {1}" -f $srvB.Id, $parentAlive)
    Write-Host ("  socket owner PID(s) reported now : {0}" -f ($ownersAfter -join ', '))
    if ($ownersAfter.Count -eq 0) {
        Write-Host '[CASE B] PASS (trivial - parent kill already released the port)'
    } else {
        Invoke-Helper $Port
        if ((Wait-Owner $Port 10 $false) -and ($script:HelperExit -eq 0)) {
            Write-Host '[CASE B] PASS - orphan holder swept, port released'
        } else {
            Write-Host '[CASE B] FAIL - port still held by an orphan process'
            $failures += 'B'
        }
    }
}
if ($srvB -and (Get-Process -Id $srvB.Id)) { taskkill /PID $srvB.Id /T /F | Out-Null }
Invoke-Helper $Port -Quiet
Remove-Item -Recurse -Force $appDir

# ---------------------------------------------------------------- RESULT -----
Write-Host ''
if ($failures.Count -eq 0) {
    Write-Host '=== ALL CASES PASS ==='
    exit 0
}
Write-Host ("=== FAILED: {0} ===" -f ($failures -join ', '))
exit 1
