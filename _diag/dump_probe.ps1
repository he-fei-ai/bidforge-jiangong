# _diag/dump_probe.ps1
# Dump start_all_bat.bytes analysis to a temp file
$path = Join-Path $PSScriptRoot 'start_all_bat.bytes'
$b = [System.IO.File]::ReadAllBytes($path)
$enc = [System.Text.Encoding]::UTF8
$text = $enc.GetString($b)
$gbk = [System.Text.Encoding]::GetEncoding('GBK')
$lines = @()
$lines += "bytes=$($b.Length)"
$lines += "headUTF8=$($enc.GetString($b,0,64).Replace([char]10,'|').Replace([char]13,'|'))"
$lines += "headGBK=$($gbk.GetString($b,0,64).Replace([char]10,'|').Replace([char]13,'|'))"
$lines += "allAscii=$( ($b | Where-Object { $_ -ge 128 }).Length -eq 0 )"
$lines += "startsCodeOf=$($text.StartsWith('Code of'))"
$lines += "isValidUTF8=$(try { [System.Text.Encoding]::UTF8.GetCharCount($b,0,$b.Length); $true } catch { $false })"
$lines += "hasBase64Like=$((Select-String -SimpleMatch -Pattern '[A-Za-z0-9+/]{40,}' -InputObject $text).Count)"
$lines += "hasConvertToSecureString=$($text -imatch 'ConvertTo-SecureString')"
$lines += "hasFromBase64=$($text -imatch 'FromBase64')"
$tmp = [System.IO.Path]::GetTempFileName()
[System.IO.File]::WriteAllText($tmp, ($lines -join [System.Environment]::NewLine), [System.Text.Encoding]::UTF8)
Write-Output "TMP=$tmp"
