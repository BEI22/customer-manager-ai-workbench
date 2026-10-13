@echo off
title 客户经理 AI 作战台 - 启动器
cd /d "D:\OpenaiWorkplace\muyuanwork\customer-manager-ai-workbench"
echo ============================================
echo   客户经理 AI 作战台 - 一键启动
echo ============================================
echo.
netstat -ano | findstr ":8000 " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
  echo [1/3] 后端已在运行，跳过启动
) else (
  echo [1/3] 正在启动后端（DeepSeek Agent）...
  start "AI-Backend" /min cmd /c "cd /d D:\OpenaiWorkplace\muyuanwork\customer-manager-ai-workbench\backend && python -m uvicorn app.main:app --port 8000"
)
echo [2/3] 等待后端就绪（首次冷启动约 40-60 秒）...
set tries=0
:waitloop
timeout /t 3 /nobreak >nul
curl -s -m 2 http://127.0.0.1:8000/api/health >nul 2>&1
if %errorlevel%==0 goto ready
set /a tries+=1
if %tries% GEQ 30 goto open
goto waitloop
:ready
echo       后端已就绪（DeepSeek 模型）
goto open
:open
echo [2/3] 打开系统界面...
start "" "D:\OpenaiWorkplace\muyuanwork\customer-manager-ai-workbench\frontend\index.html"
echo.
echo 完成！关闭后端请运行「停止后端.bat」
timeout /t 5 /nobreak >nul
exit