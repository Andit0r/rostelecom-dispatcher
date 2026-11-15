@echo off
chcp 65001 >nul
rem Демо для проверяющих и показа: запускает API-симулятор (tools\sim) против уже работающего сервера.
rem Сам сервер НЕ запускает: поднимите его заранее (docker compose up --build или tools\run-backend-lan.bat).
rem Симулятор загружает день на сервер. Сайт диспетчера подхватывает его сам, если включено автообновление
rem (опрос /ui/plan/status, без перезагрузки). Перед первым запуском в интерфейсе можно нажать «Очистить день».
rem Адрес по умолчанию http://127.0.0.1:8080, другой - первым параметром: tools\demo.bat http://127.0.0.1:8100
rem Дата дня - вторым параметром (tools\demo.bat http://127.0.0.1:8080 2026-12-01) или ответом на вопрос;
rem один и тот же набор нарядов эталонного дня переносится на эту дату (run_sim.py --date).
rem Используем 127.0.0.1, а не localhost (IPv6 и wslrelay на Windows).
setlocal
cd /d "%~dp0.."

if "%~1"=="" (set "BASE=http://127.0.0.1:8080") else (set "BASE=%~1")
set "DAY=%~2"

rem Прокси очищаются только в этом окне; локальный адрес всегда без прокси
set HTTP_PROXY=
set HTTPS_PROXY=
set NO_PROXY=127.0.0.1,localhost

rem Интерпретатор: окружение backend, иначе системный python
set "PY="
if exist "backend\.venv\Scripts\python.exe" set "PY=backend\.venv\Scripts\python.exe"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo [!] Python не найден: нет backend\.venv и python нет в PATH.
  exit /b 1
)

rem Проверка сервера: /api/v1/health, таймаут 5 с, без прокси
echo [i] Проверка сервера %BASE% ...
"%PY%" -c "import urllib.request as u,sys; u.build_opener(u.ProxyHandler({})).open(sys.argv[1].rstrip('/')+'/api/v1/health', timeout=5).read()" "%BASE%" >nul 2>&1
if errorlevel 1 goto :nohealth

:askdate
if defined DAY goto :checkdate
set /p "DAY=Дата дня (ГГГГ-ММ-ДД, Enter = сегодня): "
if not defined DAY goto :menu
:checkdate
echo %DAY%| findstr /r /x "[0-9][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]" >nul
if errorlevel 1 (
  echo [!] Неверный формат даты: %DAY% ^(нужно ГГГГ-ММ-ДД^)
  set "DAY="
  goto :askdate
)

:menu
echo.
echo ==== Демо: API-симулятор ====
echo Сервер: %BASE%
if defined DAY (echo Дата дня: %DAY%) else (echo Дата дня: сегодня ^(или ближайший рабочий^))
echo   1 - План на весь день сразу
echo   2 - Поток звонков диспетчеру
echo   3 - Сборка ПГП
echo   4 - Текучка: отказы персонала, задержки, отмены
echo   5 - Всё подряд
echo   9 - Подготовить справочники (seed): только справочники, без нарядов
echo   0 - Выход
echo.
set "CH="
set /p "CH=Выбор: "
if "%CH%"=="" goto :end
if "%CH%"=="0" goto :end
set "SC="
if "%CH%"=="1" set "SC=bulk"
if "%CH%"=="2" set "SC=calls"
if "%CH%"=="3" set "SC=pgp"
if "%CH%"=="4" set "SC=churn"
if "%CH%"=="5" set "SC=all"
if "%CH%"=="9" (
  "%PY%" tools\sim\seed.py --base-url "%BASE%"
  goto :menu
)
if not defined SC (
  echo Неизвестный пункт: %CH%
  goto :menu
)

if not exist out mkdir out
echo.
echo [i] Запуск сценария %SC% (seed 1), это может занять несколько минут ...
set "DATEARG="
if defined DAY set "DATEARG=--date %DAY%"
"%PY%" tools\sim\run_sim.py --base-url "%BASE%" --scenario %SC% --seed 1 %DATEARG% --report out\sim_%SC%.md
echo [i] Код возврата симулятора: %errorlevel%
echo Отчёт: out\sim_%SC%.md
goto :menu

:nohealth
echo Сервер не отвечает на %BASE%. Запустите docker compose up --build ^(или tools\run-backend-lan.bat^).
endlocal
exit /b 1

:end
endlocal
exit /b 0
