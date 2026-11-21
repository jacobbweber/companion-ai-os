"""
Live Token, Reasoning, Harness Event Streaming & `<100ms` Cooperative `CancelScope`
(`companion/streaming.py` — A-Frame Infrastructure Layer).

Provides:
1. `ContextVar`-isolated event sink (`emit_stream_event`), multimodal wire attachment scopes,
   and transient `<turn_context>` wire scope (preserving 100% static `messages[0]` KV-cache prefix).
2. `CancelScope` aborting blocked HTTP sockets (`shutdown(SHUT_RDWR)`) and `Popen` subprocesses in `<100ms`.
3. Hardened `<think>` / prefix-orphan `</think>` stream splitter, JSON-array `<tool_call>` rescue,
   and distinct-`id` parallel `tool_calls` delta assembly.
"""

import contextlib
import contextvars
import json
import re
import socket
import threading
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

StreamCallback = Callable[[Dict[str, Any]], None]

_STREAM_SINK: contextvars.ContextVar[Optional[StreamCallback]] = contextvars.ContextVar(
    "companion_stream_event_sink", default=None
)
_WIRE_DOC_CONTEXT: contextvars.ContextVar[str] = contextvars.ContextVar("companion_wire_doc_context", default="")
_WIRE_IMAGES: contextvars.ContextVar[Tuple[str, ...]] = contextvars.ContextVar("companion_wire_images", default=())
_WIRE_TURN_CONTEXT: contextvars.ContextVar[str] = contextvars.ContextVar("companion_wire_turn_context", default="")


class CancelScope:
    """Socket- & subprocess-aware cancellation scope waking blocked C-level reads/waits in `<100ms`."""

    def __init__(self, cancel_event: Optional[threading.Event] = None) -> None:
        self.cancel_event = cancel_event or threading.Event()
        self._lock = threading.Lock()
        self._sockets: List[Any] = []
        self._responses: List[Any] = []
        self._procs: List[Any] = []
        self._children: List["CancelScope"] = []

    @property
    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def check(self) -> None:
        if self.cancel_event.is_set():
            raise InterruptedError("Operation cancelled by user.")

    @staticmethod
    def _extract_raw_socket(resp_or_sock: Any) -> Any:
        if hasattr(resp_or_sock, "shutdown"):
            return resp_or_sock
        fp = getattr(resp_or_sock, "fp", None)
        raw = getattr(fp, "raw", fp) if fp is not None else None
        return getattr(raw, "_sock", None) or getattr(resp_or_sock, "_sock", None) or getattr(resp_or_sock, "sock", None)

    @staticmethod
    def _abort_socket_obj(sock_obj: Any) -> None:
        if sock_obj is None:
            return
        with contextlib.suppress(Exception):
            sock_obj.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(Exception):
            if hasattr(sock_obj, "_real_close"):
                sock_obj._real_close()
        with contextlib.suppress(Exception):
            sock_obj.close()

    @staticmethod
    def _terminate_proc(proc: Any) -> None:
        if proc is None:
            return
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            proc.terminate()

    def register_socket(self, sock_obj: Any) -> None:
        if sock_obj is None:
            return
        with self._lock:
            if self.cancel_event.is_set():
                self._abort_socket_obj(sock_obj)
                raise InterruptedError("Operation cancelled by user.")
            if sock_obj not in self._sockets:
                self._sockets.append(sock_obj)

    def unregister_socket(self, sock_obj: Any) -> None:
        with self._lock:
            self._sockets = [s for s in self._sockets if s is not sock_obj]

    @contextlib.contextmanager
    def track_response(self, resp: Any) -> Iterator[Any]:
        raw_sock = self._extract_raw_socket(resp)
        with self._lock:
            if self.cancel_event.is_set():
                self._abort_socket_obj(raw_sock)
                with contextlib.suppress(Exception):
                    resp.close()
                raise InterruptedError("Operation cancelled by user.")
            self._responses.append((raw_sock, resp))
        try:
            yield resp
        finally:
            with self._lock:
                self._responses = [pair for pair in self._responses if pair[1] is not resp]
                if raw_sock is not None:
                    self._sockets = [s for s in self._sockets if s is not raw_sock]

    @contextlib.contextmanager
    def track_popen(self, proc: Any) -> Iterator[Any]:
        with self._lock:
            if self.cancel_event.is_set():
                self._terminate_proc(proc)
                raise InterruptedError("Operation cancelled by user.")
            self._procs.append(proc)
        try:
            yield proc
        finally:
            with self._lock:
                self._procs = [p for p in self._procs if p is not proc]

    def spawn_child(self) -> "CancelScope":
        """Create and register a child `CancelScope` that aborts immediately when this parent scope cancels."""
        child = CancelScope()
        with self._lock:
            if self.cancel_event.is_set():
                child.cancel()
            else:
                self._children.append(child)
        return child

    def unregister_child(self, child: "CancelScope") -> None:
        with self._lock:
            self._children = [c for c in self._children if c is not child]

    def cancel(self) -> None:
        with self._lock:
            self.cancel_event.set()
            socks = list(self._sockets)
            resps = list(self._responses)
            procs = list(self._procs)
            kids = list(self._children)
        for s in socks:
            self._abort_socket_obj(s)
        for rs, _resp in resps:
            self._abort_socket_obj(rs)
        for p in procs:
            self._terminate_proc(p)
        for k in kids:
            k.cancel()


