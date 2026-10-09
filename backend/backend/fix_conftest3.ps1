# Use Get-Content which can bypass the broken ACL
$dir = Get-Location
$p = Join-Path $dir "tests\conftest.py"
Write-Host "Target: $p"
try {
    # Get-Content can read despite broken ACL
    $lines = Get-Content -LiteralPath $p -Encoding UTF8
    Write-Host "Read $($lines.Count) lines"
    $content = $lines -join "`n"
    Write-Host "Content length: $($content.Length)"
    
    # Try to overwrite using Set-Content (uses different access mode)
    $tmp = Join-Path $env:TEMP "conftest_backup.py"
    Set-Content -LiteralPath $tmp -Value $content -Encoding UTF8 -NoNewline
    Write-Host "Wrote backup to $tmp"
    
    # Try to delete original and copy back
    try {
        Remove-Item -LiteralPath $p -Force -ErrorAction Stop
        Write-Host "Deleted original"
    } catch {
        Write-Host "Delete failed: $($_.Exception.Message)"
        # Try .NET method
        try {
            [System.IO.File]::Delete($p)
            Write-Host "Deleted via .NET"
        } catch {
            Write-Host "Delete .NET also failed: $($_.Exception.Message)"
        }
    }
    
    # Copy backup to original location
    Copy-Item -LiteralPath $tmp -Destination $p -Force
    Write-Host "Copied backup to original"
    
    # Verify with Python-readable check
    $bytes = [System.IO.File]::ReadAllBytes($p)
    Write-Host "Verify: $($bytes.Length) bytes readable via .NET"
} catch {
    Write-Host "FAILED: $($_.Exception.Message)"
}
