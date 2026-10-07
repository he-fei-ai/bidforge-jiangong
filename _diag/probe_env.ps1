# _diag/probe_env.ps1
# Environment probe: Node.js / Python / PATH / workspace
$ErrorActionPreference = "Continue"
$LogFile = Join-Path $PSScriptRoot 'probe_env.log'
function Write-Log(["string"]$Msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Msg"
    try { Add-Content -Path $LogFile -Value $line -Encoding UTF8 } catch { Add-Content -Path $LogFile -Value $line }
    Write-Host $line
}
Write-Log "=== probe_env start ==="
try { $nodeVer = node --version 2>&1; $nodeRc = $LASTEXITCODE } catch { $nodeVer = "exception: $_"; $nodeRc = -1 }
Write-Log "node --version => exit=$nodeRc output=$nodeVer"
Write-Log "node location => $(Get-Command node -ErrorAction SilentlyContinue | Select-Object -First 1 | Format-List -LiteralPath * | Out-String).Trim()"
$nvmHome = $env:NVM_HOME; $nvmSymlink = $env:NVM_SYMLINK
Write-Log "NVM_HOME => $($nvmHome -join [Environment]::NewLine)"
Write-Log "NVM_SYMLINK => $($nvmSymlink -join [Environment]::NewLine)"
if ($nvmHome) { if (Test-Path $nvmHome) { Write-Log "NVM_HOME exists"; Get-ChildItem -Path $nvmHome -ErrorAction SilentlyContinue | ForEach-Object { Write-Log "  NVM dir: $($_.Name)" } } else { Write-Log "NVM_HOME does NOT exist" } }
if ($nvmSymlink) { if (Test-Path $nvmSymlink) { Write-Log "NVM_SYMLINK exists" } else { Write-Log "NVM_SYMLINK does NOT exist" } }
try { $pyVer = python --version 2>&1; $pyRc = $LASTEXITCODE } catch { $pyVer = "exception: $_"; $pyRc = -1 }
Write-Log "python --version => exit=$pyRc output=$pyVer"
try { $pyPath = (Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1).Path; Write-Log "python path => $pyPath" } catch { Write-Log "python path => not found" }
$root = Split-Path $PSScriptRoot -Parent
Write-Log "CWD => $PWD"
Write-Log "workdir => $root"
Write-Log "workdir exists => $(Test-Path $root)"
Write-Log "start_all.bat exists => $(Test-Path (Join-Path $root 'start_all.bat'))"
$nodeDirs = @("C:\Program Files\nodejs","C:\Program Files (x86)\nodejs","C:\nvm4w\nodejs","C:\nvm4w\tools","C:\nvm4w\libs")
Write-Log "Node candidate dirs:"
foreach ($d in $nodeDirs) { if (Test-Path $d) { $f = Get-ChildItem -Path $d -Filter "node.exe" -ErrorAction SilentlyContinue; if ($f) { Write-Log "  [$d] HAS node.exe" } else { Write-Log "  [$d] exists, no node.exe" } } else { Write-Log "  [$d] does not exist" } }
try { $gitVer = git --version 2>&1; $gitRc = $LASTEXITCODE } catch { $gitVer = "exception: $_"; $gitRc = -1 }
Write-Log "git --version => exit=$gitRc output=$gitVer"
Write-Log "=== probe_env end ==="
exit 0
