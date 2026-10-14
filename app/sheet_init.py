"""scripts/init_sheet.py logic: idempotent creation of tabs, headers, notes, dropdowns, colours and samples."""
from __future__ import annotations

import sys

from .config import BANNED_TXT_PATH, load_config, load_env
from .sheets import COLUMNS, IMAGE_MODES, IMAGE_STYLES, STATUS_VALUES

NOTES = {
    "ID": "ربات پر می‌کند. اگر خالی باشد، شناسه‌ی یکتای W-0001 می‌گیرد. کارها با ID انجام می‌شود، نه با شماره‌ی سطر.",
    "Status": "ربات پر می‌کند (انسان فقط Pending یا Deleted می‌گذارد). فقط سطرهای Pending پردازش می‌شوند.",
    "Title": "انسان: تیتر پست.",
    "Body": "انسان: متن اصلی پست.",
    "FullText": "انسان (اختیاری): اگر پر باشد، کل کپشن همین متن است. Title و Body نادیده گرفته می‌شوند.",
    "SourceURL": "انسان: لینک منبع (https). هر عدد پست باید در این منبع پیدا شود.",
    "Hashtags": "انسان: هشتگ‌ها با فاصله یا کاما جدا شوند. مثال: #ارز_دیجیتال #بازار",
    "CTA": "انسان (اختیاری): جمله‌ی دعوت به اقدام، مثل «برای ادامه کانال را دنبال کنید».",
    "ImageMode": "انسان: gemini (پیش‌فرض اگر خالی باشد) یا none (بدون تصویر).",
    "ImageStyle": "انسان (اختیاری): photo، 3d، illustration، minimal یا infographic-bg.",
    "ImagePrompt": "انسان (اختیاری): پرامپت کامل تصویر. اگر پر باشد، جایگزین قالب می‌شود.",
    "AdminNote": "ربات پر می‌کند: یادداشت وضعیت برای ادمین.",
    "RejectReason": "ربات پر می‌کند: دلیل رد توسط ادمین.",
    "PolicyReport": "ربات پر می‌کند: خلاصه‌ی سیاست‌ها، مثلاً ⚠️2 ⛔0.",
    "Error": "ربات پر می‌کند: آخرین خطا به فارسی.",
    "PostURL": "ربات پر می‌کند: لینک پست منتشرشده.",
    "UpdatedAt": "ربات پر می‌کند: زمان آخرین تغییر به وقت تهران.",
    "PublishedAt": "ربات پر می‌کند: زمان انتشار به وقت تهران.",
}
STATUS_COLORS = {
    "Drafted": (1.0, 0.95, 0.6), "Published": (0.7, 0.92, 0.7), "NeedsFix": (1.0, 0.75, 0.75),
    "Failed": (0.95, 0.5, 0.5), "Waiting": (1.0, 0.85, 0.6), "Rejected": (0.85, 0.85, 0.85),
    "Publishing": (0.7, 0.85, 1.0), "Processing": (0.85, 0.92, 1.0), "Deleted": (0.6, 0.6, 0.6),
}
SAMPLE_CONTENT = [
    {"FullText": "این یک نمونه‌ی متن کامل است؛ همین متن، بدون تغییر، منتشر می‌شود.\n\n#نمونه", "ImageMode": "none", "Status": "Pending"},
    {"Title": "تلگرام؛ نمونه‌ای با منبع", "Body": "تلگرام در سال ۲۰۱۳ راه‌اندازی شد.",
     "SourceURL": "https://en.wikipedia.org/wiki/Telegram_(software)", "Hashtags": "#نمونه #تلگرام",
     "ImageMode": "none", "Status": "Pending"},
    {"Title": "ادعای بدون منبع", "Body": "رشد ۴۰٪ی بازار در یک ماه گذشته.", "Hashtags": "#نمونه",
     "ImageMode": "none", "Status": "Pending"},
]
SAMPLE_BANNED = [("کلاهبرداری", "hard"), ("بدون ریسک", "soft")]
SAMPLE_SOURCES = ["en.wikipedia.org"]


def col_letter(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def missing_in(headers: list[str], required: list[str]) -> list[str]:
    have = {h.strip() for h in headers}
    return [h for h in required if h not in have]


def header_requests(ws_id: int, start_col: int, names: list[str]) -> list[dict]:
    """updateCells requests that write headers and their notes at start_col (1-based)."""
    reqs = []
    for i, name in enumerate(names):
        reqs.append({"updateCells": {
            "rows": [{"values": [{"userEnteredValue": {"stringValue": name},
                                  "note": NOTES.get(name, "")}]}],
            "start": {"sheetId": ws_id, "rowIndex": 0, "columnIndex": start_col - 1 + i},
            "fields": "userEnteredValue,note",
        }})
    return reqs


def dropdown_request(ws_id: int, col_index: int, values: list[str], strict: bool = True) -> dict:
    return {"setDataValidation": {
        "range": {"sheetId": ws_id, "startRowIndex": 1, "endRowIndex": 2000,
                  "startColumnIndex": col_index, "endColumnIndex": col_index + 1},
        "rule": {"condition": {"type": "ONE_OF_LIST",
                               "values": [{"userEnteredValue": v} for v in values]},
                 "strict": strict, "showCustomUi": True},
    }}


def status_color_requests(ws_id: int, col_index: int) -> list[dict]:
    reqs = []
    for i, (value, (r, g, b)) in enumerate(STATUS_COLORS.items()):
        reqs.append({"addConditionalFormatRule": {"index": i, "rule": {
            "ranges": [{"sheetId": ws_id, "startRowIndex": 1, "endRowIndex": 2000,
                        "startColumnIndex": col_index, "endColumnIndex": col_index + 1}],
            "booleanRule": {"condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": value}]},
                            "format": {"backgroundColor": {"red": r, "green": g, "blue": b}}},
        }}})
    return reqs


