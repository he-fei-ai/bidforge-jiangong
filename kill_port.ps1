
# ============================================================================
#  kill_port.ps1 - release TCP listen ports reliably on Windows
#  ============================================================================
#  WHY THIS EXISTS
#  start_all.bat used to do: netstat -> taskkill /PID <owner> /T /F, then probe
#  again. That is not enough for "uvicorn --reload":
#    * the reloader PARENT creates the listen socket,
#    * the worker CHILD inherits a handle to that socket,
#    * when the parent is gone, netstat still reports the (dead) parent PID as
#      the socket owner, so taskkill fails with "process not found" while the
#      port keeps LISTENing, held by the orphaned worker.
#  The old script then aborted with "insufficient privileges / run as admin",
#  which is misleading: the real holder is a live child process.
#
#  STRATEGY
#    1. resolve current owner PIDs of the port
#    2. kill live owners with taskkill /T /F
#    3. if the port is still busy: walk the process tree downwards from the
#       reported owner (and from every process that names it as parent_pid)
#       and kill the living descendants that actually hold the socket handle
#    4. wait until the listener is really gone (up to -WaitSec seconds)
#
#  Exit code: 0 = every requested port is free, 1 = at least one still busy.
#
#  NOTE: keep all output ASCII-only. start_all.bat is saved as GBK/CP936 and
#  this file is invoked from it; non-ASCII here risks console mojibake.
# ============================================================================

param(
    [string] $Ports   = "8000,5175",
    [int]    $WaitSec = 8,
    [switch] $Quiet
)

$ErrorActionPreference = 'SilentlyContinue'

# Info goes straight to stdout: writing it with Write-Output would pollute the
# return value of Release-Port (every stream object gets collected into the
# result array, turning "$true/$false" into a string array).
function Write-Info($msg) {
    if (-not $Quiet) { [Console]::Out.WriteLine($msg) }
}

# PIDs we must never touch
$ProtectedPids = @(0, 4)

function Get-PortOwnerPids {
    param([int] $Port)
    $pids = @()
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen
    if ($conns) {
        $pids = @($conns | ForEach-Object { $_.OwningProcess })
    }
    if ($pids.Count -eq 0) {
        # Fallback: netstat works even when the CIM/GetNetTCPConnection stack
        # is unavailable or blocked on older systems.
        $needle = ":$Port "
        $lines = & netstat -ano | Select-String -SimpleMatch $needle |
                 Select-String -Pattern 'LISTENING'
        foreach ($l in $lines) {
            $cols = ($l.Line -split '\s+') | Where-Object { $_ }
            if ($cols.Count -ge 5) {
                $p = 0
                if ([int]::TryParse($cols[-1], [ref]$p)) { $pids += $p }
            }
        }
    }
    return @($pids | Where-Object { $_ -ne 0 } | Select-Object -Unique)
}

function Test-ProcessAlive {
    param([int] $ProcId)
    return [bool](Get-CimInstance Win32_Process -Filter "ProcessId=$ProcId")
}

# Living descendants of $RootPid (BFS over Win32_Process.ParentProcessId).
# Works even when $RootPid itself has already exited - children keep the
# InheritedFrom/ParentProcessId value, which is exactly the orphan case.
function Get-LivingDescendants {
    param([int] $RootPid)
    $found = @()
    $queue = New-Object System.Collections.Queue
    $queue.Enqueue($RootPid)
    $visited = @{}
    while ($queue.Count -gt 0) {
        $cur = $queue.Dequeue()
        if ($visited.ContainsKey($cur)) { continue }
        $visited[$cur] = $true
        $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$cur"
        foreach ($ch in $children) {
            if ($ProtectedPids -contains [int]$ch.ProcessId) { continue }
            $found += [int]$ch.ProcessId
            $queue.Enqueue([int]$ch.ProcessId)
        }
    }
    return @($found | Select-Object -Unique)
}

function Stop-Pid {
    param([int] $ProcId)
    if ($ProtectedPids -contains $ProcId) { return $false }
    & taskkill /PID $ProcId /T /F 2>&1 | Out-Null
    return (-not (Test-ProcessAlive -ProcId $ProcId))
}

function Release-Port {
    param([int] $Port)

    $owners = Get-PortOwnerPids -Port $Port
    if ($owners.Count -eq 0) {
        Write-Info "  port $Port : free"
        return $true
    }

    foreach ($o in $owners) {
        if (Test-ProcessAlive -ProcId $o) {
            if (Stop-Pid -ProcId $o) {
                Write-Info "  port $Port : killed PID $o"
            } else {
                Write-Info "  port $Port : FAILED to kill live PID $o (access denied?)"
            }
        } else {
            Write-Info "  port $Port : owner PID $o is already dead, looking for the process that inherited the socket handle..."
        }
        # Orphan sweep: kill living descendants of the reported owner. This is
        # the uvicorn --reload case (reloader dead, worker holds the socket).
        foreach ($d in (Get-LivingDescendants -RootPid $o)) {
            $di = Get-CimInstance Win32_Process -Filter "ProcessId=$d"
            $name = ''
            if ($di) { $name = $di.Name }
            # conhost.exe only inherits console handles, never the TCP socket;
            # skipping it avoids touching a console host that a live GUI app
            # might still share.
            if ($name -match '^conhost\.exe$|^OpenConsole\.exe$') { continue }
            $cmd = $di.CommandLine
            $tag = ''
            if ($cmd) { $tag = ($cmd -replace '\s+', ' ') }
            if ($tag.Length -gt 120) { $tag = $tag.Substring(0, 120) + '...' }
            if (Stop-Pid -ProcId $d) {
                Write-Info "  port $Port : killed descendant PID $d $tag"
            } else {
                Write-Info "  port $Port : FAILED to kill descendant PID $d $tag"
            }
        }
    }

    # Wait for the kernel to drop the listener (killed handles close async).
    $deadline = (Get-Date).AddSeconds($WaitSec)
    while ((Get-Date) -lt $deadline) {
        if ((Get-PortOwnerPids -Port $Port).Count -eq 0) {
            Write-Info "  port $Port : released"
            return $true
        }
        Start-Sleep -Milliseconds 500
    }

    $still = Get-PortOwnerPids -Port $Port
    if ($still.Count -eq 0) { return $true }
    Write-Info "  port $Port : STILL BUSY (owner PID(s): $($still -join ', '))"
    return $false
}

$ok = $true
foreach ($p in ($Ports -split ',')) {
    $port = 0
    if (-not [int]::TryParse($p.Trim(), [ref]$port)) { continue }
    if (-not (Release-Port -Port $port)) { $ok = $false }
}

if ($ok) { exit 0 } else { exit 1 }
