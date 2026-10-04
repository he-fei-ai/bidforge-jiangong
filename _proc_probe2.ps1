$out = 'j:\_proc_probe_out.txt'
"listeners on 8000:" | Out-File $out -Encoding utf8
Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
  Select-Object LocalPort, OwningProcess | Format-Table -AutoSize | Out-String -Width 120 | Out-File $out -Append -Encoding utf8
"python/uvicorn processes:" | Out-File $out -Append -Encoding utf8
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='uvicorn.exe'" |
  Select-Object ProcessId, @{n='Cmd';e={$_.CommandLine.Substring(0, [Math]::Min(160, $_.CommandLine.Length))}} |
  Format-List | Out-String -Width 200 | Out-File $out -Append -Encoding utf8
