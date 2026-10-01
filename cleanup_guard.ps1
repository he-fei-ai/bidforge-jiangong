# ============================================================================
#  cleanup_guard.ps1 - unified process check & cleanup mechanism
#  ============================================================================
#  WHY THIS EXISTS
#  Starting / stopping the toolbox (backend :8000 + frontend :5175) can leave
#  residue that KILL_PORT and KILL_PYTEST alone cannot cover:
#    * backend python (uvicorn app.main:app) that failed to bind a port
#      (crash loop / hung boot) - no port owner, so port-based kill misses it;
#    * frontend node/npm/vite processes after the console window is closed -
#      vite re-binds 5176+ when 5175 is taken, so a port-based sweep finds
#      nothing while the dev server keeps running;
#    * timeout leftovers - transient `import app.main` / urllib health-probe
#      python stubs that hung instead of exiting;
#    * orphaned processes whose parent died (the "zombie" family) yet hold no
#      port - classic when the start_all.bat console windows are force-closed;
#    * zombie pytest / test-worker trees (they pin logs/backend.log and freeze
#      the 5MB log rotation, see AGENTS.md 5.6).
#
#  DESIGN DECISIONS
#    * One mechanism, one report. Modes:
#        -Check     inventory only, never kill (exit 2 if issues found, 0 else)
#        -Clean     check + full cleanup (ports, zombie pytest, everything)
#        -Startup   same as Clean (used by start_all.bat at boot; the bat's own
#                   passes 1-3 made the work idempotent, so re-running them here
#                   is free of side effects and keeps standalone Startup correct)
#        -Shutdown  Clean + sweep app-owned temp/lock/pytest .pt_* residue
#    * Reuse the hardened sub-scripts as the single source of truth:
#        - kill_port.ps1          port owners + orphan socket holders
#        - kill_zombie_pytest.ps1 leaked pytest / test workers (DryRun=report)
#    * Safety (never kill the wrong thing):
#        - protected PID set = 0/4 + this process + every ancestor (walk up);
#        - image-name gating: only python*/node/npm/cmd.exe processes are
#          considered; powershell/conhost/OpenConsole are never candidates;
#        - signature gating: command line must match the toolbox fingerprint
#          (uvicorn app.main:app / the toolbox root path / a known stub).
#
#  Exit code: 0 = clean, 1 = cleanup incomplete, 2 = (Check/DryRun) issues.
#  NOTE: keep all output ASCII-only (console code page safety, see kill_port.ps1).
# ============================================================================

param(
    [ValidateSet('Check', 'Clean', 'Startup', 'Shutdown')]
    [string] $Mode = 'Check',
    [string] $Ports         = '8000,5175',
    [int]    $WaitSec       = 8,
    # a stub younger than this is treated as "still working" and left alone
    [int]    $StubMinAgeSec = 60,
    [switch] $DryRun,
    [switch] $Quiet,
    [switch] $SkipTempSweep
)

$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
$leaf = Split-Path -Leaf $root
$script:Failures = 0
function Write-Info($msg) {
    if (-not $Quiet) { [Console]::Out.WriteLine($msg) }
}

function Die($msg) {
    [Console]::Error.WriteLine("[cleanup_guard] $msg")
    exit 1
}

# --------------------------------------------------------------------------
#  protected PID set: 0 / 4 / self / every ancestor of this process
# --------------------------------------------------------------------------
$script:Protected = New-Object 'System.Collections.Generic.HashSet[int]'
[void]$script:Protected.Add(0)
[void]$script:Protected.Add(4)
$curPid = [System.Diagnostics.Process]::GetCurrentProcess().Id
[void]$script:Protected.Add($curPid)
for ($g = 0; $g -lt 64; $g++) {
    $pi = Get-CimInstance Win32_Process -Filter "ProcessId=$curPid"
    if (-not $pi) { break }
    $pp = [int]$pi.ParentProcessId
    if ($pp -le 0 -or $script:Protected.Contains($pp)) { break }
    [void]$script:Protected.Add($pp)
    $curPid = $pp
}

function Test-Protected([int]$id) {
    return $script:Protected.Contains($id)
}

# process snapshot used by inventory + orphan detection
$script:AllProcs = @(Get-CimInstance Win32_Process)
$script:AlivePids = @{}
foreach ($p in $script:AllProcs) {
    $script:AlivePids[[int]$p.ProcessId] = $true
}

function Test-Alive([int]$id) {
    if ($id -le 0) { return $false }
    return $script:AlivePids.ContainsKey($id)
}

