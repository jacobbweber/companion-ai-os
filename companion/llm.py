"""
Resilient Multi-Provider & Multi-Dialect LLM Client (A-Frame Infrastructure Layer).

Uses Python standard library urllib.request across `openai_compat`, `anthropic_messages`, and
`gemini_native` dialects (`companion.llm_wire`), enforcing `local_only` LAN Privacy Lock,
per-endpoint `ModelGate` swap scheduling, `<think>` extraction, and `<tool_call>` rescue.
"""

import contextlib, contextvars, json, re, sys, threading, time, urllib.error, urllib.request, uuid  # noqa: E401
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from companion.config import CAPABILITY_TIERS, TierConfig, settings
from companion.llm_wire import (
    build_openai_resource_url, heal_http_400_request, inject_openai_turn_attachments,
    is_local_or_lan_endpoint, prepare_wire_request, read_wire_response, urlopen_safe,
)
from companion.streaming import (
    check_cancelled, emit_stream_event, get_active_cancel_scope,
    has_stream_sink, rescue_text_tool_calls, strip_thinking_tags,
)
from companion.telemetry import telemetry


_CALL_PRIORITY: contextvars.ContextVar[str] = contextvars.ContextVar("companion_llm_priority", default="foreground")
__all__ = [
    "ModelGate", "_sanitize_wire_messages", "active_swap_status", "chat", "check_model_endpoints",
    "current_tier_name", "get_endpoint_gate", "model_gate", "normalize_assistant_message",
    "recover_empty_synthesis", "rescue_text_tool_calls", "tier_context",
]
_ABORT_CHECK: contextvars.ContextVar[Optional[Callable[[], bool]]] = contextvars.ContextVar("companion_llm_abort_check", default=None)
_ACTIVE_TIER: contextvars.ContextVar[str] = contextvars.ContextVar("companion_llm_tier", default="")
_GLOBAL_FG_LOCK = threading.Lock()
_GLOBAL_FG_COUNT = 0
_GLOBAL_FG_THREADS: Dict[int, int] = {}


