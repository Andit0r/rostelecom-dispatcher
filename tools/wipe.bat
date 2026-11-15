@echo off
chcp 65001 >nul
rem Очистка сервера (POST /ui/day/reset): конкретный день, все дни или всё вместе со справочниками.
rem   tools\wipe.bat                          - меню: 1 день / 2 все дни / 3 всё со справочниками
rem   tools\wipe.bat [адрес] [дата] [/y]      - сразу очистить день YYYY-MM-DD (/y - без вопроса)
rem Адрес по умолчанию http://127.0.0.1:8080. Перед удалением показывает, что будет удалено, и спрашивает Y/N.
rem Справочник сотрудников (ФИО) и аккаунты не удаляются. На диск ничего не пишет.
rem При AUTH_ENABLED=on спросит логин/пароль диспетчера (или возьмёт DISPATCHER_LOGIN / DISPATCHER_PASSWORD).
rem Используем 127.0.0.1, а не localhost (IPv6 и wslrelay на Windows).
setlocal
cd /d "%~dp0.."

set "BASE=http://127.0.0.1:8080"
set "DATE_ARG="
set "YES_ARG="
:args
if "%~1"=="" goto :argsdone
if /i "%~1"=="/y" (set "YES_ARG=--yes") else (
  echo %~1| findstr /b /i "http" >nul && (set "BASE=%~1") || (set "DATE_ARG=--date %~1")
)
shift
goto :args
:argsdone

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

"%PY%" tools\sim\wipe.py --base-url "%BASE%" %DATE_ARG% %YES_ARG%
set "RC=%errorlevel%"
endlocal & exit /b %RC%
