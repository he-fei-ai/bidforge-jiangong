@echo off
REM ============================================================================
REM  stop_all.bat - clean shutdown of the toolbox (backend + frontend + all
REM  leftover processes + temp residue). Complements start_all.bat.
REM
REM  ASCII-only on purpose: .bat files in this repo are saved with different
REM  encodings (start_all.bat is GBK/CP936); keeping this file pure ASCII
REM  removes any code-page dependency. Chinese instructions live in
REM  docs\process_guard_mechanism.md.
REM
REM  What it does (delegated to cleanup_guard.ps1 -Mode Shutdown):
REM    1. release ports 8000 / 5175 (incl. orphan socket holders)
REM    2. close titled toolbox console windows (front-/back-...toolbox)
REM    3. sweep zombie pytest / test-worker processes (log-rotation pinner)
REM    4. kill leftover backend/frontend/script/timeout stubs & orphans
REM    5. remove app-owned temp / lock / office-convert / ~$* / stale .tmp
REM ============================================================================
title Stop Toolbox
echo.
echo ============================================
echo   Toolbox shutdown: backend / frontend /
echo   zombie processes / timeout leftovers
echo ============================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0cleanup_guard.ps1" -Mode Shutdown
set "GUARD_EXIT=%errorlevel%"
echo.
if "%GUARD_EXIT%"=="0" (
    echo [OK] Toolbox fully stopped and cleaned - you may close all windows.
) else (
    echo [!] Some leftover process could not be removed ^(errorlevel %GUARD_EXIT%^).
    echo     Inspect the output above, or retry from an elevated shell.
    echo     Tip:  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0cleanup_guard.ps1" -Mode Shutdown
)
echo.
pause