@echo off
REM Hudoo SEO Desk launcher. ASCII only -- Chinese here breaks on GBK consoles.
cd /d "%~dp0"
py -3.14 -m app.main %*
if errorlevel 1 pause
