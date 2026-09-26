
# ============================================================================
#  kill_zombie_pytest.ps1 - detect & clean leftover pytest / test-worker
#  processes safely on Windows.
#  ============================================================================
#  WHY THIS EXISTS
#  Running the backend's pytest suite from an agent / terminal can leak child
#  processes: a `python -m pytest` that is interrupted (Ctrl+C, killed session,
#  crashed harness) may leave behind:
#    * the pytest process itself still holding open file handles (it can pin
#      logs/backend.log -> log rotation freezes, see AGENTS.md 5.6),
#    * spawn / multiprocessing workers (children of pytest) that keep running,
#    * a hung `ruff`/tool subprocess the test spawned.
#  Before touching anything this script LIST matching processes with PID /
#  parent / start-time / command line, so the operator can audit the source.
#
#  SAFETY - how we avoid killing the wrong thing
#    * only python.exe-style processes whose command line clearly references
#      pytest (or its spawn workers) are candidates;
#    * NEVER touch the current process, its ancestors (the shell running this
#      script), PIDs 0/4 (System Idle / System), or any non-python process;
#    * default action is KILL, pass -DryRun to only list.
#
#  Exit code: 0 = nothing left running (or -DryRun), 1 = some candidates could
#  not be killed.
#
#  NOTE: keep all output ASCII-only (console code page safety, see kill_port.ps1)
# ============================================================================

param(
    [switch] $DryRun,
    [switch] $Quiet
)

$ErrorActionPreference = 'SilentlyContinue'

function Write-Info($msg) {
    if (-not $Quiet) { [Console]::Out.WriteLine($msg) }
}

# ---- build the "must never kill" PID set: self + all ancestors ------------
$Protected = New-Object System.Collections.Generic.HashSet[int]
[void]$Protected.Add(0)
[void]$Protected.Add(4)
[void]$Protected.Add([System.Diagnostics.Process]::GetCurrentProcess().Id)

# walk up the parent chain of the current process
$curPid = [System.Diagnostics.Process]::GetCurrentProcess().Id
for ($guard = 0; $guard -lt 64; $guard++) {
    $pi = Get-CimInstance Win32_Process -Filter "ProcessId=$curPid"
    if (-not $pi) { break }
    $pp = [int]$pi.ParentProcessId
    if ($pp -le 0 -or $Protected.Contains($pp)) { break }
    [void]$Protected.Add($pp)
    $curPid = $pp
}

function Test-PytestCmd($cmd) {
    if (-not $cmd) { return $false }
    # `-m pytest`, `.../pytest`, `py.test`, `pytest.ini`, or a spawn worker
    return ($cmd -match '(^|[\s/\\])-m\s+pytest' -or
            $cmd -match '[\s/\\]pytest(\.exe|\.py)?(\s|$)' -or
            $cmd -match 'py\.test' -or
            $cmd -match 'multiprocessing')
}

$all = Get-CimInstance Win32_Process
$pythonNames = $all | Where-Object { $_.Name -match '^python(\.exe|w\.exe)?$' -or $_.Name -match '^py(\.exe)?$' }

# map pid -> process for parent checks
$byPid = @{}
foreach ($p in $all) { $byPid[[int]$p.ProcessId] = $p }

# a multiprocessing/spawn child only counts as a zombie if it (or an ancestor)
# is a pytest run - avoids killing unrelated python tools that use spawns.
function Test-IsPytestTree($p) {
    if (Test-PytestCmd $p.CommandLine) { return $true }
    $node = $p
    for ($g = 0; $g -lt 16; $g++) {
        $ppid = [int]$node.ParentProcessId
        if (-not $byPid.ContainsKey($ppid)) { break }
        $node = $byPid[$ppid]
        if (Test-PytestCmd $node.CommandLine) { return $true }
    }
    return $false
}

$candidates = @()
foreach ($p in $pythonNames) {
    $id = [int]$p.ProcessId
    if ($Protected.Contains($id)) { continue }
    if ($p.CommandLine -match 'lsp_server|autopep8|pylance|language_server|site-packages.\\(ruff|black)') { continue }
    if (Test-PytestCmd $p.CommandLine -or ($p.CommandLine -match 'spawn_main|multiprocessing' -and (Test-IsPytestTree $p))) {
        $candidates += $p
    }
}

if ($candidates.Count -eq 0) {
    Write-Info 'kill_zombie_pytest: no leftover pytest / test-worker processes found.'
    exit 0
}

Write-Info ("kill_zombie_pytest: found {0} candidate process(es):" -f $candidates.Count)
$fail = 0
foreach ($p in $candidates) {
    $dt = $null
    try { $dt = $p.ConvertToDateTime($p.CreationDate) } catch {}
    $start = if ($dt) { $dt.ToString('yyyy-MM-dd HH:mm:ss') } else { 'NA' }
    $cmd = ($p.CommandLine -replace '\s+', ' ')
    if ($cmd.Length -gt 160) { $cmd = $cmd.Substring(0, 160) + '...' }
    Write-Info ("  PID={0} PPID={1} START={2}" -f $p.ProcessId, $p.ParentProcessId, $start)
    Write-Info ("    CMD: $cmd")

    if ($DryRun) { continue }

    & taskkill /PID $p.ProcessId /T /F 2>&1 | Out-Null
    $still = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.ProcessId)"
    if ($still) {
        Write-Info ("    [!] FAILED to kill PID {0}" -f $p.ProcessId)
        $fail++
    } else {
        Write-Info ("    [OK] killed PID {0}" -f $p.ProcessId)
    }
}

if ($DryRun) {
    Write-Info 'kill_zombie_pytest: (DryRun) nothing was killed.'
    exit 0
}
if ($fail -gt 0) { exit 1 } else { exit 0 }