_CANCEL_SCOPE: contextvars.ContextVar[Optional[CancelScope]] = contextvars.ContextVar(
    "companion_cancel_scope", default=None
)


@contextlib.contextmanager
def cancel_scope_context(scope: Optional[CancelScope]) -> Iterator[Optional[CancelScope]]:
    """Bind a `CancelScope` for the active foreground turn or background job."""
    tok = _CANCEL_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _CANCEL_SCOPE.reset(tok)


def get_active_cancel_scope() -> Optional[CancelScope]:
    """Return the active `CancelScope`, if any."""
    return _CANCEL_SCOPE.get()


def check_cancelled() -> None:
    """Raise `InterruptedError` immediately if the active `CancelScope` has been cancelled."""
    scope = _CANCEL_SCOPE.get()
    if scope is not None and scope.is_cancelled:
        raise InterruptedError("Operation cancelled by user.")


def get_active_wire_doc_context() -> str:
    """Return active per-turn extracted document attachment text for wire injection (empty when none)."""
    return _WIRE_DOC_CONTEXT.get()


def get_active_wire_images() -> Tuple[str, ...]:
    """Return active per-turn base64 `data:image/...` URIs for OpenAI multimodal wire injection."""
    return _WIRE_IMAGES.get()


def get_active_wire_turn_context() -> str:
    """Return transient `<turn_context>` block injected onto the current user turn without mutating `messages[0]`."""
    return _WIRE_TURN_CONTEXT.get()


@contextlib.contextmanager
def turn_context_scope(turn_context: str) -> Iterator[None]:
    """Bind dynamic per-turn HUD/skill/approval context without mutating the cached `messages[0]` prefix."""
    tok = _WIRE_TURN_CONTEXT.set((turn_context or "").strip())
    try:
        yield
    finally:
        _WIRE_TURN_CONTEXT.reset(tok)


@contextlib.contextmanager
def wire_attachment_context(doc_context: str = "", image_data_uris: Iterable[str] = ()) -> Iterator[None]:
    """Bind extracted document text and/or multimodal `data:image/...` URIs strictly for the active turn."""
    tok_doc = _WIRE_DOC_CONTEXT.set((doc_context or "").strip())
    tok_img = _WIRE_IMAGES.set(tuple(u for u in image_data_uris if u))
    try:
        yield
    finally:
        _WIRE_IMAGES.reset(tok_img)
        _WIRE_DOC_CONTEXT.reset(tok_doc)


