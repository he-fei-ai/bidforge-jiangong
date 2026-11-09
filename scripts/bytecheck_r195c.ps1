$ErrorActionPreference = 'SilentlyContinue'
$files = @(
  'j:\编程\专项方案工具箱\frontend\src\main.tsx',
  'j:\编程\专项方案工具箱\frontend\src\App.tsx',
  'j:\编程\专项方案工具箱\frontend\src\utils\exportResponse.ts',
  'j:\编程\专项方案工具箱\frontend\src\tests\exportResponse.test.ts',
  'j:\编程\专项方案工具箱\frontend\src\components\review\DossierExportCard.tsx',
  'j:\编程\专项方案工具箱\frontend\src\pages\schemeWorkbench\tabs\ExportTab.tsx'
)
foreach ($f in $files) {
  $b = [System.IO.File]::ReadAllBytes($f)
  $text = [System.Text.Encoding]::UTF8.GetString($b)
  $crlf = ([regex]::Matches($text, "`r`n")).Count
  $lfOnly = ([regex]::Matches($text, "(?<!`r)`n")).Count
  $kind = if ($lfOnly -eq 0) { 'CRLF' } elseif ($crlf -eq 0) { 'LF' } else { 'MIXED' }
  '{0}  {1}' -f (Split-Path $f -Leaf), $kind
}
