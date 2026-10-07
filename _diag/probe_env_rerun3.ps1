# _diag/probe_env_rerun3.ps1
# Probe: Node/NVM environment & start_all.bat env check
$d = $PSScriptRoot
$root = Split-Path $PSScriptRoot -Parent
Write-Host '================ [1] _diag listing ================'
Get-ChildItem $d -Force | Select-Object FullName, Name, Length, LastWriteTime | Format-Table -AutoSize
Write-Host '================ [2] Node / NVM resolution ================'
Write-Host 'NVM_HOME = ' $env:NVM_HOME
Write-Host 'NVM_SYMLINK = ' $env:NVM_SYMLINK
Write-Host '--- Get-Command node --'
Get-Command node -ErrorAction SilentlyContinue | Select-Object Name, CommandType, Definition | Format-List -Force
Write-Host '--- node.exe under C:\nvm4w\nodejs --'
Get-ChildItem 'C:\nvm4w\nodejs' -Filter 'node.exe' -Recurse -ErrorAction SilentlyContinue | Select-Object FullName | Format-Table -AutoSize
Write-Host '--- node.exe under Program Files\nodejs --'
$q = (Get-Item env:ProgramFiles).Value
Get-ChildItem (Join-Path $q 'nodejs') -Filter 'node.exe' -Recurse -ErrorAction SilentlyContinue | Select-Object FullName | Format-Table -AutoSize
Write-Host '--- node --version (via full path) --'
$ex = Get-ChildItem 'C:\nvm4w\nodejs' -Filter 'node.exe' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
if ($ex) { & $ex.FullName --version } else { Write-Host 'node.exe not found' }
Write-Host '================ [3] commit-hash dir under J:\ ================'
Get-ChildItem 'J:\' -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^[0-9a-f]{40}$' } | Select-Object Name | Format-Table -AutoSize
Write-Host '================ [4] start_all.bat (first 50) ================'
Get-Content (Join-Path $root 'start_all.bat') -TotalCount 50
Write-Host '================ [5] HEAD commit check ================'
$head = Join-Path $root '.git\HEAD'
if (Test-Path $head) { Get-Content $head } else { Write-Host 'NO .git/HEAD' }
Write-Host '================ [6] PATH (first 300) ================'
$env:PATH.Substring(0, [Math]::Min(300, $env:PATH.Length))
