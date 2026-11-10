@echo off
rem Переходим в папку скрипта, чтобы все относительные пути работали откуда угодно.
cd /d "%~dp0"

rem # author  # github.com\dkxce
rem # version # v1.0.0.0

:begin
chcp 65001 >nul
set "PROCESS_NAME=h9_ui_flash"

:iflaunched
tasklist /nh /fi "imagename eq %PROCESS_NAME%.exe" 2>nul | findstr /i "%PROCESS_NAME%" >nul
if %errorlevel% neq 0 goto build
echo Процесс %PROCESS_NAME% сейчас запущен.
set /p "choice=Остановить его и запустить сборку exe? [Y/N]: "
if /i "%choice%"=="Y" (
    powershell -Command "Stop-Process -Name %PROCESS_NAME% -Force"
	goto build
) else exit /b	

:build
@echo on
.venv\Scripts\create-version-file h9_ui_flash.yaml --outfile h9_ui_flash.txt
.venv\Scripts\pyinstaller.exe --clean --paths . --version-file="h9_ui_flash.txt" -i "h9_ui_flash.ico" --noconsole --onefile ui_app/app.py
@del /Q h9_ui_flash.txt
move /y dist\app.exe dist\h9_ui_flash.exe
tools\upx.exe -9f dist\h9_ui_flash.exe
copy /y hidapi.dll dist\hidapi.dll
@echo Сборка завершена
@pause

:end