# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
Personal Agile Coach Helper (`skills/personal-agile-coach/scripts/coach_helper.py`).

Provides Ring-1 human capacity budgeting (weekday vs. weekend slots) and
anti-procrastination 15-minute unblocker slicing over Universal Project Graph items.
"""

import json
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List


def compute_human_capacity_report(
    items: List[Dict[str, Any]],
    weekday_minutes: int = 90,
    weekend_minutes: int = 240,
    weekdays_count: int = 5,
    weekend_days_count: int = 2,
) -> Dict[str, Any]:
    """Compute realistic weekly capacity utilization and identify aging/oversized human items."""
    human_open = [
        it for it in items
        if str(it.get("owner", "human")) in ("human", "collaborative")
        and str(it.get("status", "ready")) not in ("done", "completed", "cancelled")
    ]
    total_est = sum(max(5, int(it.get("estimate_minutes", 30))) for it in human_open)
    budget = (weekday_minutes * weekdays_count) + (weekend_minutes * weekend_days_count)
    oversized = [it["item_id"] for it in human_open if int(it.get("estimate_minutes", 30)) > 90]
    now = datetime.now(timezone.utc)
    stalled: List[str] = []
    for it in human_open:
        raw_ts = str(it.get("updated_at") or it.get("created_at") or "").strip()
        if not raw_ts:
            continue
        try:
            norm = raw_ts.replace("Z", "+00:00") if "T" in raw_ts else (raw_ts.replace(" ", "T") + "+00:00")
            age_days = (now - datetime.fromisoformat(norm).astimezone(timezone.utc)).days
            if age_days >= 14:
                stalled.append(str(it["item_id"]))
        except Exception:
            continue
    return {
        "open_human_items": len(human_open),
        "estimated_minutes": total_est,
        "weekly_budget_minutes": budget,
        "over_capacity": total_est > budget,
        "utilization_pct": round((total_est / max(1, budget)) * 100.0, 1),
        "oversized_item_ids": oversized,
        "stalled_item_ids": stalled,
    }


def propose_unblocker_slice(item: Dict[str, Any]) -> Dict[str, Any]:
    """Build a 15-minute first-step unblocker item specification for a stalled or oversized task."""
    title = str(item.get("title") or "Task").strip()
    return {
        "title": f"15m First Step: {title[:90]}",
        "description": f"Complete the smallest concrete 15-minute physical or decision step to unblock `{title}`.",
        "owner": "human",
        "estimate_minutes": 15,
        "priority": max(1, int(item.get("priority", 3)) - 1),
        "blocks_item_id": item.get("item_id", ""),
    }


def _run_self_test() -> int:
    sample = [
        {"item_id": "wi_1", "title": "Clean garage", "owner": "human", "status": "ready", "estimate_minutes": 180, "updated_at": "2025-01-01 00:00:00"},
        {"item_id": "wi_2", "title": "Draft CAD macro", "owner": "companion", "status": "blocked", "estimate_minutes": 45},
    ]
    rep = compute_human_capacity_report(sample, weekday_minutes=30, weekend_minutes=60)
    assert rep["open_human_items"] == 1
    assert "wi_1" in rep["oversized_item_ids"]
    assert "wi_1" in rep["stalled_item_ids"]
    unb = propose_unblocker_slice(sample[0])
    assert unb["estimate_minutes"] == 15 and unb["blocks_item_id"] == "wi_1"
    print(json.dumps({"ok": True, "report": rep, "unblocker": unb}))
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(_run_self_test())
    payload = json.loads(sys.stdin.read() or "[]")
    print(json.dumps(compute_human_capacity_report(payload), indent=2))
