# _diag/rebuild_probe.ps1
# 1) Decode start_all_bat.bytes -> decoded_bat.bat
$src = Join-Path $PSScriptRoot 'start_all_bat.bytes'
$dst = Join-Path $PSScriptRoot 'decoded_bat.bat'
try {
    $bytes = [System.IO.File]::ReadAllBytes($src)
    [System.IO.File]::WriteAllBytes($dst, $bytes)
    Write-Host 'decoded_bat.bat bytes: ' $bytes.Length
} catch {
    Write-Host 'DECODE_ERROR: ' $_.Exception.Message
    exit 1
}
# 2) Run probe_env.ps1 and capture output
$probe = Join-Path $PSScriptRoot 'probe_env.ps1'
$out = Join-Path $PSScriptRoot 'probe_env_output.txt'
try {
    [System.IO.File]::WriteAllText($out, '', [System.Text.Encoding]::UTF8)
    $proc = [System.Diagnostics.Process]::Start($probe)
    $proc.WaitForExit(600000)
    $raw = [System.IO.File]::ReadAllText($out, [System.Text.Encoding]::UTF8)
    $lines = $raw -split '\r?\n'
    $kept = 0
    foreach ($ln in $lines) {
        $s = $ln.Trim()
        if ($s -eq '') { continue }
        $kept++
    }
    Write-Host 'probe_env lines kept: ' $kept
} catch {
    Write-Host 'PROBE_RUN_ERROR: ' $_.Exception.Message
}
# 3) Run rerun probes
$probes = @((Join-Path $PSScriptRoot 'probe_env_rerun.ps1'),(Join-Path $PSScriptRoot 'probe_env_rerun3.ps1'))
foreach ($p in $probes) {
    if (-not (Test-Path $p)) { Write-Host 'MISSING: ' $p; continue }
    $outFile = $p -replace '\.ps1$', '_output.txt'
    try {
        $proc2 = [System.Diagnostics.Process]::Start($p)
        $proc2.WaitForExit(600000)
        if (Test-Path $outFile) {
            $raw2 = [System.IO.File]::ReadAllText($outFile, [System.Text.Encoding]::UTF8)
            $lines2 = $raw2 -split '\r?\n'
            $kept2 = @($lines2 | Where-Object { $_.Trim() -ne '' }).Count
            Write-Host 'rerun kept: ' $kept2
        }
    } catch { Write-Host 'RERUN_ERROR: ' $_.Exception.Message }
}
Write-Host 'done'
