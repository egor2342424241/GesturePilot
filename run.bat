@echo off
setlocal
cd /d "%~dp0"
set PYTHON_EXE=.venv\Scripts\python.exe

if not exist "%PYTHON_EXE%" (
  echo [GesturePilot] Creating Python 3.11 environment...
  py -3.11 -m venv .venv
  if errorlevel 1 (
    echo.
    echo Could not create the environment. Install 64-bit Python 3.11 and enable the py launcher.
    pause
    exit /b 1
  )
)

"%PYTHON_EXE%" -c "import cv2, mediapipe, pyautogui, PySide6" >nul 2>&1
if errorlevel 1 (
  echo [GesturePilot] Installing required packages. Internet is needed for this first setup...
  "%PYTHON_EXE%" -m pip install --upgrade pip
  if errorlevel 1 goto :install_failed
  "%PYTHON_EXE%" -m pip install -r requirements.txt
  if errorlevel 1 goto :install_failed
)

"%PYTHON_EXE%" main.py
if errorlevel 1 pause
exit /b 0

:install_failed
echo.
echo Dependency installation failed. Check the internet connection and Python version, then run run.bat again.
pause
exit /b 1
