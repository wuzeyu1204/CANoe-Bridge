@echo off
setlocal
cd /d "%~dp0\.."
python -m zlg_canoe_bridge.gui config\bridge_config.json
pause
