from __future__ import annotations

import sys
from pathlib import Path


def ensure_api_path() -> None:
    api_path = Path(__file__).resolve().parents[2] / "api"
    api_path_str = str(api_path)
    if api_path_str not in sys.path:
        sys.path.insert(0, api_path_str)


ensure_api_path()

