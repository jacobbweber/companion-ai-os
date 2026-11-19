---
name: proactive-checkin
description: Conduct a warm, plain-English executive check-in, daily goal review, and priority alignment with the user.
required_tools: [agile_manage, routine_manage, memory_manage, telemetry_manage]
---

# Proactive Check-In & Daily Goal Review Runbook

## When to Use This Skill
Use this runbook when the user asks for a check-in, asks "where do we stand?", starts a new day/session wanting an overview, or asks what you recommend working on next.

## Distinction: Runbook vs. CLI Snapshot
- The `/checkin` slash command (`check_in()` in `companion/agent.py`) is a deterministic CLI status printout.
- **This `SKILL.md` is your agentic runbook** for conducting a thoughtful, conversational executive review using your primitive tools.

## Step-by-Step Procedure
1. **Survey Active Goals & Work Items**:
   - Call `agile_manage(action="board_view")` to inspect active goals/projects, `human_pending` unblockers, `companion_ready` slices, and `blocked` items.
2. **Check Overnight / Idle Autonomy Activity**:
   - Call `routine_manage(action="autonomy_status")` (or `action="morning"` if unseen overnight routine or goal-slice runs exist) to see what ran autonomously.
3. **Review Recent Notes & Rollback Lessons**:
   - Call `memory_manage(action="get_notes", limit=5)` to recall recent user preferences, architectural notes, or rollback lessons.
4. **Synthesize in Plain Executive English**:
   - Present a warm, concise 3-part summary (never dumping raw database tables):
     1. **Where We Are**: System health, Background Autonomy posture, and anything accomplished since last check-in.
     2. **Top Priorities**: The top 1–2 ready or blocked work items in plain English.
     3. **Clear Recommendation**: One concrete, high-value next step you recommend tackling right now.
