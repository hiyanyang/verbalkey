@echo off
chcp 65001 >nul
cd /d "%~dp0"
"D:\程序\skills\_venv\voice-input\Scripts\python.exe" src\main.py
pause
