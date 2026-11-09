$ErrorActionPreference = 'SilentlyContinue'
$files = @(
  'j:\编程\专项方案工具箱\docs\_r195_baseline_backend.txt',
  'j:\编程\专项方案工具箱\backend\app\routers\_chart_pipeline.py',
  'j:\编程\专项方案工具箱\backend\app\services\content_blocks.py',
  'j:\编程\专项方案工具箱\backend\app\services\content_utils.py',
  'j:\编程\专项方案工具箱\backend\app\services\ai\image_engine.py',
  'j:\编程\专项方案工具箱\backend\app\services\content_plan.py'
)
foreach ($f in $files) { $i = Get-Item $f; '{0}  {1}' -f $i.LastWriteTime.ToString('MM-dd HH:mm:ss'), $i.Name }
Write-Output ''
Get-CimInstance Win32_Process -Filter "Name like 'python%'" | ForEach-Object { '{0}  {1}' -f $_.ProcessId, $_.CommandLine.Substring(0, [Math]::Min(120, $_.CommandLine.Length)) }
