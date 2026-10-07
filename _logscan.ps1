$files = @("backend.log","backend.log.1","backend.log.3") | ForEach-Object { "j:\编程\专项方案工具箱\logs\$_" }
$pattern = '^\S+ \S+ \[WARNING\] ([^:]+): (.*)$'
function Norm($m){
  $m = $m -replace "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>"
  $m = $m -replace "[0-9a-f]{16,}", "<hex>"
  $m = $m -replace "\d+(\.\d+)?", "<n>"
  $m = $m -replace "'[^']*'", "'<x>'"
  return $m
}
$rows = foreach($f in $files){ foreach($l in Get-Content $f){ if($l -match $pattern){ [PSCustomObject]@{Mod=$matches[1].Trim(); Pat=(Norm $matches[2])} } }
$rows | Group-Object Mod,Pat | Sort-Object Count -Descending | Select-Object -Skip 40 -First 50 Count,Name | Format-Table -AutoSize | Out-String -Width 220
