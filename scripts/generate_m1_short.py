"""生成《纸鹤的夜航》M1 Mock 短片。"""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.media import MediaToolError, generate_m1_short  # noqa: E402


def main() -> int:
    try:
        result = generate_m1_short(ROOT)
    except (MediaToolError, OSError, ValueError) as exc:
        print(f"M1 FAILED: {exc}", file=sys.stderr)
        return 1
    print("M1 GENERATION PASS")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