# live check used after our own kills - the snapshot would still say "alive"
# for a process killed one step earlier
function Test-AliveNow([int]$id) {
    if ($id -le 0) { return $false }
    return [bool](Get-CimInstance Win32_Process -Filter "ProcessId=$id")
}

# --------------------------------------------------------------------------
#  image-name gates (the ONLY process families we ever consider)
# --------------------------------------------------------------------------
function Test-PythonImg($n) { return $n -match '^python(w)?(\d+(\.\d+)*)?(\.exe)?$' -or $n -match '^py(\.exe)?$' }
function Test-NodeImg($n)   { return $n -match '^node(w)?(\.exe)?$' -or $n -match '^npm(\.exe)?$' }
function Test-CmdImg($n)    { return $n -match '^cmd(\.exe)?$' }

# --------------------------------------------------------------------------
#  signature classifiers (single source of truth for "is this ours?")
# --------------------------------------------------------------------------
function Test-BackendCmd($cmd) {
    if (-not $cmd) { return $false }
    return ($cmd -match 'app\.main:app')
}

function Test-StubCmd($cmd) {
    if (-not $cmd) { return $false }
    # dependency-check / health-probe one-liners the start script spawns;
    # these MUST exit within seconds - a still-runner is a timeout leftover
    if ($cmd -match 'import\s+app\.main') { return $true }
    if ($cmd -match 'urllib\.request' -and $cmd -match 'urlopen') { return $true }
    return $false
}

function Test-FrontendCmd($cmd, $img) {
    if (-not $cmd) { return $false }
    # the toolbox root path on the command line is the fingerprint
    if (-not ($cmd -match [regex]::Escape($root))) { return $false }
    # node running a script under our root (vite / npm-cli of THIS project):
    # root-in-argv is already specific enough for node.
    if (Test-NodeImg $img) { return $true }
    # cmd.exe: a sibling tool shell whose command line merely mentions the
    # workspace MUST NOT be killed. Require a real dev-server command.
    return ($cmd -match 'npm(\.cmd|\.js)?(\s+)(run\s+dev|install|start)' -or
            $cmd -match 'vite(\s|$)' -or
            $cmd -match 'node_modules[\\/](vite|esbuild|rollup|react-refresh)')
}
# --------------------------------------------------------------------------
#  inventory
# --------------------------------------------------------------------------
# PIDs owning the toolbox's listen ports = the "active service" set. Check mode
# must NOT flag a healthy running backend/frontend as residue; stragglers of the
# same signature (crash loop, re-bound vite on 5176+, hung boot) ARE residue.
$script:PortArr = @($Ports -split ',' | ForEach-Object { $_.Trim() } |
    Where-Object { $_ } | ForEach-Object { [int]$_ })
$script:ListenPids = @{}
if ($script:PortArr.Count -gt 0) {
    $tcp = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $script:PortArr -contains [int]$_.LocalPort })
    foreach ($c in $tcp) { $script:ListenPids[[int]$c.OwningProcess] = $true }
}

# pid start-time lookup - built LAZILY, only when a STUB is matched. Stub
# staleness is the only decision that needs process age. WMI CreationDate is
# frequently EMPTY on this host (proved by selftest), so the .NET start time
# is the reliable source - but a naive "resolve every process" batch cost ~4s
# per run, so it is charged only to stub rows.
$script:StartMap = @{}
$script:StartMapBuilt = $false

function Get-StubAgeSec($p) {
    # age for the stub-staleness decision, computed once per stub row
    if (-not $script:StartMapBuilt) {
        foreach ($np in @(Get-Process -ErrorAction SilentlyContinue)) {
            try { $script:StartMap[[int]$np.Id] = $np.StartTime } catch {}
        }
        $script:StartMapBuilt = $true
    }
    $start = $null
    if ($script:StartMap.ContainsKey([int]$p.ProcessId)) {
        $start = $script:StartMap[[int]$p.ProcessId]
    }
    if (-not $start) { return 0.0 }
    return ((Get-Date) - $start).TotalSeconds
}

