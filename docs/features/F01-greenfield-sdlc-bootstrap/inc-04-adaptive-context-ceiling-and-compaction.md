# Feature F01 / Increment 04: Live Endpoint Context-Ceiling Detection, 32k Output Budgets & Mid-Turn Auto-Compaction

- **Milestone:** `v5.1.3`
- **Status:** SHIPPED (Verified locally & on `Spark` `v5.1.3`)
- **Upstream Increment:** `docs/features/F01-greenfield-sdlc-bootstrap/inc-03-turn-truncation-recovery-and-output-budget.md`

## 1. Forensic Root Cause (`Spark` Traces `tr_1795831719701_76db08` & `tr_1795833885516_9fa429`)

After `v5.1.2`, Companion successfully wrote Stage 1 (`docs/product.md`), Stage 2 (`docs/architecture.md`, `docs/adr/ADRs.md`), and 21 Stage 3 feature/increment files (`F01`–`F06`). On the final 2 `F06` files (`msgs=85..97`), telemetry recorded:
- `tokens=65532..65536`, `finish=length`, `chars=0..42`, `think_chars=41..110`.
- `Ollama /api/ps` on `192.168.1.29:11434` showed `qwen3.8:latest` loaded with `"context_length": 65536` (`64K` total KV window), whereas `Spark`'s `.env` declared `COMPANION_REASONING_CONTEXT_WINDOW=262144` (`256K`).

Three compound engineering issues prevented seamless continuation:
1. **Configured vs. Live Runner Context Drift (`262,144` vs. `65,536`):** Companion waited for `75% * 262,144 = 196,608` tokens to auto-compact, so at `65,400` prompt tokens (`99.8%` of Ollama's actual `65,536` KV window), only ~`130` tokens remained before `finish_reason="length"`.
2. **Stage-1 -> Stage-2 Compaction Token-Density Bypass (`companion/context.py`):** `compact_messages_if_needed` incorporated `last_usage_tokens * 4` in `pre_chars`, but dropped `last_usage_tokens` in `post_stage1_chars` and blocked compaction whenever `len(messages) <= keep_recent + 2` even if giant spec messages exceeded the token trigger.
3. **Truncation Recovery Appending Without Compacting (`companion/agent.py`):** When `finish_reason="length"` was caused by context-window exhaustion (`completion_tokens << max_tokens`) rather than `max_tokens`, appending recovery prompts without running `compact_messages_if_needed(force=True)` left `0` headroom for the retry.

## 2. Stage 4 Contracts & Invariants

| Component | Symbol | Deterministic Contract |
| :--- | :--- | :--- |
| `companion/config.py` | `_DEFAULT_TIER_MAX_TOKENS`, `Settings` | Global default `max_tokens = 16384`; `coding` and `reasoning` default to `max(self.max_tokens, 32768)` unless `COMPANION_<TIER>_MAX_TOKENS` is explicitly set. |
| `companion/streaming.py` | `detect_context_ceiling_truncation`, `record_live_context_ceiling`, `get_live_context_ceiling` | Distinguishes `max_tokens` output caps from server context-window exhaustion (`completion_tokens < 0.75 * target_max_tokens` with `finish_reason="length"` -> `real_ceiling = total_tokens`) and caches live `(base_url, model)` runner `context_length` (populated proactively from Ollama `/api/ps` & model discovery). |
| `companion/context.py` | `effective_tier_context_window`, `compact_messages_if_needed` | Computes `min(configured_window, live_runner_ceiling)`; preserves `last_usage_tokens` density across Stage 1; dynamically reduces `eff_keep` when oversized recent messages exceed `55%` of the window. |
| `companion/agent.py` | `_run_step_loop` | When `ctx_ceil > 0` triggers mid-turn, immediately runs `compact_messages_if_needed(..., keep_recent=6, force=True)` (via `fast` tier + lossless `KeyTokens` verification) before continuing—reclaiming ~70–80% of the window automatically. |
