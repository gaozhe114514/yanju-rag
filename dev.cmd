@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".env" (
  echo 没有找到 .env，先复制 .env.example 为 .env 并填写配置。
  pause
  exit /b 1
)
echo 言据本地预览：http://127.0.0.1:8011/
echo 改前端保存后按 Ctrl+F5；改后端会自动重启。按 Ctrl+C 停止。
E:\venvs\yanju311\Scripts\python.exe -m uvicorn api.app:app --host 127.0.0.1 --port 8011 --reload --env-file .env
pause
