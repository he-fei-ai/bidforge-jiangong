# _diag/probe_env_rerun.ps1
# Re-run probe: list _diag files and dump start_all.bat
$files = @{}
if (Test-Path $PSScriptRoot) { Get-ChildItem $PSScriptRoot | ForEach-Object { $files[$_.Name] = $_.Length } } else { $files['_diag'] = 'MISSING' }
$root = Split-Path $PSScriptRoot -Parent
$probe = Join-Path $root 'start_all.bat'
$probeLines = @()
if (Test-Path $probe) { Get-Content $probe | ForEach-Object { $probeLines += $_ } }
Write-Output '== _diag files =='
$files.GetEnumerator() | Sort-Object Name | ForEach-Object { Write-Output ("{0} : {1}" -f $_.Name, $_.Value) }
Write-Output '== start_all.bat =='
$probeLines | ForEach-Object { Write-Output $_ }