@contextlib.contextmanager
def clean_subagent_wire_context() -> Iterator[None]:
    """Clear `_WIRE_IMAGES`, `_WIRE_DOC_CONTEXT`, `_WIRE_TURN_CONTEXT`, and `_STREAM_SINK` for isolated subagents."""
    t_img = _WIRE_IMAGES.set(())
    t_doc = _WIRE_DOC_CONTEXT.set("")
    t_trn = _WIRE_TURN_CONTEXT.set("")
    t_snk = _STREAM_SINK.set(None)
    try:
        yield
    finally:
        _STREAM_SINK.reset(t_snk)
        _WIRE_TURN_CONTEXT.reset(t_trn)
        _WIRE_DOC_CONTEXT.reset(t_doc)
        _WIRE_IMAGES.reset(t_img)


def has_stream_sink() -> bool:
    """Return `True` when a live stream event listener is bound in the current execution context."""
    return _STREAM_SINK.get() is not None


def get_active_stream_sink() -> Optional[StreamCallback]:
    """Return the active live stream callback, if any."""
    return _STREAM_SINK.get()


def emit_stream_event(event: Dict[str, Any]) -> None:
    """Emit a structured live event (`text_delta`, `thinking_delta`, `phase`, `tool_start`, `tool_end`) safely."""
    sink = _STREAM_SINK.get()
    if sink is None:
        return
    with contextlib.suppress(Exception):
        sink(event)


@contextlib.contextmanager
def stream_event_context(sink: Optional[StreamCallback]) -> Iterator[None]:
    """Bind a `ContextVar`-isolated live stream callback for the current turn or background job."""
    tok = _STREAM_SINK.set(sink)
    try:
        yield
    finally:
        _STREAM_SINK.reset(tok)


class _ThinkTagStreamSplitter:
    """Incremental state machine splitting `<think>...</think>`, `<tool_call>`, and `<function=...>` from visible deltas."""

    def __init__(self) -> None:
        self.in_think = False
        self._tool_close_tag = ""
        self.saw_open_think = False
        self._emitted_pre_text: List[str] = []
        self._buf = ""

    def feed(self, text: str) -> None:
        if not text:
            return
        self._buf += text
        self._drain(final=False)

    def flush(self) -> None:
        self._drain(final=True)

    def _emit_visible(self, chunk: str) -> None:
        if not chunk:
            return
        if not self.saw_open_think:
            self._emitted_pre_text.append(chunk)
        emit_stream_event({"type": "text_delta", "delta": chunk})

    def _drain(self, final: bool) -> None:
        while self._buf:
            low = self._buf.lower()
            if self._tool_close_tag:
                end_tc = low.find(self._tool_close_tag)
                if end_tc == -1:
                    if final:
                        self._buf = ""
                    return
                self._buf = self._buf[end_tc + len(self._tool_close_tag) :]
                self._tool_close_tag = ""
                continue
            if not self.in_think:
                orphan_end = low.find("</think>")
                th_idx = low.find("<think>")
                if orphan_end != -1 and (th_idx == -1 or orphan_end < th_idx):
                    pre_thought = "".join(self._emitted_pre_text) + self._buf[:orphan_end]
                    self._emitted_pre_text.clear()
                    self.saw_open_think = True
                    emit_stream_event({"type": "orphan_think_closed", "thinking": pre_thought})
                    self._buf = self._buf[orphan_end + len("</think>") :]
                    continue
                tc_idx, fn_idx = low.find("<tool_call>"), low.find("<function=")
                orphan_tc = low.find("</tool_call>")
                if orphan_tc != -1 and (tc_idx == -1 or orphan_tc < tc_idx) and (th_idx == -1 or orphan_tc < th_idx):
                    if orphan_tc > 0:
                        self._emit_visible(self._buf[:orphan_tc])
                    self._buf = self._buf[orphan_tc + len("</tool_call>") :]
                    continue
                candidates = [i for i in (tc_idx, fn_idx, th_idx) if i != -1]
                if not candidates:
                    hold = 0 if final else min(len(self._buf), 14)
                    safe_upto = len(self._buf) - hold if ("<" in self._buf[-hold:] and hold) else len(self._buf)
                    if safe_upto <= 0:
                        return
                    chunk, self._buf = self._buf[:safe_upto], self._buf[safe_upto:]
                    self._emit_visible(chunk)
                    if not final:
                        return
                    continue
                first = min(candidates)
                if first > 0:
                    self._emit_visible(self._buf[:first])
                if first == th_idx:
                    self._buf = self._buf[first + len("<think>") :]
                    self.in_think = True
                    self.saw_open_think = True
                    self._emitted_pre_text.clear()
                elif first == tc_idx:
                    self._buf = self._buf[first + len("<tool_call>") :]
                    self._tool_close_tag = "</tool_call>"
                else:
                    self._buf = self._buf[first + len("<function=") :]
                    self._tool_close_tag = "</function>"
            else:
                end_idx = low.find("</think>")
                if end_idx == -1:
                    tc_in, fn_in = low.find("<tool_call>"), low.find("<function=")
                    t_cands = [i for i in (tc_in, fn_in) if i != -1]
                    if t_cands:
                        t_first = min(t_cands)
                        if t_first > 0:
                            emit_stream_event({"type": "thinking_delta", "delta": self._buf[:t_first]})
                        is_tc = t_first == tc_in
                        self._buf = self._buf[t_first + len("<tool_call>" if is_tc else "<function=") :]
                        self.in_think = False
                        self._tool_close_tag = "</tool_call>" if is_tc else "</function>"
                        continue
                    hold = 0 if final else min(len(self._buf), 12)
                    safe_upto = len(self._buf) - hold if ("<" in self._buf[-hold:] and hold) else len(self._buf)
                    if safe_upto <= 0:
                        return
                    chunk, self._buf = self._buf[:safe_upto], self._buf[safe_upto:]
                    if chunk:
                        emit_stream_event({"type": "thinking_delta", "delta": chunk})
                    if not final:
                        return
                    continue
                if end_idx > 0:
                    emit_stream_event({"type": "thinking_delta", "delta": self._buf[:end_idx]})
                self._buf = self._buf[end_idx + len("</think>") :]
                self.in_think = False


