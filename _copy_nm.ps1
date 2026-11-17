# copy_nm.ps1 - copy frontend node_modules with multithreading
$ErrorActionPreference = 'Continue'
function P([int[]]$c) { -join ($c | ForEach-Object { [char]$_ }) }
$zx = P @(0x7F16,0x7A0B)
$nm = P @(0x4E13,0x9879,0x65B9,0x6848,0x5DE5,0x5177,0x7BB1)
$nm2 = $nm + '2'
$src = Join-Path (Join-Path (Join-Path 'J:\' $zx) $nm) 'frontend\node_modules'
$dst = Join-Path (Join-Path (Join-Path (Join-Path 'J:\' $zx) $nm2) $nm) 'frontend\node_modules'
Write-Output "SRC=$src"
Write-Output "DST=$dst"
# /MT:32 multithread, /E include empty dirs, retry once
robocopy $src $dst /E /MT:32 /R:1 /W:1 /NFL /NDL /NJH /NJS /NP
Write-Output "[nm] robocopy exit=$LASTEXITCODE"
