@echo off
rem fetch.bat - Windows (cmd) entry point; forwards every argument to fetch.ps1
rem
rem Usage:
rem   fetch.bat --ver 154.0.8037.21
rem   fetch.bat update --ver 154.0.8037.21 --save
rem   fetch.bat arupa_desktop --arupa-desktop-ver refs/tags/v1.0.0
rem   fetch.bat nomad_android --nomad-android-ver refs/heads/release
rem   fetch.bat deps --ver 154.0.8037.21 --nohooks
rem   fetch.bat toolchains --os win --arch all
rem   fetch.bat host-deps --os win --arch all --vs-installer C:\Downloads\vs_Community.exe
rem   fetch.bat pull
rem   fetch.bat push --dry-run
rem   fetch.bat log branch
rem   fetch.bat --dry-run
rem   fetch.bat -h
rem
rem Why this wrapper: cmd cannot execute .ps1 directly (".PS1" is missing from
rem PATHEXT and the extension has no file association), so typing ".\fetch.ps1"
rem in a cmd or SSH session does nothing at all - no output, no error.
rem
rem Unlike build.bat this wrapper does NOT pushd to the script directory:
rem pull / push / log / branch scan the direct subdirectories of the caller's
rem current directory, so switching directories here would scan the workspace
rem root instead of wherever the command was actually run.
rem
rem Env: FETCH_PS_SHELL - PowerShell executable to use
rem      (default: Windows PowerShell 5.1, which fetch.ps1 targets)
setlocal EnableExtensions

set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if defined FETCH_PS_SHELL set "PS=%FETCH_PS_SHELL%"

"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0fetch.ps1" %*

rem Propagate the real exit code (cmd / SSH / CI callers rely on it).
set "CODE=%ERRORLEVEL%"
exit /b %CODE%
