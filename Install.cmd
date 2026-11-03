@echo off
rem VAMPVIZ: double-click to install or update. "Install.cmd -Uninstall" removes it.
if not exist "%~dp0install.ps1" (echo Extract the whole ZIP first, then double-click Install.cmd in the extracted folder.& pause & exit /b 1)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %* & exit /b
