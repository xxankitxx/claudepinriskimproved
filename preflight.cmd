@echo off
REM VAJRA 2.0.1 preflight: runs every check on THIS machine and writes preflight_report.txt
REM usage:  preflight.cmd <today's Breeze session key>     (double-click = no smoke test)
setlocal
cd /d "%~dp0"
set LOG=preflight_report.txt
echo VAJRA 2.0.1 PREFLIGHT  %DATE% %TIME% > "%LOG%"
echo folder: %CD% >> "%LOG%"
echo. >> "%LOG%"
echo [1] python >> "%LOG%"
python --version >> "%LOG%" 2>&1
python -c "import sys; print(sys.executable)" >> "%LOG%" 2>&1
echo. >> "%LOG%"
echo [2] packages >> "%LOG%"
python -c "import pyarrow, numpy, yaml, fastapi, uvicorn, breeze_connect; print('pyarrow', pyarrow.__version__, '| numpy', numpy.__version__, '| fastapi', fastapi.__version__, '| uvicorn', uvicorn.__version__, '| breeze_connect OK')" >> "%LOG%" 2>&1
python -c "import pytest; print('pytest', pytest.__version__)" >> "%LOG%" 2>&1
echo. >> "%LOG%"
echo [3] files >> "%LOG%"
if exist vajra\credentials.py (echo credentials.py: present >> "%LOG%") else (echo credentials.py: MISSING - copy from D:\ClaudePinRisk\vajra >> "%LOG%")
if exist tapes\2026-10-16\tick_tape.parquet (echo tapes\2026-10-16: present >> "%LOG%") else (echo tapes\2026-10-16: MISSING - copy from D:\ClaudePinRisk\tapes for the pre-open level seed >> "%LOG%")
if exist vajra\.token (echo WARNING: vajra\.token exists and would override -Token >> "%LOG%")
for /f "tokens=3" %%a in ('dir /-c D:\ ^| findstr /c:"bytes free"') do set FREE=%%a
echo free bytes on D: %FREE% >> "%LOG%"
echo. >> "%LOG%"
echo [4] completion gate (verify_vajra.py) >> "%LOG%"
python verify_vajra.py >> "%LOG%" 2>&1
echo verify exit code: %ERRORLEVEL% >> "%LOG%"
echo. >> "%LOG%"
echo [5] test suite >> "%LOG%"
python -m pytest -q -p no:cacheprovider tests >> "%LOG%" 2>&1
echo pytest exit code: %ERRORLEVEL% >> "%LOG%"
echo. >> "%LOG%"
echo [6] launcher parse check >> "%LOG%"
powershell -NoProfile -Command "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path run_vajra.ps1).Path,[ref]$null,[ref]$e); 'run_vajra.ps1 parse errors: ' + $e.Count; $e | %% { $_.Message }" >> "%LOG%" 2>&1
echo. >> "%LOG%"
if "%~1"=="" (
  echo [7] live smoke test: SKIPPED (no session key given) >> "%LOG%"
) else (
  echo [7] live smoke test (Breeze session + websocket + subscriptions, 60 s) >> "%LOG%"
  python -m vajra.app --smoke 60 --token %~1 >> "%LOG%" 2>&1
  echo smoke exit code: %ERRORLEVEL% >> "%LOG%"
)
echo. >> "%LOG%"
echo PREFLIGHT DONE >> "%LOG%"
type "%LOG%"
echo.
echo Report written to %CD%\%LOG%
pause
