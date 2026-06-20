# run.ps1 -- one-click restart of the OCR tool (kill old instance, launch latest code)
#
# Why: the tool is single-instance (main.py exits silently if the mutex
# OCRTool_SingleInstance is already held). If an old instance is still running,
# `python main.py` is silently blocked and you keep seeing the OLD UI.
# This script kills only the python/pythonw process whose command line is THIS
# folder's main.py (won't touch your other python programs), then launches latest.
#
# Usage: right-click -> "Run with PowerShell", or in a terminal:  ./run.ps1
# (ASCII-only on purpose: avoids PowerShell 5.1 GBK/UTF-8 source-encoding pitfalls.)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$mainPy = Join-Path $root "main.py"
Write-Host "[run] project dir: $root"

# 1) Find and stop old instance(s) of THIS folder's main.py (matched by command line).
#    We launch with the ABSOLUTE main.py path (below), so the command line embeds $root
#    and this match is precise -- it won't touch other projects' main.py.
$killed = 0
Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match 'main\.py' -and $_.CommandLine -match [regex]::Escape($root) } |
    ForEach-Object {
        Write-Host ("[run] stopping old instance PID {0}" -f $_.ProcessId)
        try { Stop-Process -Id $_.ProcessId -Force; $killed++ } catch {}
    }
if ($killed -eq 0) { Write-Host "[run] no old instance; launching." }
else { Start-Sleep -Milliseconds 500 }

# 2) Launch latest code with pythonw (no console window); fall back to python.
#    Use the ABSOLUTE main.py path so future run.ps1 can find & kill this instance.
$pyw = Join-Path (Split-Path (Get-Command python).Source) "pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = (Get-Command python).Source }
Start-Process -FilePath $pyw -ArgumentList "`"$mainPy`"" -WorkingDirectory $root
Start-Sleep -Seconds 1

# 3) Report running instances
$ps = Get-Process python, pythonw -ErrorAction SilentlyContinue
Write-Host ("[run] launched. python/pythonw instances now: {0}" -f @($ps).Count)
$ps | Select-Object Id, ProcessName, StartTime | Format-Table -AutoSize
