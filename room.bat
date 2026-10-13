@echo off
rem Starts the Claude Room server (if it isn't running yet) and opens your chat.
cd /d "%~dp0"
python -c "import json,socket,sys;p=json.load(open('room.json'))['port'];sys.exit(0 if socket.socket().connect_ex(('127.0.0.1',p)) else 1)" 2>nul
if %errorlevel%==0 (
  start "Claude Room server" /min python server.py
  timeout /t 2 /nobreak >nul
)
python chat.py
