$ErrorActionPreference = 'Stop'
Set-Location 'j:\编程\专项方案工具箱\backend'
$files = (Get-Content 'j:\编程\专项方案工具箱\docs\_r195_cluster_files.txt' -Encoding UTF8) + @('tests/test_export_r195_20261109.py')
$files = $files | Where-Object { $_ -and $_.Trim() }
Write-Output ("N=" + $files.Count)
python -m pytest @files --tb=short -q -p no:cacheprovider > 'j:\编程\专项方案工具箱\docs\_r195_final_cluster.txt' 2>&1
Write-Output ("EXIT=" + $LASTEXITCODE)
