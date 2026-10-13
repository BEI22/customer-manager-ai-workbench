@echo off
title 关闭后端
echo 正在关闭客户经理 AI 作战台后端...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING"') do taskkill /f /pid %%a >nul 2>&1
echo 后端已关闭
timeout /t 2 /nobreak >nul
exit