class ModelGate:
    """Per-endpoint `BoundedSemaphore` concurrency, foreground-priority & VRAM-swap execution gate."""

    def __init__(self, max_concurrency: int = 1) -> None:
        self._cond = threading.Condition()
        self._max_concurrency = max(1, min(8, int(max_concurrency or 1)))
        self._semaphore = threading.BoundedSemaphore(self._max_concurrency)
        self._in_flight = 0
        self._thread_depth: Dict[int, int] = {}
        self._fg_active = 0
        self._bg_waiting = 0
        self._bg_in_flight = 0
        self.loaded_model: Optional[str] = None
        self.loaded_checked_mono: float = 0.0
        self._swap_info: Optional[Dict[str, Any]] = None
        self._health_unsupported: bool = False

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    def set_max_concurrency(self, limit: int) -> None:
        eff = max(1, min(8, int(limit or 1)))
        with self._cond:
            if self._in_flight == 0 and self._max_concurrency != eff:
                self._max_concurrency = eff
                self._semaphore = threading.BoundedSemaphore(eff)
                self._cond.notify_all()

    @contextlib.contextmanager
    def foreground_turn(self) -> Iterator[None]:
        """Mark an interactive user turn as active; background LLM calls wait until it finishes."""
        global _GLOBAL_FG_COUNT
        tid = threading.get_ident()
        tok = _CALL_PRIORITY.set("foreground")
        with _GLOBAL_FG_LOCK:
            _GLOBAL_FG_COUNT += 1
            _GLOBAL_FG_THREADS[tid] = _GLOBAL_FG_THREADS.get(tid, 0) + 1
        with self._cond:
            self._fg_active += 1
            self._cond.notify_all()
        try:
            yield
        finally:
            with _GLOBAL_FG_LOCK:
                _GLOBAL_FG_COUNT = max(0, _GLOBAL_FG_COUNT - 1)
                rem = _GLOBAL_FG_THREADS.get(tid, 1) - 1
                if rem <= 0:
                    _GLOBAL_FG_THREADS.pop(tid, None)
                else:
                    _GLOBAL_FG_THREADS[tid] = rem
            with self._cond:
                self._fg_active = max(0, self._fg_active - 1)
                self._cond.notify_all()
            with _GATES_LOCK:
                extra_gates = list(_ENDPOINT_GATES.values())
            for g in extra_gates:
                with g._cond:
                    g._cond.notify_all()
            _CALL_PRIORITY.reset(tok)

    @contextlib.contextmanager
    def background_context(self, abort_check: Optional[Callable[[], bool]] = None, priority: str = "background") -> Iterator[None]:
        """Run a block (idle routine at `'background'` or card worker at `'foreground'`) with optional abort check."""
        p_tok = _CALL_PRIORITY.set((priority or "background").strip().lower())
        a_tok = _ABORT_CHECK.set(abort_check)
        try:
            yield
        finally:
            _ABORT_CHECK.reset(a_tok)
            _CALL_PRIORITY.reset(p_tok)

    @contextlib.contextmanager
    def _acquire_slot_cancelable(self, limit: Optional[int] = None, exclusive: bool = False) -> Iterator[None]:
        tid = threading.get_ident()
        with self._cond:
            depth = self._thread_depth.get(tid, 0)
            if depth > 0:
                self._thread_depth[tid] = depth + 1
                reentrant = True
            else:
                reentrant = False
        if reentrant:
            try:
                yield
            finally:
                with self._cond:
                    rem = self._thread_depth.get(tid, 1) - 1
                    if rem <= 0:
                        self._thread_depth.pop(tid, None)
                    else:
                        self._thread_depth[tid] = rem
            return

        eff_limit = 1 if exclusive else max(1, min(8, int(limit if limit is not None else self._max_concurrency)))
        with self._cond:
            while True:
                check_cancelled()
                abort_fn = _ABORT_CHECK.get()
                if abort_fn is not None and abort_fn():
                    raise InterruptedError("LLM call aborted while waiting for endpoint semaphore.")
                must_drain = (exclusive and self._in_flight > 0) or (eff_limit != self._max_concurrency and self._in_flight > 0)
                if not must_drain:
                    if self._max_concurrency != eff_limit:
                        self._max_concurrency = eff_limit
                        self._semaphore = threading.BoundedSemaphore(eff_limit)
                    if self._semaphore.acquire(blocking=False):
                        sem = self._semaphore
                        self._in_flight += 1
                        self._thread_depth[tid] = 1
                        break
                self._cond.wait(timeout=0.02)
        try:
            yield
        finally:
            with self._cond:
                self._thread_depth.pop(tid, None)
                self._in_flight = max(0, self._in_flight - 1)
                sem.release()
                self._cond.notify_all()

    @contextlib.contextmanager
    def acquire_call(
        self, priority: Optional[str] = None, serialize: bool = True,
        max_concurrency: Optional[int] = None, exclusive: bool = False,
    ) -> Iterator[None]:
        """Acquire a `BoundedSemaphore` endpoint slot while yielding `'background'` idle calls to foreground turns."""
        check_cancelled()
        eff_prio = (priority or _CALL_PRIORITY.get() or "foreground").lower()
        abort_fn = _ABORT_CHECK.get()
        tid = threading.get_ident()
        with _GLOBAL_FG_LOCK:
            on_fg_thread = tid in _GLOBAL_FG_THREADS
        if eff_prio != "background" or on_fg_thread:
            if serialize:
                with self._acquire_slot_cancelable(limit=max_concurrency, exclusive=exclusive):
                    yield
            else:
                yield
            return

        with self._cond:
            self._bg_waiting += 1
            try:
                while True:
                    check_cancelled()
                    if abort_fn is not None and abort_fn():
                        raise InterruptedError("Background LLM call preempted by foreground user activity.")
                    with _GLOBAL_FG_LOCK:
                        any_fg = _GLOBAL_FG_COUNT > 0
                    if self._fg_active == 0 and not any_fg:
                        break
                    self._cond.wait(timeout=0.02)
                self._bg_in_flight += 1
            finally:
                self._bg_waiting = max(0, self._bg_waiting - 1)
        try:
            if serialize:
                with self._acquire_slot_cancelable(limit=max_concurrency, exclusive=exclusive):
                    yield
            else:
                yield
        finally:
            with self._cond:
                self._bg_in_flight = max(0, self._bg_in_flight - 1)
                self._cond.notify_all()

    def begin_swap(self, from_model: Optional[str], to_model: str, base_url: str) -> None:
        with self._cond:
            self._swap_info = {"swapping": True, "from_model": from_model or "unloaded", "to_model": to_model, "base_url": base_url, "started_mono": time.monotonic()}

    def end_swap(self, loaded_model: Optional[str] = None) -> None:
        with self._cond:
            if loaded_model:
                self.loaded_model, self.loaded_checked_mono = loaded_model, time.monotonic()
            self._swap_info = None

    def swap_status(self) -> Optional[Dict[str, Any]]:
        with self._cond:
            if not self._swap_info:
                return None
            elapsed = round(max(0.0, time.monotonic() - float(self._swap_info["started_mono"])), 1)
            return {"swapping": True, "from_model": self._swap_info["from_model"], "to_model": self._swap_info["to_model"], "base_url": self._swap_info["base_url"], "elapsed_seconds": elapsed}

    def status(self) -> Dict[str, Any]:
        with self._cond:
            return {
                "foreground_active": self._fg_active, "background_waiting": self._bg_waiting,
                "background_in_flight": self._bg_in_flight, "in_flight": self._in_flight,
                "max_concurrency": self._max_concurrency, "loaded_model": self.loaded_model, "swap": self.swap_status(),
            }


model_gate = ModelGate()
_ENDPOINT_GATES: Dict[str, ModelGate] = {}
_GATES_LOCK = threading.Lock()


