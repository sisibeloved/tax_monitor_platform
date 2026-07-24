"""Compatibility entry point for the manual Lark Base alert queue.

This module never calls the Feishu message API. ``--execute`` remains accepted
as an alias for ``--enqueue`` so existing operator commands create queue rows
instead of sending direct messages.
"""

from __future__ import annotations

from pathlib import Path
import sys


BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from scripts.enqueue_feishu_alert_notifications import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
