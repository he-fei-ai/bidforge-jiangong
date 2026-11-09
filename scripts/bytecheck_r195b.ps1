$ErrorActionPreference = 'SilentlyContinue'
$files = @(
  'j:\编程\专项方案工具箱\backend\app\services\docx_math.py',
  'j:\编程\专项方案工具箱\backend\app\routers\_export_docx_parts.py',
  'j:\编程\专项方案工具箱\backend\app\routers\export.py',
  'j:\编程\专项方案工具箱\backend\tests\test_export_r195_20261109.py',
  'j:\编程\专项方案工具箱\frontend\src\utils\exportResponse.ts',
  'j:\编程\专项方案工具箱\frontend\src\tests\exportResponse.test.ts'
)
foreach ($f in $files) {
  $b = [System.IO.File]::ReadAllBytes($f)
  $text = [System.Text.Encoding]::UTF8.GetString($b)
  $crlf = ([regex]::Matches($text, "`r`n")).Count
  $lfOnly = ([regex]::Matches($text, "(?<!`r)`n")).Count
  $bom = ($b.Length -ge 3 -and $b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF)
  $fffd = ([regex]::Matches($text, [char]0xFFFD)).Count
  $kind = if ($lfOnly -eq 0) { 'CRLF' } elseif ($crlf -eq 0) { 'LF' } else { 'MIXED' }
  '{0}  {1}  BOM={2} FFFD={3}' -f (Split-Path $f -Leaf), $kind, $bom, $fffd
}
