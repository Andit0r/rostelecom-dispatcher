@echo off
chcp 65001 >nul
rem Загружает на работающий сервер ТОЛЬКО справочники из каталога data\synth через /api/v1
rem (PUT reference + PUT reference/travel-matrix; без нарядов и дней) и ФИО-заготовку data\synth\staff_names.json
rem для записей справочника сотрудников с пустым ФИО. Потом в интерфейсе: «Создать день» и «Новый наряд».
rem Внимание: PUT reference удаляет на сервере все дни и планы. При AUTH_ENABLED=on для ФИО спросит логин/пароль
rem диспетчера (или возьмёт DISPATCHER_LOGIN / DISPATCHER_PASSWORD).
rem Адрес по умолчанию http://127.0.0.1:8080, другой - первым параметром: tools\seed.bat http://127.0.0.1:8100
rem Используем 127.0.0.1, а не localhost (IPv6 и wslrelay на Windows).
setlocal
cd /d "%~dp0.."

if "%~1"=="" (set "BASE=http://127.0.0.1:8080") else (set "BASE=%~1")

rem Прокси очищаются только в этом окне; локальный адрес всегда без прокси
set HTTP_PROXY=
set HTTPS_PROXY=
set NO_PROXY=127.0.0.1,localhost
set PYTHONIOENCODING=utf-8

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

"%PY%" tools\sim\seed.py --base-url "%BASE%"
set "RC=%errorlevel%"
endlocal & exit /b %RC%
