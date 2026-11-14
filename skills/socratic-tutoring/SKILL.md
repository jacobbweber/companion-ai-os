---
name: socratic-tutoring
description: Conversational Socratic Tutor to Teach, Learn, Study, Quiz, and Test any Subject/Course/Module/Syllabus with Pedagogical Progression (Priming -> Dual Coding -> Elaboration -> Active Recall Retrieval), Spaced Repetition Mastery, Inline Chat Flashcards, and Wiki Study Notes.
required_tools: [education_manage, wiki_manage, memory_manage]
---

# Headless Education & Conversational Socratic Tutor (`socratic-tutoring`)

## When to Use This Skill
Activate this runbook whenever the user wants to learn a new subject, build a study syllabus, review due spaced-repetition items, practice quizzes conversationally, request flashcards in Chat, or curate permanent study guides into the Knowledge Wiki.

## Core Pedagogical Philosophy
- **Conversation Over Cluttered Dashboards**: You ARE the tutor. Conduct priming explanations, visual Mermaid diagrams, plain-language teach-back checks, and quiz questions directly in Forever Chat while keeping deterministic mastery state in SQLite via `education_manage`.
- **4-Stage Cognitive Sequence**: Guide each syllabus module through:
  1. `priming` — High-level mental map, why it matters, and how parts connect before jargon.
  2. `dual_coding` — Pair concise explanation with a clear `mermaid` architecture/flow diagram.
  3. `elaboration` — Ask the user to explain the mechanism in their own words or apply it to a real scenario; gently correct misconceptions.
  4. `retrieval` -> `mastered` — Test active recall via conversational quiz questions or inline flashcards, logging every grade to spaced repetition.

## Step-by-Step Procedure
1. **Check Active Course & Due Reviews**:
   - Call `education_manage(action="progress")` (and `education_manage(action="due")` if due count > 0) to ground your session in the user's current module and struggle areas.
2. **Design or Select Syllabus**:
   - When starting a new topic, call `education_manage(action="save_course", title=..., domain=..., goal_summary=..., modules=[...])` with 3–7 bite-sized modules.
3. **Teach & Capture Study Items**:
   - Deliver `priming` + `dual_coding` (`mermaid` block), advance stage via `education_manage(action="advance_stage", course_id=..., stage="elaboration")`, and register 3–6 high-signal recall prompts via `education_manage(action="upsert_items", course_id=..., items=[...])`.
   - When a module's mental model solidifies, save a permanent reference guide via `education_manage(action="curate_note", course_id=..., module_title=..., core_idea=..., mechanism_markdown=..., mermaid_diagram=...)`.
4. **Grade Quizzes & Surface Inline Chat Flashcards**:
   - For conversational quizzes: ask 1–3 questions in Chat, evaluate the user's reply, explain nuances, and call `education_manage(action="grade_item", item_id=..., passed=True|False, miss_reason=...)`.
   - When the user asks for flashcards or rapid drill: call `education_manage(action="flashcard_deck", course_id=..., limit=6)` and include its returned ```` ```companion-flashcards ... ``` ```` fenced block verbatim in your reply so interactive flip-cards render right inside Chat!
