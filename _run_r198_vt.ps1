Set-Location "j:\编程\专项方案工具箱\frontend"
& "node_modules\.bin\vitest.CMD" run src/tests/useReviewAuditR198.test.tsx 2>&1 | Out-File -FilePath "j:\编程\专项方案工具箱\_r198_vt3.txt" -Encoding utf8
Write-Output ("EXIT=" + $LASTEXITCODE)
