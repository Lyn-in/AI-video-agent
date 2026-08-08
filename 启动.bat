@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title AI Video Workbench

set "PYEXE="

rem --- 1) py launcher (most reliable on Windows) ---
where py >nul 2>&1 && (
  py -3 -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1 && set "PYEXE=py -3"
)

rem --- 2) python on PATH ---
if not defined PYEXE (
  where python >nul 2>&1 && (
    python -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1 && set "PYEXE=python"
  )
)

rem --- 3) common install locations, in case PATH was not set ---
if not defined PYEXE (
  for %%V in (313 312 311 310) do (
    if not defined PYEXE (
      if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
      if exist "C:\Python%%V\python.exe" set "PYEXE=C:\Python%%V\python.exe"
      if exist "%PROGRAMFILES%\Python%%V\python.exe" set "PYEXE=%PROGRAMFILES%\Python%%V\python.exe"
    )
  )
)

if not defined PYEXE goto NOPYTHON

%PYEXE% tools\launcher.py
echo.
echo ----------------------------------------------------
echo  Stopped. Close this window, or double-click to restart.
echo ----------------------------------------------------
pause
goto :eof

:NOPYTHON
echo.
echo ====================================================
echo   Python not found
echo ====================================================
echo.
echo   This tool needs Python 3.10 or newer.
echo.
echo   1. Download it from  https://www.python.org/downloads/
echo   2. IMPORTANT: tick "Add Python to PATH" during setup.
echo      Missing that checkbox is the usual cause of this error.
echo   3. Then double-click this file again.
echo.
echo   Opening the download page for you...
echo.
start "" "https://www.python.org/downloads/"
pause