def get_endpoint_gate(base_url: Optional[str] = None) -> ModelGate:
    """Return the `ModelGate` for `base_url` (sharing `model_gate` for the default endpoint, isolating separate boxes)."""
    norm = (base_url or settings.base_url).rstrip("/").lower()
    if norm == settings.base_url.rstrip("/").lower():
        return model_gate
    with _GATES_LOCK:
        if norm not in _ENDPOINT_GATES:
            _ENDPOINT_GATES[norm] = ModelGate()
        return _ENDPOINT_GATES[norm]


def active_swap_status() -> Optional[Dict[str, Any]]:
    """Return active Docker/VRAM model-swap metadata across any endpoint gate, if a swap is in progress."""
    st = model_gate.swap_status()
    if st:
        return st
    with _GATES_LOCK:
        gates = list(_ENDPOINT_GATES.values())
    for g in gates:
        st = g.swap_status()
        if st:
            return st
    return None


def probe_gateway_loaded_model(base_url: str, api_key: str = "", timeout: float = 1.2) -> Tuple[Optional[str], Optional[bool]]:
    """Query swap-gateway `/health` (e.g. vLLM swap gateway) for `(loaded_model, upstream_healthy)`."""
    headers = {"User-Agent": "companion-os/2.3", **({"Authorization": f"Bearer {api_key}"} if api_key else {})}
    try:
        with urlopen_safe(urllib.request.Request(f"{base_url.rstrip('/')}/health", method="GET", headers=headers), timeout=timeout, allow_private_lan=True, allow_redirects=False) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        if isinstance(data, dict) and ("loaded_model" in data or "upstream_healthy" in data):
            loaded = str(data["loaded_model"]) if data.get("loaded_model") else None
            healthy = bool(data["upstream_healthy"]) if "upstream_healthy" in data else True
            return loaded, healthy
    except Exception:
        pass
    return None, None


@contextlib.contextmanager
def tier_context(tier: Optional[str]) -> Iterator[None]:
    """Bind a logical model tier (`fast`, `reasoning`, `audio`, `image`, `video`) for nested LLM calls."""
    clean = (tier or "").strip().lower()
    if not clean or clean == "auto":
        yield
        return
    tok = _ACTIVE_TIER.set(clean)
    try:
        yield
    finally:
        _ACTIVE_TIER.reset(tok)


def current_tier_name() -> str:
    """Return the active contextvar model tier (or `'fast'` if unset)."""
    val = (_ACTIVE_TIER.get() or "").strip().lower()
    return val if val and val != "auto" else "fast"


def discover_provider_models(base_url: str, api_key: str = "", dialect: str = "openai_compat", timeout: float = 3.5) -> Dict[str, Any]:
    """Probe live provider health and discover served models (`openai_compat` + Ollama `/api/ps`, `anthropic_messages`, `gemini_native`)."""
    d, base, t0 = (dialect or "openai_compat").strip().lower(), (base_url or "").strip().rstrip("/"), time.perf_counter()
    if settings.effective_privacy_mode() == "local_only" and not is_local_or_lan_endpoint(base):
        return {"ok": False, "latency_ms": 0.0, "status_detail": "BLOCKED (`local_only` Privacy Lock)", "models": []}
    try:
        if d == "anthropic_messages":
            m_url = f"{base}/models" if base.lower().endswith("/v1") else f"{base}/v1/models"
            hdrs = {"User-Agent": "companion-os/3.3", "anthropic-version": "2023-06-01", **({"x-api-key": api_key} if api_key else {})}
            with urlopen_safe(urllib.request.Request(m_url, method="GET", headers=hdrs), timeout=timeout, allow_private_lan=True, allow_redirects=False) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace"))
            ms = round((time.perf_counter() - t0) * 1000.0, 1)
            items = [{"id": str(m["id"]), "label": str(m.get("display_name") or m["id"]), "loaded": False, "context_window": 200000} for m in (data.get("data") or []) if isinstance(m, dict) and m.get("id")]
        elif d == "gemini_native":
            g_base = base if base.lower().endswith(("/v1beta", "/v1alpha", "/v1")) else f"{base}/v1beta"
            hdrs = {"User-Agent": "companion-os/3.3", **({"x-goog-api-key": api_key} if api_key else {})}
            with urlopen_safe(urllib.request.Request(f"{g_base}/models", method="GET", headers=hdrs), timeout=timeout, allow_private_lan=True, allow_redirects=False) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace"))
            ms = round((time.perf_counter() - t0) * 1000.0, 1)
            items = [
                {"id": str(m["name"]).removeprefix("models/"), "label": str(m.get("displayName") or str(m["name"]).removeprefix("models/")), "loaded": False, "context_window": int(m.get("inputTokenLimit") or 1048576)}
                for m in (data.get("models") or [])
                if isinstance(m, dict) and m.get("name") and (not m.get("supportedGenerationMethods") or any(x in m["supportedGenerationMethods"] for x in ("generateContent", "embedContent")))
            ]
        else:
            m_url = build_openai_resource_url(base, "models")
            hdrs = {"User-Agent": "companion-os/3.3", **({"Authorization": f"Bearer {api_key}"} if api_key else {})}
            if "openrouter.ai" in base.lower():
                hdrs["HTTP-Referer"], hdrs["X-Title"] = "https://companion.local", "Companion AI OS"
            with urlopen_safe(urllib.request.Request(m_url, method="GET", headers=hdrs), timeout=timeout, allow_private_lan=True, allow_redirects=False) as r:
                raw = r.read().decode("utf-8", errors="replace")
            ms = round((time.perf_counter() - t0) * 1000.0, 1)
            data = json.loads(raw) if raw.strip().startswith(("{", "[")) else {}
            raw_models = [m for m in (data if isinstance(data, list) else (data.get("data") or [])) if isinstance(m, dict) and m.get("id")]
            loaded_ids = {str(m["id"]) for m in raw_models if m.get("loaded")}
            if raw_models and not loaded_ids and is_local_or_lan_endpoint(base):
                ps_root = re.sub(r"/(?:v1|api/v1)$", "", base, flags=re.I)
                try:
                    with urlopen_safe(urllib.request.Request(f"{ps_root}/api/ps", method="GET", headers=hdrs), timeout=0.8, allow_private_lan=True, allow_redirects=False) as ps_r:
                        ps_data = json.loads(ps_r.read().decode("utf-8", errors="replace"))
                    loaded_ids = {str(x.get("name") or x.get("model")) for x in (ps_data.get("models") or []) if isinstance(x, dict)}
                except Exception:
                    pass
            raw_models.sort(key=lambda m: str(m["id"]) not in loaded_ids)
            items = [{"id": str(m["id"]), "label": f"{m['id']} (loaded)" if str(m["id"]) in loaded_ids else str(m["id"]), "loaded": str(m["id"]) in loaded_ids, "context_window": int(m.get("context_length") or m.get("max_model_len") or 0)} for m in raw_models]
        top_labels = [x["label"] if x.get("loaded") else x["id"] for x in items[:3]]
        return {"ok": True, "latency_ms": ms, "status_detail": f"ONLINE ({ms:.0f}ms{f', serving: {', '.join(top_labels)}' if top_labels else ''})", "models": items}
    except Exception as exc:
        ms = round((time.perf_counter() - t0) * 1000.0, 1)
        reason = f"HTTP {exc.code} {getattr(exc, 'reason', exc)}" if hasattr(exc, "code") else str(getattr(exc, "reason", exc))
        return {"ok": False, "latency_ms": ms, "status_detail": f"OFFLINE ({reason})", "models": []}