function Get-Inventory {
    $items = @()
    foreach ($p in $script:AllProcs) {
        $id = [int]$p.ProcessId
        if (Test-Protected $id) { continue }
        $name = $p.Name
        $cmd = if ($p.CommandLine) { $p.CommandLine } else { '' }
        $cmd1 = ($cmd -replace '\s+', ' ')
        if ($cmd1.Length -gt 200) { $cmd1 = $cmd1.Substring(0, 200) }

        $type = $null
        if (Test-PythonImg $name) {
            if (Test-BackendCmd $cmd) {
                $type = 'backend'
            } elseif (Test-StubCmd $cmd) {
                $type = 'stub'
            }
        } elseif (Test-NodeImg $name) {
            if (Test-FrontendCmd $cmd $name) { $type = 'frontend' }
        } elseif (Test-CmdImg $name) {
            if (Test-FrontendCmd $cmd $name) { $type = 'frontend' }
        }
        if (-not $type) { continue }

        $start = $null
        $age = 0
        if ($type -eq 'stub') {
            $age = Get-StubAgeSec $p   # only stubs need age; backend/frontend are always stale
        }
        $pp = [int]$p.ParentProcessId
        $orphan = (-not $script:AlivePids.ContainsKey($pp)) -and (-not (Test-Protected $pp))
        # stubs younger than StubMinAgeSec are "still working", not leftovers yet
        $stale = ($type -ne 'stub') -or ($age -ge $StubMinAgeSec) -or $orphan
        $isService = $script:ListenPids.ContainsKey($id)
        $items += [pscustomobject]@{
            pid = $id; ppid = $pp; type = $type; name = $name; cmd = $cmd1
            start = $start; ageSec = [int]$age; orphan = $orphan; stale = $stale
            isService = $isService
        }
    }

    # titled toolbox console windows (e.g. "back-...toolbox" / "front-...toolbox").
    # Get-Process sees MainWindowTitle; Win32_Process does not. Only titles that
    # end in "-<leaf>" are matched, so an unrelated terminal tab or prompt that
    # merely shows the folder name is never killed.
    $titleSuffix = '-' + [regex]::Escape($leaf)
    foreach ($c in @(Get-Process -Name cmd -ErrorAction SilentlyContinue)) {
        if (Test-Protected ([int]$c.Id)) { continue }
        $t = $c.MainWindowTitle
        if ($t -and $t -match $titleSuffix) {
            $items += [pscustomobject]@{
                pid = $c.Id; ppid = 0; type = 'window'; name = 'cmd'; cmd = $t
                start = $c.StartTime; ageSec = [int](((Get-Date) - $c.StartTime).TotalSeconds)
                orphan = $false; stale = $true; isService = $true
            }
        }
    }
    return $items
}

# --------------------------------------------------------------------------
#  zombie pytest detection - INLINE, no child powershell (mirrors the logic in
#  kill_zombie_pytest.ps1; keep the two in sync, selftests gate both).
# --------------------------------------------------------------------------
function Test-PytestCmdInline($cmd) {
    if (-not $cmd) { return $false }
    return ($cmd -match '(^|[\s/\\])-m\s+pytest' -or
            $cmd -match '[\s/\\]pytest(\.exe|\.py)?(\s|$)' -or
            $cmd -match 'py\.test' -or
            $cmd -match 'multiprocessing')
}

function Get-PytestCandidates {
    $all = @(Get-CimInstance Win32_Process)
    $byPid = @{}
    foreach ($p in $all) { $byPid[[int]$p.ProcessId] = $p }

    function Test-PytestTreeLocal($p) {
        if (Test-PytestCmdInline $p.CommandLine) { return $true }
        $node = $p
        for ($g = 0; $g -lt 16; $g++) {
            $ppid = [int]$node.ParentProcessId
            if (-not $byPid.ContainsKey($ppid)) { break }
            $node = $byPid[$ppid]
            if (Test-PytestCmdInline $node.CommandLine) { return $true }
        }
        return $false
    }

    $candidates = @()
    foreach ($p in $all) {
        $id = [int]$p.ProcessId
        if (Test-Protected $id) { continue }
        if (-not (Test-PythonImg $p.Name)) { continue }
        if ($p.CommandLine -match 'lsp_server|autopep8|pylance|language_server|site-packages\.\\(ruff|black)') { continue }
        if ((Test-PytestCmdInline $p.CommandLine) -or ($p.CommandLine -match 'spawn_main|multiprocessing' -and (Test-PytestTreeLocal $p))) {
            $candidates += $p
        }
    }
    return $candidates
}

function Get-PytestLeaks {
    $rows = @()
    foreach ($p in @(Get-PytestCandidates)) {
        $cmd = if ($p.CommandLine) { ($p.CommandLine -replace '\s+', ' ') } else { '' }
        if ($cmd.Length -gt 160) { $cmd = $cmd.Substring(0, 160) }
        $rows += [pscustomobject]@{ pid = [int]$p.ProcessId; type = 'pytest'; cmd = $cmd }
    }
    return $rows
}