def _merge_tool_call_deltas(acc: Dict[int, Dict[str, Any]], tc_deltas: List[Dict[str, Any]]) -> None:
    """Merge streamed `delta.tool_calls` fragments; allocate a new slot whenever `tc['id']` differs from `acc[slot]['id']`."""
    for item in tc_deltas:
        if not isinstance(item, dict):
            continue
        raw_idx, tc_id = item.get("index"), str(item.get("id") or "")
        matched_slot = next((k for k, v in acc.items() if tc_id and v.get("id") == tc_id), None)
        if matched_slot is not None:
            idx = matched_slot
        elif raw_idx is not None:
            cand = int(raw_idx or 0)
            if tc_id and cand in acc and acc[cand].get("id") not in ("", f"call_{cand}", tc_id):
                idx = max(acc) + 1
            else:
                idx = cand
        elif tc_id and acc and acc[max(acc)].get("id") not in ("", f"call_{max(acc)}", tc_id):
            idx = max(acc) + 1
        else:
            idx = max(acc) if acc else 0
        entry = acc.setdefault(idx, {"id": f"call_{idx}", "type": "function", "function": {"name": "", "arguments": ""}})
        if tc_id:
            entry["id"] = tc_id
        if isinstance(item.get("extra_content"), dict):
            entry["extra_content"] = item["extra_content"]
        fn = item.get("function")
        if isinstance(fn, dict):
            if fn.get("name"):
                entry["function"]["name"] += str(fn["name"])
            if "arguments" in fn and fn["arguments"] is not None:
                arg_val = fn["arguments"]
                entry["function"]["arguments"] += json.dumps(arg_val) if isinstance(arg_val, (dict, list)) else str(arg_val)


ThinkTagStreamSplitter = _ThinkTagStreamSplitter


