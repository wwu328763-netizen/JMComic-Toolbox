@echo off
chcp 65001 >nul
title JMComic
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0jm.ps1"
if errorlevel 1 pause
