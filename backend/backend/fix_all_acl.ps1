# ASCII-only PowerShell script to fix broken ACLs on all .py files in tests/
# Uses Get-Content (can bypass broken ACL) to read, .NET Delete to remove, then writes new file
$dir = Get-Location
$testsDir = Join-Path $dir "tests"
$files = Get-ChildItem -Path $testsDir -Filter "*.py" -Recurse
$fixed = 0
$failed = 0
$alreadyOk = 0

foreach ($f in $files) {
    $p = $f.FullName
    # Check if file is readable via .NET (Python-compatible)
    $readable = $false
    try {
        $null = [System.IO.File]::ReadAllBytes($p)
        $readable = $true
    } catch {
        $readable = $false
    }
    
    if ($readable) {
        $alreadyOk++
        continue
    }
    
    # File is broken - fix it
    try {
        # Read content via PowerShell Get-Content (bypasses broken ACL)
        $content = Get-Content -LiteralPath $p -Raw -Encoding UTF8
        if ($null -eq $content -or $content.Length -eq 0) {
            # Try line-by-line
            $lines = Get-Content -LiteralPath $p -Encoding UTF8
            $content = $lines -join "`r`n"
        }
        if ($null -ne $content -and $content.Length -gt 0) {
            # Delete via .NET (can bypass ACL for delete)
            [System.IO.File]::Delete($p)
            # Write new file with default ACL
            [System.IO.File]::WriteAllText($p, $content, (New-Object System.Text.UTF8Encoding $false))
            $fixed++
        } else {
            $failed++
            Write-Host "EMPTY: $p"
        }
    } catch {
        $failed++
        Write-Host "FAILED: $p - $($_.Exception.Message)"
    }
}

Write-Host ""
Write-Host "Already OK: $alreadyOk"
Write-Host "Fixed: $fixed"
Write-Host "Failed: $failed"
Write-Host "Total: $($files.Count)"
