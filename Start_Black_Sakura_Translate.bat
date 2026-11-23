@echo off
chcp 65001 >nul
title Black Sakura Translate
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo [LOI] Khong tim thay Python trong PATH!
    echo Vui long cai Python 3.10+ tai https://www.python.org/downloads/ (tich chon "Add Python to PATH").
    pause
    exit /b 1
)

python -c "import fastapi, uvicorn, httpx, rapidfuzz" >nul 2>&1
if errorlevel 1 (
    echo [Black Sakura Translate] Dang cai dat cac thu vien Python lan dau...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [LOI] Cai dat thu vien that bai. Kiem tra ket noi mang hoac chay: pip install -r requirements.txt
        pause
        exit /b 1
    )
)

python app.py %*
if errorlevel 1 (
    echo.
    echo [Black Sakura Translate] Ung dung da dung voi ma loi %ERRORLEVEL%.
    pause
)