def sample_row(headers: list[str], values: dict[str, str]) -> list[str]:
    return [values.get(h, "") for h in headers]


def run() -> int:
    import gspread

    cfg = load_config()
    env = load_env()
    if cfg.sheet.backend == "local":
        from .context import make_backend
        backend = make_backend(cfg, env)  # creates data/sheet/Content.csv with the 18 headers if missing
        print(f"✅ صف محلی آماده است: {backend.folder}")
        print(f"   هدرهای Content: {len(COLUMNS)} ستون. نیازی به Google Sheets نیست.")
        return 0
    if not env.sheet_id or not env.service_account_file.exists():
        print("❌ GOOGLE_SHEET_ID یا فایل سرویس‌اکانت در .env/secrets تنظیم نشده است.")
        return 1
    gc = gspread.service_account(filename=str(env.service_account_file))
    sh = gc.open_by_key(env.sheet_id)
    titles = [w.title for w in sh.worksheets()]
    created: list[str] = []
    for tab in (cfg.sheet.tab_content, cfg.sheet.tab_banned, cfg.sheet.tab_sources):
        if tab not in titles:
            sh.add_worksheet(title=tab, rows=500, cols=max(len(COLUMNS) + 2, 20))
            created.append(tab)
    print("تب‌های ساخته‌شده:", ", ".join(created) or "هیچ (از قبل بودند)")

    # --- Content
    ws = sh.worksheet(cfg.sheet.tab_content)
    headers = [h.strip() for h in (ws.row_values(1) or [])]
    miss = missing_in(headers, COLUMNS)
    reqs: list[dict] = []
    if miss:
        start = len([h for h in headers if h]) + 1
        reqs += header_requests(ws.id, start, miss)
        headers = headers + miss
        print("هدرهای اضافه‌شده:", ", ".join(miss))
    else:
        print("همه‌ی هدرهای Content از قبل وجود داشتند.")
    if reqs:
        sh.batch_update({"requests": reqs})
    idx = {h: i for i, h in enumerate(headers)}
    dv: list[dict] = []
    if "Status" in idx:
        dv.append(dropdown_request(ws.id, idx["Status"], STATUS_VALUES))
        meta = sh.fetch_sheet_metadata()
        existing_cf = 0
        for s in meta.get("sheets", []):
            if s["properties"]["sheetId"] == ws.id:
                existing_cf = len(s.get("conditionalFormats", []))
        if existing_cf == 0:
            dv += status_color_requests(ws.id, idx["Status"])
    if "ImageMode" in idx:
        dv.append(dropdown_request(ws.id, idx["ImageMode"], IMAGE_MODES))
    if "ImageStyle" in idx:
        dv.append(dropdown_request(ws.id, idx["ImageStyle"], IMAGE_STYLES))
    dv.append({"updateSheetProperties": {"properties": {"sheetId": ws.id,
                                                       "gridProperties": {"frozenRowCount": 1}},
                                         "fields": "gridProperties.frozenRowCount"}})
    sh.batch_update({"requests": dv})
    print("dropdown، رنگ وضعیت و ثابت کردن هدر اعمال شد.")

    values = ws.get_all_values()
    has_data = any(any(c.strip() for c in row) for row in values[1:])
    if not has_data:
        rows = [sample_row(headers, s) for s in SAMPLE_CONTENT]
        ws.append_rows(rows, value_input_option="RAW")
        print(f"{len(rows)} سطر نمونه اضافه شد (چون شیت خالی بود).")
    else:
        print("سطرهای داده وجود دارند؛ سطرهای نمونه اضافه نشدند.")

    # --- BannedWords
    wb = sh.worksheet(cfg.sheet.tab_banned)
    bh = [h.strip() for h in (wb.row_values(1) or [])]
    if not bh:
        wb.update([["Word", "Level", "Note"]], "A1", value_input_option="RAW")
        bh = ["Word", "Level", "Note"]
        words = _banned_from_txt() or SAMPLE_BANNED
        wb.append_rows([[w, lvl, "نمونه؛ فهرست واقعی را کامل کنید"] for w, lvl in words], value_input_option="RAW")
        print("تب BannedWords ساخته و با نمونه پر شد.")
    else:
        print("تب BannedWords از قبل هدر دارد.")

    # --- Sources
    wsrc = sh.worksheet(cfg.sheet.tab_sources)
    sh_headers = [h.strip() for h in (wsrc.row_values(1) or [])]
    if not sh_headers:
        wsrc.update([["Domain"]], "A1", value_input_option="RAW")
        wsrc.append_rows([[d] for d in SAMPLE_SOURCES], value_input_option="RAW")
        print("تب Sources ساخته و دامنه‌ی نمونه اضافه شد.")
    else:
        print("تب Sources از قبل هدر دارد.")
    print("\n✅ شیت آماده است. حالا python -m app.main --check را اجرا کنید.")
    return 0


def _banned_from_txt() -> list[tuple[str, str]]:
    out = []
    if BANNED_TXT_PATH.exists():
        for line in BANNED_TXT_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#") and "|" in line:
                w, _, lvl = line.partition("|")
                out.append((w.strip(), lvl.strip() or "hard"))
    return out


def main() -> int:
    try:
        return run()
    except Exception as exc:  # noqa: BLE001
        print(f"❌ خطا: {str(exc)[:300]}")
        print("اگر خطای 403 است: شیت را با ایمیل سرویس‌اکانت Editor کنید. اگر 404 است: GOOGLE_SHEET_ID را بررسی کنید.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