def probe_endpoint(base_url: str, api_key: str = "", timeout: float = 2.5, dialect: str = "openai_compat") -> Tuple[bool, str]:
    """Perform a fast health check against a provider endpoint and return `(online, status_detail)`."""
    res = discover_provider_models(base_url, api_key=api_key, dialect=dialect, timeout=timeout)
    return bool(res["ok"]), str(res["status_detail"])


def format_model_tiers_status(active_mode: str = "auto", check_health: bool = False) -> str:
    """Format a human-readable summary of all capability tiers (`default`, `fast`, `coding`, `reasoning`, `image`, `audio`, `video`, `embedding`)."""
    mode, priv = (active_mode or "auto").strip().lower(), settings.effective_privacy_mode()
    lines = [
        f"=== Companion Multi-Tier Model Router (Mode: `{mode}` | Privacy: `{priv}`) ===",
        "Tiers inherit `default` unless customized in System -> Models, Tiers & Context (`/providers`) or `.env`.",
        "",
    ]
    probe_cache: Dict[Tuple[str, str], Tuple[bool, str]] = {}
    for t_name in CAPABILITY_TIERS:
        cfg: TierConfig = settings.resolve_tier(t_name)
        src_badge = f"DEDICATED:{cfg.dialect}" if cfg.is_dedicated else f"INHERITED:{cfg.dialect}"
        eff_tag = f" | effort={cfg.reasoning_effort}" if cfg.reasoning_effort else ""
        fb_tag = f" | fallback={cfg.fallback_model}" if cfg.fallback_model else ""
        health_str = ""
        if check_health:
            ckey = (cfg.base_url, cfg.dialect)
            if ckey not in probe_cache:
                probe_cache[ckey] = probe_endpoint(cfg.base_url, cfg.api_key, dialect=cfg.dialect)
            ok, detail = probe_cache[ckey]
            if not ok and t_name != "default" and (cfg.fallback_model or cfg.base_url != settings.base_url.rstrip("/")):
                detail += f" -> FAILOVER to `{cfg.fallback_model or 'default'}`"
            health_str = f" | Health: {detail}"
        lines.append(
            f"  * `{t_name:<9}` : `{cfg.model}` @ `{cfg.base_url}` "
            f"(ctx={cfg.context_window_tokens:,} | temp={cfg.temperature:.2f}{eff_tag}{fb_tag} | {src_badge}){health_str}"
        )
    lines.append(
        "\nCommands: `/model status`, `/model check` (live ping), `/model auto|<tier>`, `/providers` (provider & Vault matrix)."
    )
    return "\n".join(lines)


