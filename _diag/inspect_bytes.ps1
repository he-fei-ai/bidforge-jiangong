# _diag/inspect_bytes.ps1
# Inspect start_all_bat.bytes: length, encoding detection, hex dump, sample decode
$ErrorActionPreference = 'Stop'
$bytes = [System.IO.File]::ReadAllBytes((Join-Path $PSScriptRoot 'start_all_bat.bytes'))
Write-Host "LEN=$($bytes.Length)"
$hex32 = -join ($bytes[0..([Math]::Min(31, $bytes.Length-1))] | ForEach-Object { $_.ToString('x2') })
Write-Host "FIRST32=$hex32"
$utf8 = $null
try { [System.Text.Encoding]::UTF8.GetString($bytes,0,$bytes.Length); $utf8 = $true } catch { $utf8 = $false }
Write-Host "UTF8_VALID=$utf8"
$gbk = $null
try { [System.Text.Encoding]::GetEncoding('GB18030').GetString($bytes,0,$bytes.Length); $gbk = $true } catch { $gbk = $false }
Write-Host "GB18030_VALID=$gbk"
$cp = $null
try { [System.Text.Encoding]::GetEncoding('CP936').GetString($bytes,0,$bytes.Length); $cp = $true } catch { $cp = $false }
Write-Host "CP936_VALID=$cp"
$nonAscii = -1
for ($i=0; $i -lt $bytes.Length; $i++) { if ($bytes[$i] -gt 127) { $nonAscii=$i; break } }
if ($nonAscii -ge 0) { Write-Host "FIRST_NONASCII=$nonAscii" } else { Write-Host 'FIRST_NONASCII=NONE' }
$cr = 0; $lf = 0
for ($i=0; $i -lt $bytes.Length; $i++) { if ($bytes[$i] -eq 13) { $cr++ } elseif ($bytes[$i] -eq 10) { $lf++ } }
Write-Host "CR=$cr LF=$lf"
$sampleLen = [Math]::Min(2048, $bytes.Length)
$sample = [System.Text.Encoding]::UTF8.GetString($bytes, 0, $sampleLen)
$base64 = ($sample -match '[A-Za-z0-9+/=]{80,}')
Write-Host "B64_LIKE=$base64"
$encs = @('UTF8','GB18030','CP936','Latin1')
foreach ($e in $encs) {
  try {
    $t = switch ($e) {
      'UTF8' { [System.Text.Encoding]::UTF8.GetString($bytes) }
      'GB18030' { [System.Text.Encoding]::GetEncoding('GB18030').GetString($bytes) }
      'CP936' { [System.Text.Encoding]::GetEncoding('CP936').GetString($bytes) }
      'Latin1' { [System.Text.Encoding]::GetEncoding('Latin1').GetString($bytes) }
    }
    Write-Host "\n--$e--"
    Write-Host $t.Substring(0, [Math]::Min(200,$t.Length))
  } catch {
    Write-Host "\n--$e--ERR $_"
  }
}
$test = $null
try { $test = [System.Text.Encoding]::UTF8.GetString($bytes) } catch {}
if ($test) {
  $first = $test.Substring(0, [Math]::Min(500,$test.Length))
  $outPath = Join-Path $PSScriptRoot 'inspect_utf8.txt'
  $first | Out-File -Encoding UTF8 $outPath
  Write-Host "Wrote $outPath"
}
