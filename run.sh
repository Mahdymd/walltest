#!/usr/bin/env bash
# ایجنت انتشار وال - اجرای یک‌مرحله‌ای برای لینوکس و مک
set -u
cd "$(dirname "$0")"
export PYTHONUTF8=1

echo "=== مرحله ۱: پیدا کردن پایتون ==="
PY=""
for cand in python3.13 python3.15 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] <= (3,15) else 1)' 2>/dev/null; then
    PY="$cand"; break
  fi
done
if [ -z "$PY" ]; then
  echo "❌ پایتون ۳.۱۱ تا ۳.۱۵ پیدا نشد. آن را نصب کنید و دوباره اجرا کنید."
  exit 1
fi
echo "✅ پایتون: $PY"

echo "=== مرحله ۲: محیط مجازی (.venv) ==="
if [ ! -x ".venv/bin/python" ]; then
  "$PY" -m venv .venv || { echo "❌ ساخت محیط مجازی انجام نشد."; exit 1; }
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "=== مرحله ۳: نصب کتابخانه‌ها ==="
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt || { echo "❌ نصب کتابخانه‌ها خطا داد."; exit 1; }

echo "=== مرحله ۴: Chromium (اختیاری در فاز ۱) ==="
python -m playwright install chromium || echo "⚠️ نصب Chromium انجام نشد؛ در فاز ۱ اجباری نیست."

echo "=== مرحله ۵: فایل .env ==="
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "⚠️ فایل .env ساخته شد. مقادیر آن را با یک ویرایشگر پر کنید و دوباره اجرا کنید."
  exit 1
fi

echo "=== مرحله ۶: بررسی تنظیمات ==="
python scripts/check.py || { echo "❌ بررسی تنظیمات موارد ناقص دارد. راهنما را ببینید و دوباره اجرا کنید."; exit 1; }

echo "=== مرحله ۷: اجرای ربات ==="
exec python -m app.main
