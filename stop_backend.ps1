
# ============================================================================
#  stop_backend.ps1 - clean shutdown: kill the backend process tree, leftover
#  zombie pytest, and app-owned temp / lock / cache files.
#  ============================================================================
#  This is the "close the software -> auto cleanup" companion to
#  start_backend.ps1. Everything is best-effort: a failed step is LOGGED and
#  does NOT abort the run (the script still exits 0). The backend's own
#  lifespan shutdown also sweeps temp files (app/utils/process_cleanup.py);
#  this script covers the OS-level case where the process was hard-killed and
#  never got to run its own cleanup.
#
#  SAFETY: port-based kills reuse kill_port.ps1 (which walks orphan descendants
#  and refuses to touch PIDs 0/4); the pytest sweep (kill_zombie_pytest.ps1)
#  only matches python processes whose command line clearly references pytest
#  and never the caller/its ancestors. Temp sweep touches ONLY known,
#  app-owned patterns under backend\data\* - never the SQLite DB or WAL/SHM.
#
#  NOTE: keep output ASCII-only (see kill_port.ps1).
# ============================================================================

param(
    [int[]] $Ports = @(8000),
    [switch] $SweepTemp,
    [switch] $Quiet
)

$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition

function Log($msg) {
    if (-not $Quiet) { [Console]::Out.WriteLine("[stop_backend] $msg") }
}

# ---- 1. stop backend process tree (by port owner) -------------------------
Log "step 1/3: releasing backend port(s): $($Ports -join ',')"
try {
    $portArg = ($Ports -join ',')
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'kill_port.ps1') -Ports $portArg -WaitSec 8 -Quiet
    Log "  port(s) released."
} catch {
    Log "  [!] port release failed: $($_.Exception.Message)"   # logged, not fatal
}

# ---- 2. clean leftover zombie pytest / test workers -----------------------
Log "step 2/3: cleaning zombie pytest processes"
try {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'kill_zombie_pytest.ps1') -Quiet
} catch {
    Log "  [!] zombie pytest sweep failed: $($_.Exception.Message)"
}

# ---- 3. sweep app-owned temp / lock / cache -------------------------------
# -SweepTemp is ON by default for a full shutdown cleanup. It removes ONLY:
#   * legacy-Office temp converter scripts  (office-convert-*.ps1)
#   * stray lock/pid files                  (*.lock, *.pid)
#   * Word owner files                      (~$*)
#   * stale exported temp older than 1 day  (backend\data\_exports\*.docx.tmp)
# The SQLite database (*.db, -wal, -shm) is deliberately NEVER matched.
if ($SweepTemp) {
    Log "step 3/3: sweeping app-owned temp/lock/cache files"
    $patterns = @('office-convert-*.ps1', '*.lock', '*.pid', '~$*')
    $searchRoots = @(
        (Join-Path $root 'backend\data'),
        (Join-Path $root 'backend\data\uploads'),
        (Join-Path $root 'backend\data\_exports')
    )
    $now = Get-Date
    $removed = 0
    foreach ($base in $searchRoots) {
        if (-not (Test-Path $base)) { continue }
        foreach ($pat in $patterns) {
            Get-ChildItem -Path $base -Recurse -File -Filter $pat -ErrorAction SilentlyContinue | ForEach-Object {
                try {
                    Remove-Item -LiteralPath $_.FullName -Force -ErrorAction Stop
                    $removed++
                } catch {
                    Log "  [!] could not remove $($_.FullName): $($_.Exception.Message)"
                }
            }
        }
        # stale export temp (>1 day old .tmp), never touched while fresh
        Get-ChildItem -Path $base -Recurse -File -Filter '*.tmp' -ErrorAction SilentlyContinue | Where-Object {
            ($now - $_.LastWriteTime).TotalDays -gt 1
        } | ForEach-Object {
            try { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction Stop; $removed++ }
            catch { Log "  [!] could not remove $($_.FullName)" }
        }
    }
    Log "  temp sweep removed $removed item(s)."
} else {
    Log "step 3/3: temp sweep skipped (pass -SweepTemp to enable)."
}

Log "done."
exit 0
