---
name: agile-coach
description: Personal Scrum Master, Life & Software Kanban Board, Personal Goals / Jira Epics & Roadmaps, Card Checklists, Weekly Sprint Planner, Weekday/Weekend Capacity Slot Balancer, Calendar Schedule/Appointment Coordinator, and Anti-Procrastination Unblocker using agile_manage and calendar_manage.
required_tools: [agile_manage, calendar_manage, memory_manage]
---

# Universal Agile & Life Management Coach (`agile-coach`)

## When to Use This Skill
Activate this runbook whenever the user wants to organize tasks, capture a personal or long-running goal (Epic), plan their week/sprint, balance work vs. home obligations, break down procrastinated or stalled items, or inspect Kanban boards and Calendar schedules.

## Core Coaching Principles
- **Personal Goals = Epics (`save_epic`), NOT Companion `/goal` Builds**: When the user talks about a personal, home, or work goal (e.g., *"my goal of cleaning the barn"*, *"renovating the basement"*), NEVER confuse it with Companion's autonomous software workflow (`workflow_manage` / `/goal`). Create **one** Epic via `agile_manage(action="save_epic", title=..., board_id=..., horizon=..., start_date=..., target_date=...)`, then link child cards under it with `epic_id="<returned_epic_id>"` (do NOT create a duplicate parent summary card alongside the Epic, and do NOT call `save_epic` twice).
- **Cards vs. Card Checklists**: Distinct multi-session deliverables under an Epic belong as linked cards (`save_card(..., epic_id=...)`). Tiny sub-steps inside a single task belong inside that card's `checklist=["Step 1", "Step 2"]`.
- **One Engine for Life & Software**: Boards (`life-home`, `work-career`, `software-companion`, custom project boards) feed into one **Master Active Sprint** (`all-active-sprint`) so the user never overcommits their real week across disconnected lists.
- **Weekday vs. Weekend Slot Realism**: Every card carries an `effort_size` (`quick` = 1 slot `<30m`, `half_day` = 2 slots, `full_day` = 4 slots, `multi_day` = 8 slots) and a `day_slot` (`weekday`, `weekend`, `any`). Outdoor/heavy chores belong in `weekend` slots; quick phone calls belong in `weekday` slots.
- **Zero-Guilt Anti-Procrastination**: When `[AGING / STALLED RADAR]` flags untouched/rolled-over cards or a card is `multi_day`, never nag. Propose slicing a `<30m` first step via `agile_manage(action="slice_unblocker", card_id=..., first_step_title=...)`.

## Step-by-Step Procedure
1. **Inspect Current Reality**:
   - Call `agile_manage(action="board_view", board_id="all-active-sprint")` and `calendar_manage(action="week")` to inspect active sprint slots, existing Epics (`epics`), backlog items, and upcoming deadlines.
2. **Capture Epics & Linked Child Cards Cleanly**:
   - Save multi-task personal goals once via `agile_manage(action="save_epic", title=..., board_id=..., horizon="next"|"later", target_date=...)`.
   - Save actionable child tasks via `agile_manage(action="save_card", board_id=..., epic_id=..., title=..., checklist=[...], effort_size=..., day_slot=..., status="backlog"|"sprint_todo", planned_date=..., due_date=...)`.
3. **Run 5-Minute Weekly Sprint Planning**:
   - Start or roll over the sprint (`start_sprint` / `rollover_sprint`), pull prioritized `backlog` cards into `sprint_todo`, check `capacity`, and block focus/appointment times via `calendar_manage(action="add_event", ...)`.
4. **Unblock Stalled & Procrastinated Cards**:
   - Use `agile_manage(action="slice_unblocker", card_id=..., first_step_title="...", day_slot="weekday")` to place a 15-minute starter card in `sprint_todo` ahead of an overwhelming parent task.
