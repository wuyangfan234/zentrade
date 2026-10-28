"""手动跑 EDGAR 同步: python -m app.services.edgar_fundamentals_cli <data_dir> SYM [SYM ...]"""
import json
import sys
from pathlib import Path

from app.services.edgar_fundamentals import sync

if __name__ == "__main__":
    print(json.dumps(sync(Path(sys.argv[1]), sys.argv[2:]), ensure_ascii=False))