def _sanitize_wire_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Backward-compatible helper delegating OpenAI attachment/system normalization to `companion.llm_wire`."""
    return inject_openai_turn_attachments(messages)


def normalize_assistant_message(msg_obj: Dict[str, Any]) -> Tuple[str, str, List[Dict[str, Any]]]:
    """Normalize `msg_obj` in place: rescue `<tool_call>` / `<function=...>`, strip `<think>`, and ensure unique IDs."""
    raw_content = str(msg_obj.get("content") or "")
    raw_reasoning = str(msg_obj.get("reasoning_content") or msg_obj.get("reasoning") or msg_obj.get("thinking") or "").strip()
    existing_tcalls = list(msg_obj.get("tool_calls") or [])
    if not existing_tcalls and ("<tool_call>" in raw_content.lower() or "<function=" in raw_content.lower()):
        raw_content, rescued_tcalls = rescue_text_tool_calls(raw_content)
        if rescued_tcalls:
            existing_tcalls = rescued_tcalls
            if telemetry.active_trace_id:
                telemetry.log("INFO", "llm", f"Rescued {len(rescued_tcalls)} text tool call(s) from assistant content.")
    if not existing_tcalls and ("<tool_call>" in raw_reasoning.lower() or "<function=" in raw_reasoning.lower()):
        raw_reasoning, rescued_r_tcalls = rescue_text_tool_calls(raw_reasoning)
        if rescued_r_tcalls:
            existing_tcalls = rescued_r_tcalls
            if telemetry.active_trace_id:
                telemetry.log("INFO", "llm", f"Rescued {len(rescued_r_tcalls)} text tool call(s) from reasoning trace.")

    clean_content, tag_thinking = strip_thinking_tags(raw_content)
    clean_content = re.sub(r"</?tool_call>", "", clean_content, flags=re.IGNORECASE).strip()
    clean_reas, inner_reas = strip_thinking_tags(raw_reasoning)
    eff_reas = "\n\n".join(x for x in (inner_reas, clean_reas) if x).strip()
    parts: List[str] = []
    for chunk in (eff_reas, tag_thinking):
        if chunk and chunk not in parts:
            parts.append(chunk)
    combined_thinking = "\n\n".join(parts)

    if existing_tcalls:
        seen_ids: set[str] = set()
        norm_tcalls: List[Dict[str, Any]] = []
        for tc in existing_tcalls:
            tc_copy = dict(tc) if isinstance(tc, dict) else {"type": "function", "function": {"name": "unknown", "arguments": "{}"}}
            raw_id = str(tc_copy.get("id") or "").strip()
            eff_id = raw_id if (raw_id and raw_id.lower() != "null" and raw_id not in seen_ids) else f"call_{uuid.uuid4().hex[:8]}"
            seen_ids.add(eff_id)
            tc_copy["id"] = eff_id
            tc_copy.setdefault("type", "function")
            norm_tcalls.append(tc_copy)
        existing_tcalls = norm_tcalls
        msg_obj["tool_calls"] = existing_tcalls

    msg_obj["content"] = clean_content
    if combined_thinking:
        msg_obj["thinking"] = combined_thinking
    return clean_content, combined_thinking, existing_tcalls


def recover_empty_synthesis(messages: List[Dict[str, Any]], chat_fn: Callable[..., Dict[str, Any]], prior_thinking: str = "") -> str:
    """Retry once (`tools=None`) when an LLM returns an empty or `<think>`-truncated completion (`t81` guard)."""
    thought_ctx = f"\n[COMPLETED INTERNAL REASONING]\n{prior_thinking[-1600:]}\n" if prior_thinking.strip() else ""
    prompt_text = (
        f"{thought_ctx}Your previous response contained no visible answer text. Do NOT call tools or emit `<think>` tags; "
        "write a concise, direct plain-English response immediately based on the context above."
    )
    if messages and messages[-1].get("role") == "tool":
        retry_msgs = messages + [
            {"role": "assistant", "content": "I have reviewed the tool outputs."},
            {"role": "user", "content": prompt_text},
        ]
    elif messages and messages[-1].get("role") == "user":
        retry_msgs = messages + [
            {"role": "assistant", "content": "Understood."},
            {"role": "user", "content": prompt_text},
        ]
    else:
        retry_msgs = messages + [{"role": "user", "content": prompt_text}]
    try:
        retry_data = chat_fn(retry_msgs, None, "none")
        retry_msg = (retry_data.get("choices") or [{}])[0].get("message") or {}
        clean_retry, _, _ = normalize_assistant_message(retry_msg)
        if clean_retry.strip():
            if telemetry.active_trace_id:
                telemetry.log("WARN", "llm", "Recovered from empty LLM synthesis on 1-shot retry (t81 guard).")
            return clean_retry.strip()
    except InterruptedError:
        raise
    except Exception as exc:
        if telemetry.active_trace_id:
            telemetry.log("WARN", "llm", f"Empty synthesis retry failed: {exc}")

    for m in reversed(messages):
        if m.get("role") == "tool" and str(m.get("content") or "").strip():
            snippet = str(m["content"]).strip()[:400]
            fb_out = f"Completed the requested operation. Latest tool result:\n{snippet}"
            if has_stream_sink():
                emit_stream_event({"type": "text_delta", "delta": fb_out})
            return fb_out
    fb_msg = (
        "The model completed internal reasoning without emitting visible reply text. Please click Retry or reply `continue`."
        if prior_thinking.strip()
        else "Completed the requested check. Let me know how you would like to proceed."
    )
    if has_stream_sink():
        emit_stream_event({"type": "text_delta", "delta": fb_msg})
    return fb_msg


def _fail_llm(span_label: str, t0: float, msg: str) -> RuntimeError:
    """Record an error span and log entry in telemetry, then return a RuntimeError."""
    dur_ms = (time.perf_counter() - t0) * 1000.0
    telemetry.record_span("llm_call", span_label, dur_ms, status="error", detail=msg[:200])
    telemetry.log("ERROR", "llm", msg[:300])
    return RuntimeError(msg)


def _endpoint_shares_tiers(base_url: str) -> bool:
    norm = base_url.rstrip("/").lower()
    models = {settings.resolve_tier(t).model for t in ("default", "fast", "coding", "reasoning") if settings.resolve_tier(t).base_url.rstrip("/").lower() == norm}
    return len(models) > 1


def _resolve_swap_window(gate: ModelGate, base_url: str, model: str, api_key: str, base_timeout: int) -> Tuple[int, bool]:
    """Detect whether `model` triggers a container/VRAM swap on `base_url` and promote timeout to `swap_timeout`."""
    shares = _endpoint_shares_tiers(base_url) or settings.tiers_share_endpoint("fast", "reasoning") or settings.tiers_share_endpoint("default", "coding")
    if not shares or gate._health_unsupported or ":11434" in base_url:
        gate._health_unsupported = gate._health_unsupported or (":11434" in base_url)
        return base_timeout, False
    stale = (time.monotonic() - gate.loaded_checked_mono) > 15.0
    if gate.loaded_model == model and not stale:
        return base_timeout, False
    prev = gate.loaded_model
    if prev is None or stale:
        remote_loaded, healthy = probe_gateway_loaded_model(base_url, api_key)
        if remote_loaded is None and healthy is None:
            gate._health_unsupported = True
            return base_timeout, False
    else:
        remote_loaded, healthy = prev, True
    if remote_loaded == model and healthy is not False:
        gate.end_swap(model)
        return base_timeout, False
    swap_to = max(base_timeout, settings.swap_timeout)
    if (remote_loaded and remote_loaded != model) or healthy is False:
        gate.begin_swap(remote_loaded, model, base_url)
        emit_stream_event({"type": "model_swap", "from_model": remote_loaded or "cold", "to_model": model})
        telemetry.log("INFO", "llm", f"Model swap detected on {base_url}: {remote_loaded or 'cold'} -> {model} (timeout={swap_to}s)")
        print(f"  [gpu-swap] Switching model on {base_url}: {remote_loaded or 'cold'} -> {model}...", file=sys.stderr, flush=True)
        return swap_to, True
    return swap_to, False


def _resolve_ollama_fallback_or_hint(base_url: str, model: str, api_key: str) -> Tuple[Optional[str], str]:
    """Auto-detect an installed non-embedding Ollama model on HTTP 404, or return a plain-English setup hint."""
    disc = discover_provider_models(base_url, api_key=api_key, dialect="openai_compat", timeout=2.0)
    if disc.get("ok"):
        candidates = [str(m["id"]) for m in (disc.get("models") or []) if isinstance(m, dict) and m.get("id") and "embed" not in str(m["id"]).lower()]
        if candidates and candidates[0] != model:
            return candidates[0], ""
    return None, f"Ollama is running at `{base_url}`, but model `{model}` is not pulled yet. Run `ollama pull {model}` or choose a model in System -> Models."


def _post_completion(
    messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]], tool_choice: str,
    target_base_url: str, target_model: str, target_key: str, target_temp: float, target_max_tokens: int,
    target_timeout: int, max_retries: int, priority: Optional[str], tier_label: str,
    dialect: str = "openai_compat", reasoning_effort: str = "", has_tier_fallback: bool = False,
    max_concurrency: Optional[int] = None,
) -> Dict[str, Any]:
    """Execute one bounded-concurrency HTTP completion cycle across OpenAI/Anthropic/Gemini dialects with Privacy Lock & swap guard."""
    eff_dialect, span_label, call_start = (dialect or "openai_compat").strip().lower(), (f"{tier_label}:{target_model}" if tier_label else target_model), time.perf_counter()
    if settings.effective_privacy_mode() == "local_only" and not is_local_or_lan_endpoint(target_base_url):
        raise _fail_llm(span_label, call_start, f"Privacy Lock (`local_only`) blocked outbound cloud LLM call to `{target_base_url}` (`{target_model}`).")
    from companion.config import is_in_agent_tool_turn

    want_stream = bool(has_stream_sink() and settings.stream_tokens and not is_in_agent_tool_turn())
    target_url, headers, body = prepare_wire_request(eff_dialect, messages, tools, tool_choice, target_base_url, target_model, target_key, target_temp, target_max_tokens, reasoning_effort=reasoning_effort, stream=want_stream)
    payload, gate, scope = json.dumps(body).encode("utf-8"), get_endpoint_gate(target_base_url), get_active_cancel_scope()
    is_local_oai = eff_dialect == "openai_compat" and is_local_or_lan_endpoint(target_base_url)
    healed_400, tried_ollama_fb = False, False
    eff_conc = max(1, min(8, int(max_concurrency if max_concurrency is not None else (1 if ":11434" in target_base_url else 4))))
    shares_multi_model = is_local_oai and _endpoint_shares_tiers(target_base_url) and not gate._health_unsupported and ":11434" not in target_base_url
    swap_excl = bool(shares_multi_model and (gate.swap_status() is not None or (gate.loaded_model is not None and gate.loaded_model != target_model)))

    with gate.acquire_call(priority=priority, serialize=True, max_concurrency=eff_conc, exclusive=swap_excl):
        eff_timeout, is_swap = _resolve_swap_window(gate, target_base_url, target_model, target_key, target_timeout) if is_local_oai else (target_timeout, False)
        max_attempts, attempt = (max(max_retries + 1, 8) if is_swap else (max_retries + 1)), 0
        try:
            while attempt < max_attempts:
                check_cancelled()
                req = urllib.request.Request(target_url, data=payload, method="POST", headers=headers)
                try:
                    with urlopen_safe(req, timeout=eff_timeout, allow_private_lan=True, allow_redirects=False) as resp:
                        with (scope.track_response(resp) if scope is not None else contextlib.nullcontext()):
                            data = read_wire_response(eff_dialect, resp)
                    if not isinstance(data, dict) or not data.get("choices"):
                        raise RuntimeError(f"Malformed LLM response (missing choices): {str(data)[:300]}")
                    gate.end_swap(target_model)
                    dur_ms = (time.perf_counter() - call_start) * 1000.0
                    if is_swap:
                        print(f"  [gpu-swap] Model {target_model} loaded and responded ({dur_ms / 1000.0:.1f}s).", file=sys.stderr, flush=True)
                    clean_content, thinking_text, tcalls = normalize_assistant_message(data["choices"][0].get("message") or {})
                    toks = (data.get("usage") or {}).get("total_tokens", 0) if isinstance(data.get("usage"), dict) else 0
                    detail = f"tier={tier_label} dialect={eff_dialect} temp={target_temp:.2f}{' swapped=1' if is_swap else ''} msgs={len(messages)} tools_called={len(tcalls)} tokens={toks} chars={len(clean_content)} think_chars={len(thinking_text)}"
                    telemetry.record_span("llm_call", span_label, dur_ms, status="ok", detail=detail)
                    return data
                except InterruptedError:
                    raise
                except urllib.error.HTTPError as exc:
                    check_cancelled()
                    err_body = exc.read().decode("utf-8", errors="replace")[:500] if hasattr(exc, "read") else ""
                    if exc.code == 400 and not healed_400:
                        healed = heal_http_400_request(eff_dialect, target_url, body, err_body)
                        if healed is not None:
                            healed_400, (target_url, body) = True, healed
                            payload = json.dumps(body).encode("utf-8")
                            continue
                    if exc.code == 404 and is_local_oai and ":11434" in target_base_url and not tried_ollama_fb and not has_tier_fallback:
                        tried_ollama_fb = True
                        fb_model, hint = _resolve_ollama_fallback_or_hint(target_base_url, target_model, target_key)
                        if fb_model:
                            telemetry.log("INFO", "llm", f"Ollama model `{target_model}` not found; auto-selecting installed `{fb_model}`.")
                            target_model, body["model"] = fb_model, fb_model
                            payload = json.dumps(body).encode("utf-8")
                            continue
                        err_body = f"{err_body} ({hint})".strip()
                    err_text = f"HTTP {exc.code} {exc.reason}: {err_body}"
                    if exc.code in (408, 429, 500, 502, 503, 504) and attempt + 1 < max_attempts and (time.perf_counter() - call_start) < eff_timeout:
                        attempt += 1
                        telemetry.log("WARN", "llm", f"Retry {attempt}/{max_attempts - 1} on {err_text[:180]}")
                        time.sleep(5.0 if is_swap else 1.0 * attempt)
                        continue
                    raise _fail_llm(span_label, call_start, err_text)
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    check_cancelled()
                    err_text = f"could not reach {target_base_url}: {getattr(exc, 'reason', str(exc))}"
                    if attempt + 1 < max_attempts and (time.perf_counter() - call_start) < eff_timeout:
                        attempt += 1
                        telemetry.log("WARN", "llm", f"Retry {attempt}/{max_attempts - 1} on {err_text[:180]}")
                        time.sleep(5.0 if is_swap else 1.0 * attempt)
                        continue
                    if ":11434" in target_base_url:
                        err_text += " (Tip: Start Ollama and run `ollama pull qwen2.5:7b`, or configure your LLM provider in System -> Models.)"
                    raise _fail_llm(span_label, call_start, err_text)
                except RuntimeError as exc:
                    check_cancelled()
                    raise _fail_llm(span_label, call_start, str(exc))
                except Exception as exc:
                    check_cancelled()
                    raise _fail_llm(span_label, call_start, f"LLM request failed: {type(exc).__name__}: {exc}")
        finally:
            gate.end_swap()
    raise RuntimeError("LLM request failed after retries")


def chat(
    messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None, tool_choice: str = "auto",
    base_url: Optional[str] = None, model: Optional[str] = None, api_key: Optional[str] = None,
    temperature: Optional[float] = None, max_tokens: Optional[int] = None, http_timeout: Optional[int] = None,
    max_retries: int = 2, priority: Optional[str] = None, tier: Optional[str] = None,
    dialect: Optional[str] = None, reasoning_effort: Optional[str] = None,
) -> Dict[str, Any]:
    """POST one chat completion to the resolved model tier with multi-dialect translation, Privacy Lock, and failover."""
    eff_tier = (tier or current_tier_name() or "fast").strip().lower()
    tier_cfg = settings.resolve_tier(eff_tier)
    target_base_url, target_model = (base_url or tier_cfg.base_url).rstrip("/"), model or tier_cfg.model
    target_key = api_key if api_key is not None else tier_cfg.api_key
    target_temp = temperature if temperature is not None else tier_cfg.temperature
    target_max_tok, target_timeout = max_tokens or tier_cfg.max_tokens, http_timeout or settings.http_timeout
    target_dialect = dialect or tier_cfg.dialect
    target_effort = reasoning_effort if reasoning_effort is not None else tier_cfg.reasoning_effort
    has_fb = bool(base_url is None and tier_cfg.fallback_model)
    try:
        return _post_completion(
            messages, tools, tool_choice, target_base_url, target_model, target_key, target_temp, target_max_tok,
            target_timeout, max_retries, priority, tier_cfg.tier, dialect=target_dialect, reasoning_effort=target_effort,
            has_tier_fallback=has_fb, max_concurrency=tier_cfg.max_concurrency,
        )
    except InterruptedError:
        raise
    except RuntimeError as exc:
        if base_url is not None:
            raise
        priv = settings.effective_privacy_mode()
        fb_base = (tier_cfg.fallback_base_url or target_base_url).rstrip("/")
        if tier_cfg.fallback_model and (priv != "local_only" or is_local_or_lan_endpoint(fb_base)):
            telemetry.log("WARN", "llm", f"Tier `{tier_cfg.tier}` (`{target_model}`) failed ({exc}); failing over to tier fallback `{tier_cfg.fallback_model}`.")
            return _post_completion(
                messages, tools, tool_choice, fb_base, tier_cfg.fallback_model, tier_cfg.fallback_api_key or target_key,
                target_temp, target_max_tok, target_timeout, max_retries, priority, f"{tier_cfg.tier}:fallback",
                dialect=tier_cfg.fallback_dialect or target_dialect, reasoning_effort="",
                max_concurrency=1 if ":11434" in fb_base else tier_cfg.max_concurrency,
            )
        def_cfg = settings.resolve_tier("default")
        same_box, err_s = target_base_url.lower() == def_cfg.base_url.lower(), str(exc)
        if any(tok in err_s.lower() for tok in ("not a multimodal model", "does not support image", "image_url")):
            reas_cfg = settings.resolve_tier("reasoning")
            reas_allowed = priv != "local_only" or is_local_or_lan_endpoint(reas_cfg.base_url)
            if reas_allowed and (reas_cfg.model != target_model or reas_cfg.base_url.lower() != target_base_url.lower()):
                telemetry.log("INFO", "llm", f"Model `{target_model}` is text-only; routing multimodal turn to `reasoning` (`{reas_cfg.model}`).")
                return _post_completion(
                    messages, tools, tool_choice, reas_cfg.base_url, reas_cfg.model, reas_cfg.api_key,
                    temperature if temperature is not None else reas_cfg.temperature,
                    max_tokens or reas_cfg.max_tokens, target_timeout, max_retries, priority, "image->reasoning",
                    dialect=reas_cfg.dialect, reasoning_effort=reas_cfg.reasoning_effort, max_concurrency=reas_cfg.max_concurrency,
                )
        def_allowed = priv != "local_only" or is_local_or_lan_endpoint(def_cfg.base_url)
        can_failover = tier_cfg.tier != "default" and def_allowed and (not same_box or (target_model != def_cfg.model and ("HTTP 404" in err_s or "Privacy Lock" in err_s)))
        if not can_failover:
            raise
        telemetry.log("WARN", "llm", f"Tier `{tier_cfg.tier}` (`{target_model}`) failed ({exc}); failing over to `default` (`{def_cfg.model}`).")
        fallback_temp = temperature if temperature is not None else def_cfg.temperature
        return _post_completion(
            messages, tools, tool_choice, def_cfg.base_url, def_cfg.model, def_cfg.api_key,
            fallback_temp, max_tokens or def_cfg.max_tokens, target_timeout, max_retries, priority, "default",
            dialect=def_cfg.dialect, reasoning_effort=def_cfg.reasoning_effort, max_concurrency=def_cfg.max_concurrency,
        )
