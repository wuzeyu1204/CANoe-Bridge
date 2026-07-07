@echo off
setlocal
cd /d "%~dp0\.."

python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name ZLG_CANoe_Bridge ^
  --icon assets\app_icon.ico ^
  --add-data "config\bridge_config.json;config" ^
  --add-data "assets\app_icon.ico;assets" ^
  zlg_canoe_bridge\gui.py

if errorlevel 1 exit /b %errorlevel%

if not exist dist\config mkdir dist\config
copy /Y config\bridge_config.json dist\config\bridge_config.json >nul
if exist docs\USER_MANUAL_CN.md copy /Y docs\USER_MANUAL_CN.md dist\USER_MANUAL_CN.md >nul
if exist dist\logs rmdir /S /Q dist\logs
mkdir dist\logs

echo.
echo Built: dist\ZLG_CANoe_Bridge.exe
