# copy_runtime.ps1 - ASCII-only; builds Chinese paths from Unicode codepoints
$ErrorActionPreference = 'Continue'
function P([int[]]$c) { -join ($c | ForEach-Object { [char]$_ }) }
$zx = P @(0x7F16,0x7A0B)            # folder root name
$nm = P @(0x4E13,0x9879,0x65B9,0x6848,0x5DE5,0x5177,0x7BB1)  # project folder name
$nm2 = $nm + '2'
$yx = P @(0x8FD0,0x884C,0x8F6F,0x4EF6,0x2E,0x62ED)            # runtime launcher .bat
$src = Join-Path (Join-Path 'J:\' $zx) $nm
$dst = Join-Path (Join-Path (Join-Path 'J:\' $zx) $nm2) $nm
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Write-Output "SRC=$src"
Write-Output "DST=$dst"

# ---- backend ----
$ex = @('artifacts','tests','.pytest_cache','.ruff_cache','.mypy_cache','__pycache__',
        '.codebuddy','.pyc_r252','data_on_J_backup_20261103','_baseline_run','_diagnostics','data')
foreach ($p in (Get-ChildItem (Join-Path $src 'backend') -Force)) {
    $n = $p.Name
    if ($ex -contains $n) { continue }
    if ($n -like '.pytest_*' -or $n -like '.tmp_*' -or $n -like '.r249*' -or $n -like '.r113*' -or
        $n -like '_r1*' -or $n -like '_cmp*' -or $n -like '_scan*' -or $n -like '_probe*' -or
        $n -like '_mut*' -or $n -like '_eol*' -or $n -like '_after*' -or $n -like '_baseline*' -or
        $n -like '.after*' -or $n -like '.baseline*' -or $n -like '.full*' -or
        $n -like '*.log' -or $n -like '*.txt') { continue }
    robocopy (Join-Path (Join-Path $src 'backend') $n) (Join-Path (Join-Path $dst 'backend') $n) /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
}
$bf = @('requirements.txt','requirements-dev.txt','requirements-ocr.txt','requirements-windows.txt',
        'pyproject.toml','pytest.ini','ruff.toml','preflight_check.py','.env','.env.example',
        '.python-version','.gitignore','check_acl.py','scan_zero.json')
foreach ($f in $bf) {
    $s = Join-Path (Join-Path $src 'backend') $f
    if (Test-Path $s) { Copy-Item $s (Join-Path (Join-Path $dst 'backend') $f) -Force }
}
Write-Output '[backend] copied'

# ---- frontend ----
foreach ($d in @('src','node_modules','logs')) {
    $s = Join-Path (Join-Path $src 'frontend') $d
    if (Test-Path $s) {
        robocopy $s (Join-Path (Join-Path $dst 'frontend') $d) /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
    }
}
$fe = @('index.html','package.json','package-lock.json','pnpm-lock.yaml','pnpm-workspace.yaml',
        'tsconfig.json','vite.config.ts','vitest.config.ts','eslint.config.js',
        '.prettierrc.json','.prettierignore','.env.example')
foreach ($f in $fe) {
    $s = Join-Path (Join-Path $src 'frontend') $f
    if (Test-Path $s) { Copy-Item $s (Join-Path (Join-Path $dst 'frontend') $f) -Force }
}
Write-Output '[frontend] copied'

# ---- root helper scripts + launcher ----
foreach ($f in @('stop_all.bat','start_backend.ps1','stop_backend.ps1','kill_port.ps1',
                 'kill_zombie_pytest.ps1','cleanup_guard.ps1',$yx)) {
    $s = Join-Path $src $f
    if (Test-Path $s) { Copy-Item $s (Join-Path $dst $f) -Force }
}
Write-Output '[root] copied'

# ---- backend\data junction -> external data volume (do NOT copy data) ----
$dataDst = Join-Path (Join-Path $dst 'backend') 'data'
if (-not (Test-Path $dataDst)) {
    cmd /c mklink /J "$dataDst" "D:\BidForgeData" | Out-Null
    if (Test-Path $dataDst) { Write-Output '[data] junction -> D:\BidForgeData created' }
    else { Write-Output '[data] WARN junction NOT created; run: mklink /J "backend\data" D:\BidForgeData' }
} else {
    Write-Output '[data] junction already present'
}
Write-Output '[done]'
