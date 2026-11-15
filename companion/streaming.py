"""
Live Token, Reasoning & Harness Activity Event Streaming (`companion/streaming.py` — A-Frame Infrastructure Layer).

Provides a zero-dependency `ContextVar` event sink and an OpenAI-compatible Server-Sent Events (SSE)
stream assembler so CLI and LAN Web UI surfaces receive real-time token, `<think>`, phase, and tool
activity updates while preserving 100% backward compatibility with synchronous `chat_fn` signatures.
"""

import contextlib
import contextvars
import json
import re
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

StreamCallback = Callable[[Dict[str, Any]], None]

_STREAM_SINK: contextvars.ContextVar[Optional[StreamCallback]] = contextvars.ContextVar(
    "companion_stream_event_sink", default=None
)
_WIRE_DOC_CONTEXT: contextvars.ContextVar[str] = contextvars.ContextVar("companion_wire_doc_context", default="")
_WIRE_IMAGES: contextvars.ContextVar[Tuple[str, ...]] = contextvars.ContextVar("companion_wire_images", default=())


def get_active_wire_doc_context() -> str:
    """Return active per-turn extracted document attachment text for wire injection (empty when none)."""
    return _WIRE_DOC_CONTEXT.get()


def get_active_wire_images() -> Tuple[str, ...]:
    """Return active per-turn base64 `data:image/...` URIs for OpenAI multimodal wire injection."""
    return _WIRE_IMAGES.get()


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


def has_stream_sink() -> bool:
    """Return `True` when a live stream event listener is bound in the current execution context."""
    return _STREAM_SINK.get() is not None


def emit_stream_event(event: Dict[str, Any]) -> None:
    """Emit a structured live event (`text_delta`, `thinking_delta`, `phase`, `tool_start`, `tool_end`) safely."""
    sink = _STREAM_SINK.get()
    if sink is None:
        return
    try:
        sink(event)
    except Exception:
        pass


@contextlib.contextmanager
def stream_event_context(sink: Optional[StreamCallback]) -> Iterator[None]:
    """Bind a `ContextVar`-isolated live stream callback for the current turn or background job."""
    tok = _STREAM_SINK.set(sink)
    try:
        yield
    finally:
        _STREAM_SINK.reset(tok)


