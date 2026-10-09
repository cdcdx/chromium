@echo off
rem backup.bat - Windows (cmd) entry point; forwards every argument to backup.ps1
rem
rem Usage:
rem   backup.bat --ver 154.0.8037.21
rem   backup.bat --os win --ver 154.0.8037.21 --patches D:\backups\patches
rem   backup.bat --os android --ver 154.0.8037.21 --num 3 --patches D:\backups\patches
rem   backup.bat --base <commit-sha> --src D:\src\chromium\src --output D:\backups\patches
rem   backup.bat --ver 154.0.8037.21 --tracked-only
rem   backup.bat --ver 154.0.8037.21 --dry-run
rem   backup.bat -h
rem
rem Why this wrapper: cmd cannot execute .ps1 directly (".PS1" is missing from
rem PATHEXT and the extension has no file association), so typing ".\backup.ps1"
rem in a cmd or SSH session does nothing at all - no output, no error.
rem
rem Unlike build.bat this wrapper does NOT pushd to the script directory:
rem backup.py resolves a relative --src / --patches / --output against the
rem caller's current directory, so switching directories here would silently
rem move the backup somewhere else.
rem
rem Env: BACKUP_PS_SHELL - PowerShell executable to use
rem      (default: Windows PowerShell 5.1, which backup.ps1 targets)
setlocal EnableExtensions

set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if defined BACKUP_PS_SHELL set "PS=%BACKUP_PS_SHELL%"

"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0backup.ps1" %*

rem Propagate the real exit code (cmd / SSH / CI callers rely on it).
set "CODE=%ERRORLEVEL%"
exit /b %CODE%
