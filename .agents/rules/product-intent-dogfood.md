# Mandatory Product-Intent & Live UX Dogfooding Rule

Whenever developing, fixing, "testing", "live testing", or "dogfooding" any feature or UI workflow in Companion:

1. **Actually Use the Live Tool (Unit Tests Are Plumbing Only)**: Never treat unit/Vitest tests or `{"ok": true}` status codes as proof that a feature works as intended. Always exercise the real running Web UI / CLI against a **real live LLM** and witness the end-to-end behavior firsthand.
2. **Semantic Output Audit**: Inspect the actual human-facing result—exact folder names, file slugs, YAML frontmatter, prose summaries, tags, ontology concepts, and UI badges. Reject mechanical shortcuts (such as `01-inbox` destination folders, template-name filenames, raw bullet-dump summaries, or leftover `#inbox` tags).
3. **Everyday Non-Tech Domain Check ("Chicken Stir-Fry Rule")**: Verify general features (Wiki, Boards, Calendar, Chat, Memory) using everyday non-software topics (cooking, home, hobbies) so developer/homelab biases (`operational-runbook`, `preventive-maintenance`) never leak onto personal content.
4. **Verb-Promise Audit**: UI buttons that imply judgment (`Graduate`, `Curate`, `Organize`, `Plan`, `Recommend`, `Slice`) must invoke genuine context-aware reasoning and validation rather than dumb regex/string moves, and every post-click UI state must be visually inspected via real browser screenshots.
