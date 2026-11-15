@echo off
rem Запуск backend без Docker для доступа по сети: порт 8080, все интерфейсы.
rem Не запускает брандмауэр и не меняет сетевые настройки.
setlocal
cd /d "%~dp0..\backend" || (echo Не найден каталог backend & exit /b 1)

if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
  echo [i] backend\.venv не найден, используется системный python
)

set "CORS_ORIGINS=*"
set "PLANNER=pyvrp"

echo [i] API: http://0.0.0.0:8080  (проверка: /api/v1/health)
"%PY%" -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --timeout-keep-alive 330
endlocal
