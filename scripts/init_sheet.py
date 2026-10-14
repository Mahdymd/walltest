"""Creates/updates the Google Sheet structure (safe to run many times): python scripts/init_sheet.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.sheet_init import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
