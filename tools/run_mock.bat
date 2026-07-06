@echo off
cd /d %~dp0\..
python -m zlg_canoe_bridge config\bridge_config_mock.json --mock-inject
pause
