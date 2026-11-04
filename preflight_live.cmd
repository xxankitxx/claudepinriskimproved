@echo off
REM runs the full preflight INCLUDING the live Breeze smoke test with the session key given on 17 Oct (keys expire daily - edit for a new day)
call "%~dp0preflight.cmd" 57384442
