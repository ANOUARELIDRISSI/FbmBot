"""Stage [7] groundwork: records each 👍/👎 verdict against its listing_id.
Just data capture for now — actually retraining scoring weights from this
needs enough real feedback volume to mean anything, which doesn't exist
yet on day one. See Project.md stage 7.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_FEEDBACK_PATH = Path("data/feedback/feedback.jsonl")


def record_feedback(listing_id: str, verdict: str, path: Path = DEFAULT_FEEDBACK_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "listing_id": listing_id,
        "verdict": verdict,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
