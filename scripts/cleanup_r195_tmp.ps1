$ErrorActionPreference = 'SilentlyContinue'
foreach ($d in @('j:\编程\专项方案工具箱\backend\.tmp_r195', 'j:\编程\专项方案工具箱\backend\.tmp_r195b')) {
  if (Test-Path $d) { Remove-Item $d -Recurse -Force; Write-Output "removed $d" } else { Write-Output "absent $d" }
}