def read_completion_or_sse_stream(resp: Any) -> Dict[str, Any]:
    """Read either a standard JSON response or an OpenAI `text/event-stream` SSE response into a unified dict."""
    check_cancelled()
    headers = getattr(resp, "headers", None)
    ctype = str(headers.get("Content-Type", "") if headers is not None and hasattr(headers, "get") else "").lower()
    if "text/event-stream" not in ctype:
        raw = resp.read().decode("utf-8", errors="replace")
        check_cancelled()
        data = json.loads(raw)
        if has_stream_sink() and isinstance(data, dict) and data.get("choices"):
            msg = (data["choices"][0] or {}).get("message") or {}
            reas = str(msg.get("reasoning_content") or msg.get("reasoning") or msg.get("thinking") or "")
            if reas:
                emit_stream_event({"type": "thinking_delta", "delta": reas})
            splitter = _ThinkTagStreamSplitter()
            splitter.feed(str(msg.get("content") or ""))
            splitter.flush()
        return data

    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    tc_acc: Dict[int, Dict[str, Any]] = {}
    usage: Dict[str, Any] = {}
    finish_reason = "stop"
    splitter = _ThinkTagStreamSplitter()

    for raw_line in resp:
        check_cancelled()
        line = (raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, (bytes, bytearray)) else str(raw_line)).strip()
        if not line or line.startswith(":") or not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if data_str == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue
        if chunk.get("error"):
            err_obj = chunk["error"]
            err_msg = err_obj.get("message") if isinstance(err_obj, dict) else str(err_obj)
            raise RuntimeError(f"SSE stream error: {err_msg or err_obj}")
        if isinstance(chunk.get("usage"), dict) and chunk["usage"]:
            usage = chunk["usage"]
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        c0 = choices[0] if isinstance(choices[0], dict) else {}
        if c0.get("finish_reason"):
            finish_reason = str(c0["finish_reason"])
        delta = c0.get("delta") if isinstance(c0.get("delta"), dict) else {}
        r_piece = str(delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thinking") or "")
        if r_piece:
            reasoning_parts.append(r_piece)
            emit_stream_event({"type": "thinking_delta", "delta": r_piece})
        c_piece = str(delta.get("content") or "")
        if c_piece:
            content_parts.append(c_piece)
            splitter.feed(c_piece)
        if isinstance(delta.get("tool_calls"), list):
            _merge_tool_call_deltas(tc_acc, delta["tool_calls"])

    splitter.flush()
    msg_out: Dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
    if reasoning_parts:
        msg_out["reasoning_content"] = "".join(reasoning_parts)
    if tc_acc:
        msg_out["tool_calls"] = [tc_acc[k] for k in sorted(tc_acc)]
    return {"choices": [{"index": 0, "message": msg_out, "finish_reason": finish_reason}], "usage": usage}


_THINK_BLOCK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)
_UNCLOSED_THINK_RE = re.compile(r"<think>(.*)$", re.DOTALL | re.IGNORECASE)
_TOOL_CALL_TAG_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE)
_XML_FUNC_RE = re.compile(r"<function\s*=\s*([a-zA-Z0-9_.-]+)\s*>(.*?)</function>", re.DOTALL | re.IGNORECASE)
_XML_PARAM_RE = re.compile(r"<parameter\s*=\s*([a-zA-Z0-9_.-]+)\s*>(.*?)</parameter>", re.DOTALL | re.IGNORECASE)


def strip_thinking_tags(text: str) -> Tuple[str, str]:
    """Extract `<think>...</think>`, prefix-orphan `</think>`, and unclosed `<think>` into `(clean_content, thinking_text)`."""
    if not text:
        return "", ""
    raw = str(text)
    thoughts: List[str] = []
    low = raw.lower()
    close_idx, open_idx = low.find("</think>"), low.find("<think>")
    if close_idx != -1 and (open_idx == -1 or close_idx < open_idx):
        prefix_thought = raw[:close_idx].strip()
        if prefix_thought:
            thoughts.append(prefix_thought)
        raw = raw[close_idx + len("</think>") :]
    thoughts.extend(m.strip() for m in _THINK_BLOCK_RE.findall(raw) if m.strip())
    cleaned = _THINK_BLOCK_RE.sub("", raw)
    unclosed = _UNCLOSED_THINK_RE.search(cleaned)
    if unclosed:
        if unclosed.group(1).strip():
            thoughts.append(unclosed.group(1).strip())
        cleaned = cleaned[: unclosed.start()]
    return cleaned.strip(), "\n\n".join(thoughts)


