@echo off
REM One-time: puts "Update Session Keys" on your Desktop (works with a OneDrive Desktop too).
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Update Session Keys.lnk'));" ^
 "$s.TargetPath='%~dp0Update-SessionKeys.cmd'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='imageres.dll,77'; $s.Description='Put today''s Breeze session key into VAJRA 2.0, MYTHOS 2.0 and ApexOptions'; $s.Save();" ^
 "Write-Host ('Shortcut created on ' + $d)"
pause