function Write-InventoryRow($it) {
    $tag = if ($it.orphan) { ' orphan' } else { '' }
    Write-Info ("  PID={0,-6} PPID={1,-6} TYPE={2,-9} AGE={3,5}s{4} | {5}" -f `
        $it.pid, $it.ppid, $it.type, $it.ageSec, $tag, $it.cmd)
}
# --------------------------------------------------------------------------
#  kill helpers
# --------------------------------------------------------------------------
function Stop-ProcessId([int]$procId, $tag) {
    if ($DryRun) {
        Write-Info "  [dry-run] would kill PID $procId ($tag)"
        return $true
    }
    & taskkill /PID $procId /T /F 2>&1 | Out-Null
    $deadline = (Get-Date).AddSeconds(3)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-AliveNow $procId)) { return $true }
        Start-Sleep -Milliseconds 300
    }
    if (Test-AliveNow $procId) {
        Write-Info "  [!] FAILED to kill PID $procId ($tag)"
        $script:Failures++
        return $false
    }
    return $true
}

# ---- port helpers (mirror kill_port.ps1) ----------------------------------
function Get-PortOwnerPidsInline($port) {
    $pids = @()
    foreach ($c in @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) {
        $pids += [int]$c.OwningProcess
    }
    if ($pids.Count -eq 0) {
        # netstat fallback when the CIM/TCP stack is unavailable
        $needle = ":$port "
        foreach ($l in @(netstat -ano 2>$null | Select-String -SimpleMatch $needle | Select-String -Pattern 'LISTENING')) {
            $cols = @($l.Line -split '\s+' | Where-Object { $_ })
            if ($cols.Count -ge 5) {
                $p = 0
                if ([int]::TryParse($cols[-1], [ref]$p)) { $pids += $p }
            }
        }
    }
    return @($pids | Where-Object { $_ -and $_ -ne 0 } | Select-Object -Unique)
}

function Get-LivingDescendantsInline([int]$rootPid) {
    # BFS over ParentProcessId; finds the inherited-socket holder even when the
    # reported owner (reloader) is already dead - mirrors kill_port.ps1.
    $found = @()
    $queue = New-Object System.Collections.Queue
    $queue.Enqueue($rootPid)
    $visited = @{}
    while ($queue.Count -gt 0) {
        $cur = $queue.Dequeue()
        if ($visited.ContainsKey($cur)) { continue }
        $visited[$cur] = $true
        foreach ($ch in @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$cur" -ErrorAction SilentlyContinue)) {
            $cid = [int]$ch.ProcessId
            if ($cid -le 4 -or (Test-Protected $cid)) { continue }
            $found += $cid
            $queue.Enqueue($cid)
        }
    }
    return @($found | Select-Object -Unique)
}

function Invoke-KillPort {
    # Inline release of the toolbox listen ports - mirrors kill_port.ps1:
    # kill live owners, then walk orphan descendants (the "reloader dead but
    # orphan worker keeps LISTENing" case), then wait for the listener to drop.
    foreach ($port in $script:PortArr) {
        $owners = @(Get-PortOwnerPidsInline $port)
        if ($owners.Count -eq 0) {
            Write-Info "  port $port : free"
            continue
        }
        foreach ($o in $owners) {
            if (Test-Protected $o) { continue }
            if (Test-AliveNow $o) {
                if (Stop-ProcessId $o "port $port owner") { Write-Info "  port $port : killed PID $o" }
            } else {
                Write-Info "  port $port : owner PID $o dead, sweeping orphan descendants..."
            }
            foreach ($d in @(Get-LivingDescendantsInline $o)) {
                $di = Get-CimInstance Win32_Process -Filter "ProcessId=$d" -ErrorAction SilentlyContinue
                if (-not $di) { continue }
                if ($di.Name -match '^conhost\.exe$|^OpenConsole\.exe$') { continue }
                if (Stop-ProcessId $d 'orphan socket holder') { Write-Info "  port $port : killed descendant PID $d" }
            }
        }
        # wait until the listener is really gone (killed handles close async)
        $deadline = (Get-Date).AddSeconds($WaitSec)
        while ((Get-Date) -lt $deadline) {
            if (@(Get-PortOwnerPidsInline $port).Count -eq 0) {
                Write-Info "  port $port : released"
                break
            }
            Start-Sleep -Milliseconds 500
        }
    }
}

function Invoke-KillPytest {
    if ($DryRun) { return }
    foreach ($p in @(Get-PytestCandidates)) {
        $id = [int]$p.ProcessId
        if (Test-Protected $id) { continue }
        if (Stop-ProcessId $id 'zombie pytest / test worker') {
            Write-Info "  killed zombie pytest PID $id"
        }
    }
}

# --------------------------------------------------------------------------
#  Shutdown temp / lock / pytest-residue sweep (files only, never the DB)
# --------------------------------------------------------------------------
# PS 5.1 has no Get-ChildItem -Depth, and backend\data\projects grew to 5738+
# subdirectories - an unbounded recursive scan stalls Shutdown for MINUTES.
# So this walk is depth-limited (3 levels from each root) and never descends
# into the per-project data tree (nothing to sweep there anyway).
function Get-SweepFiles($base, [string[]]$patterns, $minAgeDays) {
    $found = @()
    $dirs = @($base)
    for ($depth = 0; $depth -le 2; $depth++) {
        $next = @()
        foreach ($d in $dirs) {
            foreach ($f in @(Get-ChildItem -Path $d -File -Force -ErrorAction SilentlyContinue)) {
                foreach ($pat in $patterns) {
                    if ($f.Name -like $pat) {
                        if ($minAgeDays -le 0 -or ((Get-Date) - $f.LastWriteTime).TotalDays -gt $minAgeDays) {
                            $found += $f
                        }
                        break
                    }
                }
            }
            foreach ($sub in @(Get-ChildItem -Path $d -Directory -Force -ErrorAction SilentlyContinue)) {
                if ($sub.Name -eq 'projects') { continue }  # 5738+ dirs, nothing to sweep
                $next += $sub.FullName
            }
        }
        $dirs = $next
    }
    return $found
}

function Invoke-TempSweep {
    # Time-bounded + depth-bounded sweep (see comment above). A stopwatch caps
    # the whole sweep so a residue explosion can never stall Shutdown forever.
    # Removal is best-effort: failures are common (antivirus controlled-folder
    # protection / an active handle holds the dir) and are AGGREGATED into one
    # line instead of one error line per item.
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $budgetSec = 30
    $roots = @(
        (Join-Path $root 'backend\data'),
        (Join-Path $root 'backend\data\uploads'),
        (Join-Path $root 'backend\data\_exports')
    )
    $removed = 0
    $failed = 0
    $failExamples = @()
    foreach ($base in $roots) {
        if ($sw.Elapsed.TotalSeconds -gt $budgetSec) { break }
        if (-not (Test-Path $base)) { continue }
        foreach ($f in @(Get-SweepFiles $base @('office-convert-*.ps1', '*.lock', '*.pid', '~$*') 0)) {
            try { Remove-Item -LiteralPath $f.FullName -Force -ErrorAction Stop; $removed++ }
            catch { $failed++; if ($failExamples.Count -lt 3) { $failExamples += $f.Name } }
        }
        # stale export temp (>1 day old), never touched while fresh
        foreach ($f in @(Get-SweepFiles $base @('*.tmp') 1)) {
            try { Remove-Item -LiteralPath $f.FullName -Force -ErrorAction Stop; $removed++ }
            catch { $failed++; if ($failExamples.Count -lt 3) { $failExamples += $f.Name } }
        }
    }
    # pytest .pt_* temp dirs: the pytest plugin creates them at the TOP LEVEL of
    # backend/ and chart-gap-filler/; scan only the top level (verified 2026-09-28).
    foreach ($base in @((Join-Path $root 'backend'), (Join-Path $root 'chart-gap-filler'))) {
        if (-not (Test-Path $base)) { continue }
        foreach ($d in @(Get-ChildItem -Path $base -Directory -Force -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -like '.pt_*' })) {
            if ($sw.Elapsed.TotalSeconds -gt $budgetSec) { break }
            try { Remove-Item -LiteralPath $d.FullName -Recurse -Force -ErrorAction Stop; $removed++ }
            catch { $failed++; if ($failExamples.Count -lt 3) { $failExamples += $d.Name } }
        }
    }
    if ($failed -gt 0) {
        Write-Info ("  temp sweep: {0} removed, {1} could not be removed (access denied / in use; e.g. {2})" -f `
            $removed, $failed, ($failExamples -join ', '))
    } else {
        Write-Info ("  temp sweep removed {0} item(s) in {1}s." -f $removed, [int]$sw.Elapsed.TotalSeconds)
    }
}

