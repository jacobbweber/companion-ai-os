# Feature F01 / Increment 03: Deterministic `max_tokens` Truncation Recovery, Tier Output Budgets & Live Tool-Stream Progress

- **Milestone:** `v5.1.2`
- **Status:** SHIPPED (Verified locally & on `Spark` `v5.1.2`)
- **Upstream Increment:** `docs/features/F01-greenfield-sdlc-bootstrap/inc-02-projects-root-and-folder-browser.md`

## 1. Root Cause & Anti-Sprawl Gate Verification

### Forensic Incident (`Spark` Trace `tr_1795807702001_01a373`)
During Guided Co-Design of `companion-origin` Stage 1 (`docs/product.md`), `qwen3.8:latest` (`reasoning` tier via Ollama `openai_compat`) generated `728` chars of `<think>` reasoning, streamed `"Writing product.md to disk. This is the foundation everything else builds on."` (`79` chars), and streamed ~`3,896` tokens of `<tool_call>` arguments for `write_file("docs/product.md", ...)`. Generation hit `COMPANION_MAX_TOKENS=4096` before `</tool_call>`:
1. Ollama discarded the unclosed server-buffered tool call (`tools_called=0`) and returned `finish_reason="length"`.
2. `_run_step_loop` ignored `finish_reason == "length"`, saw `tool_calls == []` and non-empty prose, and committed the 79-char sentence as a completed turn without writing `docs/product.md` or warning the user.
3. Similarly, raw `<tool_call>` streams truncated before `</tool_call>` leaked partial JSON into visible text, and streamed `delta.tool_calls` emitted zero live UI progress during multi-KB file synthesis.

### Pre-Flight Anti-Sprawl Gate Audit
1. **Gate 1 — Conceptual Compression:** Zero new Ring-0 modules (`25/25`), zero new SQLite tables, zero new `TOOLS` schemas (`18/18`), and zero new SSE event types (reuses `phase` and `text_delta`).
2. **Gate 2 — Single Source of Truth & `F5` / Restart Invariance:** `is_length_truncated` normalizes `length`, `max_tokens`, and `model_context_window_exceeded` across OpenAI/Ollama/vLLM, Anthropic, and Gemini dialects. Recovered multi-step prose joins cleanly in SQLite `conversations` so `F5` history matches live SSE output identically.
3. **Gate 3 — Full-Stack 5-Hop Seam Trace:** `.env` / `TierConfig` (`8192` default, `16384` `coding`/`reasoning`) -> `prepare_wire_request` + `heal_http_400_request` context-window clamp -> `read_completion_or_sse_stream` live `Drafting <tool> (X.X KB)` `phase` events -> `_run_step_loop` automatic continuation (up to 2 recoveries per turn) -> visible `⚠️` truncation banner if recovery budget is exhausted.
4. **Gate 4 — Human Mental Model & Progressive Disclosure:** All Ring-0 modules stay `<= 700` lines; normal turns have zero extra UI noise while long file-writing tool calls show calm live KB progress in `chat-live-activity`.

---

## Stage 4: Engineering Design

### 1. Contracts & Invariants

| Component | Function / Symbol | Contract & Edge-Case Behavior |
| :--- | :--- | :--- |
| `companion/config.py` | `_DEFAULT_TIER_MAX_TOKENS`, `Settings` | Global default `max_tokens = 8192`; `coding` and `reasoning` tiers default to `max(self.max_tokens, 16384)` unless `COMPANION_<TIER>_MAX_TOKENS` is explicitly set. |
| `companion/streaming.py` | `is_length_truncated(finish_reason)` | Returns `True` iff `finish_reason` (case-insensitive) is in `{"length", "max_tokens", "model_context_window_exceeded"}`. |
| `companion/streaming.py` | `strip_unclosed_tool_call_tail(text)` | Detects unclosed trailing `<tool_call>` or `<function=...` blocks, strips raw partial JSON/XML from visible output, and returns `(clean_prefix, True)` so `normalize_assistant_message` sets `_truncated_tool_call=True`. |
| `companion/streaming.py` & `companion/llm_wire.py` | `read_completion_or_sse_stream`, `_read_anthropic_sse` | Emits throttled `phase` progress (`Drafting <tool_name> (<N.N> KB)...`) on initial tool name and every `2048` bytes of streamed tool arguments. `heal_http_400_request` halves `max_tokens` (floor `1024`) when an endpoint rejects `max_tokens` as exceeding remaining context length. |
| `companion/llm.py` | `_post_completion` | Records `finish=<finish_reason>` in `llm_call` telemetry spans and logs `WARN` whenever `is_length_truncated(finish_reason)` triggers. |
| `companion/agent.py` | `_run_step_loop`, `_execute_tool_calls` | 1) If `tool_calls` is non-empty and truncated JSON fails `_parse_strict_tool_args`, appends a truncation-specific chunking hint. 2) If `tool_calls` is empty and `hit_max_tokens` is `True`, performs up to `2` automatic continuation steps instructing `<150`-word `<think>` and immediate execution/chunking. 3) If still truncated at exit, appends a visible `⚠️` truncation notice to both live stream and persisted message. |
