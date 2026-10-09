@echo off
rem build.bat - Windows (cmd) entry point; forwards every argument to build.ps1
rem
rem Usage:
rem   build.bat arupa_desktop gen build package publish
rem   build.bat nomad_desktop build package
rem   build.bat -h
rem
rem Why this wrapper: cmd cannot execute .ps1 directly (".PS1" is missing from
rem PATHEXT and the extension has no file association), so typing ".\build.ps1"
rem in a cmd or SSH session does nothing at all - no output, no error.
rem
rem Env: BUILD_PS_SHELL - PowerShell executable to use
rem      (default: Windows PowerShell 5.1, which build.ps1 targets)
setlocal EnableExtensions

rem Run from this script's directory, so relative arguments (build/win/args.gn,
rem --delivery ...) resolve the same no matter where build.bat is invoked from.
pushd "%~dp0"

set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if defined BUILD_PS_SHELL set "PS=%BUILD_PS_SHELL%"

"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*

rem Propagate the real exit code (cmd / SSH / CI callers rely on it).
set "CODE=%ERRORLEVEL%"
popd
exit /b %CODE%