# --------------------------------------------------------------------------
#  main
# --------------------------------------------------------------------------
Write-Info "cleanup_guard: mode=$Mode root=$root"

$items = @(Get-Inventory)
# pre-clean pytest inventory costs a child powershell spawn (~2s); only the
# check/report modes need it. Cleaning modes enumerate pytest AFTER the sweep
# (verify step) instead - the sweep itself reports what it killed.
$pytest = @()
if ($Mode -eq 'Check' -or $DryRun) { $pytest = @(Get-PytestLeaks) }
$reportTypes = @()
foreach ($it in $items) { $reportTypes += $it.type }
if ($pytest.Count) { for ($i = 0; $i -lt $pytest.Count; $i++) { $reportTypes += 'pytest' } }

Write-Info "inventory: total=$($items.Count + $pytest.Count) $($reportTypes -join ' ')"

# ---- Check / DryRun: report only -------------------------------------------------
if ($Mode -eq 'Check' -or $DryRun) {
    foreach ($it in $items) { Write-InventoryRow $it }
    foreach ($pi in $pytest) { Write-Info ("  PID={0,-6} TYPE=pytest  | {1}" -f $pi.pid, $pi.cmd) }
    # a healthy service (listening on the toolbox ports) or a titled dev window
    # is NOT residue; report orphans, stragglers and stale stubs only
    $leftovers = @($items | Where-Object { $_.orphan -or ($_.stale -and -not $_.isService) })
    if (($leftovers.Count + $pytest.Count) -eq 0) {
        Write-Info 'cleanup_guard: nothing to clean.'
        exit 0
    }
    Write-Info ("cleanup_guard: {0} leftover item(s) would be cleaned." -f ($leftovers.Count + $pytest.Count))
    exit 2
}

