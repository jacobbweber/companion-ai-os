---
name: personal-agile-coach
description: Optional personal Agile, life/chore timeboxing, weekday/weekend capacity coaching, and anti-procrastination micro-slicing skill built on top of Companion's domain-blind Universal Project & Goal Graph.
required_tools: [agile_manage, calendar_manage, run_python]
---

# Personal Agile & Life Coach (`personal-agile-coach`)

## When to Use This Skill
Activate `personal-agile-coach` whenever the human partner asks for help managing personal projects, household chores, study habits, weekly sprint capacity, or overcoming procrastination on `owner="human"` items in the Universal Project & Goal Graph (`companion/project_graph.py`).

## Step-by-Step Procedure
1. **Inspect Graph State**: Call `agile_manage(subaction="board")` to inspect active Milestones, `human` vs. `companion` items, and blocked dependencies.
2. **Capacity & Aging Analysis**: Run `scripts/coach_helper.py` via `run_python` to compute realistic weekday vs. weekend time budgets and spot stalled `owner="human"` items.
3. **15-Minute Unblocker Slicing**: When an `owner="human"` item is large (`estimate_minutes > 60`) or blocking downstream `owner="companion"` work, create a `<15m` physical/decision unblocker item (`owner="human"`) and wire `depends_on` so Companion automatically resumes once the human completes that 15-minute step.
4. **Calendar Alignment**: Schedule realistic `planned_date` slots via `agile_manage(subaction="update_card", ...)` and verify against `calendar_manage(subaction="week")`.
