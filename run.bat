@echo off
rem ============================================================
rem  ایجنت انتشار وال - اجرای یک‌مرحله‌ای برای ویندوز
rem  دوبار کلیک کنید. اولین بار چند دقیقه طول می‌کشد.
rem ============================================================
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
title ایجنت انتشار وال

set "VPY=.venv\Scripts\python.exe"

echo.
echo === مرحله ۱: پیدا کردن پایتون ===
set "PY="
py -3.13 -c "import sys" >nul 2>nul && set "PY=py -3.13"
if defined PY goto have_python
py -3.15 -c "import sys" >nul 2>nul && set "PY=py -3.15"
if defined PY goto have_python
py -3.12 -c "import sys" >nul 2>nul && set "PY=py -3.12"
if defined PY goto have_python
py -3.11 -c "import sys" >nul 2>nul && set "PY=py -3.11"
if defined PY goto have_python
python -c "import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] <= (3,15) else 1)" >nul 2>nul && set "PY=python"
if defined PY goto have_python
echo ❌ پایتون ۳.۱۱ تا ۳.۱۵ پیدا نشد.
echo    از سایت python.org نسخه ۳.۱۳ را نصب کنید و در نصب، گزینه Add python.exe to PATH را تیک بزنید.
goto fail

:have_python
echo ✅ پایتون پیدا شد: %PY%

echo.
echo === مرحله ۲: ساخت محیط مجازی (.venv) ===
if exist "%VPY%" goto venv_ready
%PY% -m venv .venv
if not exist "%VPY%" goto venv_failed
:venv_ready
echo ✅ محیط مجازی آماده است: %VPY%
"%VPY%" -c "import sys; print('   نسخه‌ی پایتون محیط مجازی:', sys.version.split()[0])"

echo.
echo === مرحله ۳: نصب کتابخانه‌ها ===
"%VPY%" -m pip install --quiet --upgrade pip
if errorlevel 1 goto pip_failed
"%VPY%" -m pip install --quiet -r requirements.txt
if errorlevel 1 goto pip_failed
echo ✅ کتابخانه‌ها نصب شدند.

echo.
echo === مرحله ۴: نصب مرورگر Chromium برای رندر منبع (اختیاری در فاز ۱) ===
"%VPY%" -m playwright install chromium
if errorlevel 1 echo ⚠️ نصب Chromium انجام نشد. این مرحله در فاز ۱ اجباری نیست؛ ادامه می‌دهیم.

echo.
echo === مرحله ۵: ساختن فایل .env در صورت نبود ===
if exist ".env" goto env_ready
copy /y ".env.example" ".env" >nul
echo ⚠️ فایل .env ساخته شد. الان با Notepad بازش کنید و مقادیر را پر کنید،
echo    بعد این پنجره را ببندید و دوباره run.bat را اجرا کنید.
notepad ".env"
goto fail
:env_ready

echo.
echo === مرحله ۶: بررسی تنظیمات (check) ===
"%VPY%" scripts\check.py
if errorlevel 1 (
  echo.
  echo ❌ بررسی تنظیمات موارد ناقص دارد. راهنمای بالا را ببینید، فایل .env را اصلاح کنید و دوباره اجرا کنید.
  goto fail
)

echo.
echo === مرحله ۷: اجرای ربات ===
"%VPY%" -m app.main
echo.
echo ربات متوقف شد.
pause
exit /b 0

:venv_failed
echo ❌ ساخت محیط مجازی انجام نشد. فایل .venv را پاک کنید و دوباره اجرا کنید.
echo    اگر باز هم خطا داد، متن بالای همین پنجره را برای پشتیبانی بفرستید.
goto fail

:pip_failed
echo ❌ نصب کتابخانه‌ها خطا داد. متن خطای بالا را بخوانید؛ اینترنت را هم بررسی کنید.
goto fail

:fail
echo.
pause
exit /b 1
