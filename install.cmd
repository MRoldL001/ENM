@echo off
chcp 65001 >nul
title ENM Installer
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "enm_exit=%ERRORLEVEL%"
if not "%enm_exit%"=="0" (
  echo.
  echo ENM installation failed.
)
echo.
pause
exit /b %enm_exit%