# ---- Clean / Startup / Shutdown --------------------------------------------------
Write-Info 'cleanup_guard: cleaning...'

# 1. release the toolbox ports (kill_port walks orphan socket holders)
Write-Info '  [1] release ports'
Invoke-KillPort

# 2. close titled toolbox console windows FIRST (their /T tree kill gets
#    npm/vite children before they can re-open ports)
$windows = @($items | Where-Object { $_.type -eq 'window' })
foreach ($w in $windows) {
    if (Test-Protected $w.pid) { continue }
    Write-Info ("  [2] window PID {0} title '{1}'" -f $w.pid, $w.cmd)
    if (Stop-ProcessId $w.pid 'toolbox console window') { Write-Info '      killed' }
}

# 3. zombie pytest / test workers (pin logs/backend.log -> log rotation freeze)
Write-Info '  [3] zombie pytest'
if (-not $DryRun) { Invoke-KillPytest }

# 4. remaining toolkit processes: backend stragglers, frontend, stale stubs,
#    and orphans of any class
$killable = @($items | Where-Object { $_.type -ne 'window' -and ($_.stale -or $_.orphan) })
foreach ($it in $killable) {
    if (Test-Protected $it.pid) { continue }
    $why = $it.type
    if ($it.orphan) { $why = "$why/orphan" }
    Write-Info ("  [4] kill PID {0} ({1})" -f $it.pid, $why)
    if ($DryRun) {
        Write-Info "      [dry-run] skipped"
        continue
    }
    if (Stop-ProcessId $it.pid $why) { Write-Info '      killed' }
}

# 5. shutdown extra: sweep temp / lock / pytest .pt_* residue
if ($Mode -eq 'Shutdown' -and -not $SkipTempSweep) {
    Write-Info '  [5] temp / lock / pytest-residue sweep'
    Invoke-TempSweep
}

# ---- verify --------------------------------------------------------------
Start-Sleep -Milliseconds 500
$script:AllProcs = @(Get-CimInstance Win32_Process)
$script:AlivePids = @{}
foreach ($p in $script:AllProcs) { $script:AlivePids[[int]$p.ProcessId] = $true }
$remaining = @(Get-Inventory)
$pyLeft = @(Get-PytestLeaks)

if ($remaining.Count -eq 0 -and $pyLeft.Count -eq 0) {
    Write-Info 'cleanup_guard: DONE - nothing left running.'
    if ($script:Failures -gt 0) { exit 1 }
    exit 0
}
Write-Info ("cleanup_guard: {0} leftover item(s) REMAIN:" -f ($remaining.Count + $pyLeft.Count))
foreach ($it in $remaining) { Write-InventoryRow $it }
foreach ($pi in $pyLeft) { Write-Info ("  PID={0,-6} TYPE=pytest  | {1}" -f $pi.pid, $pi.cmd) }
exit 1