def _normalize_parsed_tc_obj(parsed: Dict[str, Any], idx: int) -> Optional[Dict[str, Any]]:
    fn_obj = parsed.get("function") if isinstance(parsed.get("function"), dict) else parsed
    name = str(fn_obj.get("name") or fn_obj.get("tool") or "").strip()
    if not name:
        return None
    raw_args = fn_obj.get("arguments") if "arguments" in fn_obj else fn_obj.get("args", {})
    arg_str = raw_args if isinstance(raw_args, str) else json.dumps(raw_args if isinstance(raw_args, dict) else {})
    return {"id": f"rescued_tc_{idx}", "type": "function", "function": {"name": name, "arguments": arg_str}}


_PRESERVE_INDENT_PARAMS = frozenset({"old_str", "new_str", "old_string", "new_string", "old_text", "new_text", "content", "code", "body"})


def _parse_xml_func_matches(xml_matches: List[re.Match[str]], base_idx: int) -> List[Dict[str, Any]]:
    out_xml: List[Dict[str, Any]] = []
    for offset, xml_m in enumerate(xml_matches):
        fn_name = xml_m.group(1).strip()
        args_dict: Dict[str, Any] = {}
        for p_key, p_val in _XML_PARAM_RE.findall(xml_m.group(2)):
            key_c = p_key.strip()
            if key_c in _PRESERVE_INDENT_PARAMS:
                args_dict[key_c] = re.sub(r"^\r?\n|\r?\n$", "", p_val)
                continue
            val_clean = p_val.strip()
            if val_clean.startswith(("{", "[")):
                with contextlib.suppress(Exception):
                    args_dict[key_c] = json.loads(val_clean)
                    continue
            if val_clean.lower() in ("true", "false"):
                args_dict[key_c] = val_clean.lower() == "true"
                continue
            if val_clean.lower() == "null":
                continue
            args_dict[key_c] = val_clean
        out_xml.append({"id": f"rescued_tc_{base_idx + offset}", "type": "function", "function": {"name": fn_name, "arguments": json.dumps(args_dict)}})
    return out_xml


def _parse_tool_block_items(inner: str, base_idx: int) -> List[Dict[str, Any]]:
    """Parse one `<tool_call>` body in JSON object, JSON array (`[...]`), or Qwen XML (`<function=...>`) syntax."""
    body = inner.strip()
    if body.startswith("```"):
        body = re.sub(r"^```(?:json|xml)?\s*|\s*```$", "", body, flags=re.IGNORECASE).strip()
    xml_matches = list(_XML_FUNC_RE.finditer(body))
    if xml_matches:
        return _parse_xml_func_matches(xml_matches, base_idx)
    try:
        parsed = json.loads(body)
    except Exception:
        return []
    if isinstance(parsed, list):
        return [tc for off, item in enumerate(parsed) if isinstance(item, dict) and (tc := _normalize_parsed_tc_obj(item, base_idx + off)) is not None]
    if isinstance(parsed, dict):
        single = _normalize_parsed_tc_obj(parsed, base_idx)
        return [single] if single is not None else []
    return []


def rescue_text_tool_calls(text: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Rescue `<tool_call>...</tool_call>` or bare `<function=...>...</function>` blocks into OpenAI `tool_calls`."""
    if not text:
        return text, []
    low = text.lower()
    if "<tool_call>" not in low and "<function=" not in low:
        return text, []
    rescued: List[Dict[str, Any]] = []
    remaining = text
    if "<tool_call>" in low:
        for match in _TOOL_CALL_TAG_RE.finditer(remaining):
            rescued.extend(_parse_tool_block_items(match.group(1), len(rescued) + 1))
        if rescued:
            remaining = _TOOL_CALL_TAG_RE.sub("", remaining).strip()
    if "<function=" in remaining.lower():
        bare_xml = list(_XML_FUNC_RE.finditer(remaining))
        if bare_xml:
            rescued.extend(_parse_xml_func_matches(bare_xml, len(rescued) + 1))
            remaining = re.sub(r"</?tool_call>", "", _XML_FUNC_RE.sub("", remaining), flags=re.IGNORECASE).strip()
    return (remaining, rescued) if rescued else (text, [])