class _ThinkTagStreamSplitter:
    """Incremental state machine splitting `<think>...</think>` chunks from visible answer deltas."""

    def __init__(self) -> None:
        self.in_think = False
        self.in_tool_xml = False
        self._buf = ""

    def feed(self, text: str) -> None:
        if not text:
            return
        self._buf += text
        self._drain(final=False)

    def flush(self) -> None:
        self._drain(final=True)

    def _drain(self, final: bool) -> None:
        while self._buf:
            low = self._buf.lower()
            if self.in_tool_xml:
                end_tc = low.find("</tool_call>")
                if end_tc == -1:
                    if final:
                        self._buf = ""
                    return
                self._buf = self._buf[end_tc + len("</tool_call>") :]
                self.in_tool_xml = False
                continue
            if not self.in_think:
                tc_idx = low.find("<tool_call>")
                th_idx = low.find("<think>")
                candidates = [i for i in (tc_idx, th_idx) if i != -1]
                if not candidates:
                    hold = 0 if final else min(len(self._buf), 11)
                    safe_upto = len(self._buf) - hold if "<" in self._buf[-hold:] and hold else len(self._buf)
                    if safe_upto <= 0:
                        return
                    chunk, self._buf = self._buf[:safe_upto], self._buf[safe_upto:]
                    if chunk:
                        emit_stream_event({"type": "text_delta", "delta": chunk})
                    if not final:
                        return
                    continue
                first = min(candidates)
                if first > 0:
                    emit_stream_event({"type": "text_delta", "delta": self._buf[:first]})
                if first == th_idx:
                    self._buf = self._buf[first + len("<think>") :]
                    self.in_think = True
                else:
                    self._buf = self._buf[first + len("<tool_call>") :]
                    self.in_tool_xml = True
            else:
                end_idx = low.find("</think>")
                if end_idx == -1:
                    hold = 0 if final else min(len(self._buf), 8)
                    safe_upto = len(self._buf) - hold if "<" in self._buf[-hold:] and hold else len(self._buf)
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
    """Merge streamed `delta.tool_calls` fragments by `index` (or distinct `id`) into OpenAI `tool_calls` dicts."""
    for item in tc_deltas:
        if not isinstance(item, dict):
            continue
        raw_idx, tc_id = item.get("index"), str(item.get("id") or "")
        if raw_idx is not None:
            idx = int(raw_idx or 0)
        elif tc_id and (hit := next((k for k, v in acc.items() if v.get("id") == tc_id), None)) is not None:
            idx = hit
        elif tc_id and 0 in acc and acc[0].get("id") not in ("", "call_0", tc_id):
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
    headers = getattr(resp, "headers", None)
    ctype = str(headers.get("Content-Type", "") if headers is not None and hasattr(headers, "get") else "").lower()
    if "text/event-stream" not in ctype:
        raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        if has_stream_sink() and isinstance(data, dict) and data.get("choices"):
            msg = (data["choices"][0] or {}).get("message") or {}
            reas = str(msg.get("reasoning_content") or msg.get("reasoning") or "")
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
        r_piece = str(delta.get("reasoning_content") or delta.get("reasoning") or "")
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
    """Extract `<think>...</think>` blocks from text and return `(clean_content, thinking_text)`."""
    if not text:
        return "", ""
    raw = str(text)
    thoughts: List[str] = [m.strip() for m in _THINK_BLOCK_RE.findall(raw) if m.strip()]
    cleaned = _THINK_BLOCK_RE.sub("", raw)
    unclosed = _UNCLOSED_THINK_RE.search(cleaned)
    if unclosed:
        if unclosed.group(1).strip():
            thoughts.append(unclosed.group(1).strip())
        cleaned = cleaned[: unclosed.start()]
    return cleaned.strip(), "\n\n".join(thoughts)


def _parse_single_tool_block(inner: str, idx: int) -> Optional[Dict[str, Any]]:
    """Parse one `<tool_call>` body in either JSON (`{"name": ...}`) or Qwen XML (`<function=...>`) syntax."""
    body = inner.strip()
    if body.startswith("```"):
        body = re.sub(r"^```(?:json|xml)?\s*|\s*```$", "", body, flags=re.IGNORECASE).strip()
    xml_m = _XML_FUNC_RE.search(body)
    if xml_m:
        fn_name = xml_m.group(1).strip()
        args_dict: Dict[str, Any] = {}
        for p_key, p_val in _XML_PARAM_RE.findall(xml_m.group(2)):
            val_clean = p_val.strip()
            if val_clean.startswith(("{", "[")):
                try:
                    args_dict[p_key] = json.loads(val_clean)
                    continue
                except Exception:
                    pass
            args_dict[p_key] = val_clean
        return {"id": f"rescued_tc_{idx}", "type": "function", "function": {"name": fn_name, "arguments": json.dumps(args_dict)}}
    try:
        parsed = json.loads(body)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    fn_obj = parsed.get("function") if isinstance(parsed.get("function"), dict) else parsed
    name = str(fn_obj.get("name") or fn_obj.get("tool") or "").strip()
    if not name:
        return None
    raw_args = fn_obj.get("arguments") if "arguments" in fn_obj else fn_obj.get("args", {})
    arg_str = raw_args if isinstance(raw_args, str) else json.dumps(raw_args if isinstance(raw_args, dict) else {})
    return {"id": f"rescued_tc_{idx}", "type": "function", "function": {"name": name, "arguments": arg_str}}


def rescue_text_tool_calls(text: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Rescue `<tool_call>...</tool_call>` JSON/XML blocks emitted inside assistant text into OpenAI `tool_calls`."""
    if not text or "<tool_call>" not in text.lower():
        return text, []
    rescued = [tc for idx, match in enumerate(_TOOL_CALL_TAG_RE.finditer(text), 1) if (tc := _parse_single_tool_block(match.group(1), idx)) is not None]
    if not rescued:
        return text, []
    return _TOOL_CALL_TAG_RE.sub("", text).strip(), rescued
