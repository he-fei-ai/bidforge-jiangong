# ASCII-only script to fix conftest.py ACL using current working directory
$dir = Get-Location
$p = Join-Path $dir "tests\conftest.py"
Write-Host "Target: $p"
try {
    $content = [System.IO.File]::ReadAllText($p, [System.Text.Encoding]::UTF8)
    Write-Host "Read $($content.Length) chars"
    # Recreate with default ACL
    [System.IO.File]::WriteAllText($p, $content, (New-Object System.Text.UTF8Encoding $false))
    Write-Host "File recreated OK"
    $verify = [System.IO.File]::ReadAllBytes($p)
    Write-Host "Verify: $($verify.Length) bytes"
} catch {
    Write-Host "FAILED: $($_.Exception.Message)"
}
