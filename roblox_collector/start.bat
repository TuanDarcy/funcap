@echo off
cd /d "%~dp0"
echo ====================================
echo   Roblox FunCAPTCHA Collector
echo ====================================
echo.

REM Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Python khong duoc cai dat!
    pause
    exit /b 1
)

REM Check Playwright
python -c "import playwright" >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Chua cai playwright! Dang cai dat...
    pip install playwright
    playwright install chromium
)

echo [*] Kiem tra input files...
if not exist "input\accounts.txt" (
    echo [!] Chua co input\accounts.txt!
    echo     Tao file mau...
    mkdir input 2>nul
    echo # Format: username:password > input\accounts.txt
    echo # user1:pass123 >> input\accounts.txt
)

if not exist "input\proxies.txt" (
    echo [!] Chua co input\proxies.txt!
    echo     Tao file mau...
    mkdir input 2>nul
    echo # Format: ip:port hoac user:pass@ip:port > input\proxies.txt
    echo # 127.0.0.1:8080 >> input\proxies.txt
)

echo.
echo [*] Bat dau thu thap CAPTCHA...
echo     Output: captured\
echo.
python collector.py

echo.
echo [*] Xong! Anh duoc luu trong captured\
pause
