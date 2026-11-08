@echo off
chcp 65001 > nul
cd /d "%~dp0"

echo ===================================================
echo   Добавление бота в Автозагрузку Windows
echo ===================================================
echo.

set SCRIPT_PATH=%~dp0run_silent.vbs
set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set SHORTCUT_PATH=%STARTUP_DIR%\TelegramReminderBot.lnk

powershell "$s=(New-Object -COM WScript.Shell).CreateShortcut('%SHORTCUT_PATH%');$s.TargetPath='wscript.exe';$s.Arguments='\"%SCRIPT_PATH%\"';$s.WorkingDirectory='%~dp0';$s.Save()"

if %errorlevel% equ 0 (
    echo [УСПЕХ] Бот успешно добавлен в автозагрузку Windows!
    echo Теперь при каждом включении компьютера бот будет сам
    echo незаметно запускаться в фоновом режиме (без черных окон).
) else (
    echo [ОШИБКА] Не удалось создать ярлык в автозагрузке.
)

echo.
pause
