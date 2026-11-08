@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Telegram Reminder Bot

echo ===================================================
echo     Запуск Telegram Бота Напоминаний
echo ===================================================
echo.

if exist .venv\Scripts\python.exe (
    goto run_bot
)

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ОШИБКА] Python не найден в системе.
    echo Пожалуйста, установите Python 3 с официального сайта python.org
    pause
    exit /b 1
)

echo [ИНФО] Создание виртуального окружения (.venv)...
python -m venv .venv
if %errorlevel% neq 0 (
    echo [ОШИБКА] Не удалось создать виртуальное окружение.
    pause
    exit /b 1
)

echo [ИНФО] Установка библиотек...
.venv\Scripts\pip install -r requirements.txt

:run_bot
echo ===================================================
echo     Запуск бота... (Для остановки нажмите Ctrl+C)
echo ===================================================
echo.

.venv\Scripts\python.exe bot.py

if %errorlevel% neq 0 (
    echo.
    echo [ВНИМАНИЕ] Работа бота завершилась.
)
pause
