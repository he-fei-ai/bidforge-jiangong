# copy_runtime2.ps1 - finish remaining small files + data junction (no node_modules)
$ErrorActionPreference = 'Continue'
function P([int[]]$c) { -join ($c | ForEach-Object { [char]$_ }) }
$zx = P @(0x7F16,0x7A0B)
$nm = P @(0x4E13,0x9879,0x65B9,0x6848,0x5DE5,0x5177,0x7BB1)
$nm2 = $nm + '2'
$yx = P @(0x8FD0,0x884C,0x8F6F,0x4EF6,0x2E,0x62ED)
$src = Join-Path (Join-Path 'J:\' $zx) $nm
$dst = Join-Path (Join-Path (Join-Path 'J:\' $zx) $nm2) $nm

# frontend config files
$fe = @('index.html','package.json','package-lock.json','pnpm-lock.yaml','pnpm-workspace.yaml',
        'tsconfig.json','vite.config.ts','vitest.config.ts','eslint.config.js',
        '.prettierrc.json','.prettierignore','.env.example')
foreach ($f in $fe) {
    $s = Join-Path (Join-Path $src 'frontend') $f
    if (Test-Path $s) { Copy-Item $s (Join-Path (Join-Path $dst 'frontend') $f) -Force }
}
Write-Output '[frontend-config] done'

# frontend logs dir (small)
$fl = Join-Path (Join-Path $src 'frontend') 'logs'
if (Test-Path $fl) { robocopy $fl (Join-Path (Join-Path $dst 'frontend') 'logs') /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null }
Write-Output '[frontend-logs] done'

# root helper scripts + launcher
foreach ($f in @('stop_all.bat','start_backend.ps1','stop_backend.ps1','kill_port.ps1',
                 'kill_zombie_pytest.ps1','cleanup_guard.ps1',$yx)) {
    $s = Join-Path $src $f
    if (Test-Path $s) { Copy-Item $s (Join-Path $dst $f) -Force }
}
Write-Output '[root] done'

# backend\data junction
$dataDst = Join-Path (Join-Path $dst 'backend') 'data'
if (-not (Test-Path $dataDst)) {
    cmd /c mklink /J "$dataDst" "D:\BidForgeData" | Out-Null
    if (Test-Path $dataDst) { Write-Output '[data] junction -> D:\BidForgeData created' }
    else { Write-Output '[data] WARN junction NOT created' }
} else {
    Write-Output '[data] junction present'
}
Write-Output '[done]'
