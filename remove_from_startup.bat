@echo off
chcp 65001 > nul

set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set SHORTCUT_PATH=%STARTUP_DIR%\TelegramReminderBot.lnk

if exist "%SHORTCUT_PATH%" (
    del "%SHORTCUT_PATH%"
    echo [УСПЕХ] Бот удален из автозагрузки Windows.
) else (
    echo [ИНФО] Бот не был найден в автозагрузке.
)

pause
