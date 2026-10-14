"""Full pre-flight check: python scripts/check.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import check_cli  # noqa: E402

if __name__ == "__main__":
    sys.exit(check_cli())
