"""
Multi-Dialect LLM Wire Translators, LAN Privacy Guard & Model Discovery (`companion/llm_wire.py` — A-Frame Infrastructure Layer).

Provides 100% Python 3.12 stdlib protocol translation, SSE streaming normalization, local/LAN
privacy classification, and live model discovery across three wire dialects:
1. `openai_compat`      — vLLM, Ollama, LM Studio, OpenAI (`gpt-*`, `o*`), xAI Grok, OpenRouter, DeepSeek, Groq
2. `anthropic_messages` — Native Anthropic Messages API (`/v1/messages` + Extended Thinking + `signature` replay)
3. `gemini_native`      — Native Google Gemini API (`v1beta` `:generateContent` + `thoughtSignature` + multimodal `inlineData`)
"""

import ipaddress
import json
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from companion.streaming import (
    ThinkTagStreamSplitter,
    emit_stream_event,
    get_active_wire_doc_context,
    get_active_wire_images,
    has_stream_sink,
    read_completion_or_sse_stream,
)

VALID_DIALECTS = ("openai_compat", "anthropic_messages", "gemini_native")
VALID_PRIVACY_MODES = ("hybrid", "local_only", "cloud_allowed")
VALID_REASONING_EFFORTS = ("", "low", "medium", "high")
_EFFORT_BUDGETS = {"low": 1024, "medium": 4096, "high": 8192}
_LAN_HOST_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa", ".localdomain", ".ts.net")
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
_OPENAI_REASONING_RE = re.compile(r"^(?:o1|o3|o4|gpt-5)(?:$|[-_])", re.I)


def is_local_or_lan_endpoint(url: str) -> bool:
    """Return `True` strictly when `url` resolves to loopback, private LAN (RFC1918), Tailscale CGNAT, or `.local`/`.lan`."""
    raw = (url or "").strip()
    if not raw:
        return False
    host = (urllib.parse.urlsplit(raw if "://" in raw else f"http://{raw}").hostname or "").strip().lower()
    if not host:
        return False
    if host in ("localhost", "0.0.0.0") or "." not in host or host.endswith(_LAN_HOST_SUFFIXES):
        return True
    try:
        ip_obj = ipaddress.ip_address(host)
        return bool(ip_obj.is_loopback or ip_obj.is_private or (ip_obj.version == 4 and ip_obj in _CGNAT_NET))
    except ValueError:
        return False


def build_openai_resource_url(base_url: str, resource: str) -> str:
    """Normalize an OpenAI-compatible base URL without duplicating `/v1` (`/v1`, `/api/v1`, `/openai/v1`, `/v1beta/openai`)."""
    base, res = (base_url or "").strip().rstrip("/"), resource.lstrip("/")
    return f"{base}/{res}" if base.lower().endswith(("/v1", "/api/v1", "/openai/v1", "/v1beta/openai", "/v2")) else f"{base}/v1/{res}"


def _split_data_uri(uri: str) -> Tuple[str, str]:
    if not uri.startswith("data:") or "," not in uri:
        return "application/octet-stream", ""
    head, b64 = uri[5:].split(",", 1)
    return head.split(";", 1)[0].strip().lower() or "application/octet-stream", b64.strip()


def _parse_tc_args(raw_args: Any) -> Dict[str, Any]:
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str) and raw_args.strip():
        try:
            parsed = json.loads(raw_args)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except Exception:
            return {}
    return {}


def _extract_msg_text(raw_c: Any) -> str:
    if not isinstance(raw_c, list):
        return str(raw_c or "")
    return "\n".join(str(p.get("text", "")) for p in raw_c if isinstance(p, dict) and p.get("type") == "text")


def inject_openai_turn_attachments(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize leading `system` message and inject active turn document/media attachments onto the last `user` message."""
    wire: List[Dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "system" and wire:
            if len(wire) == 1 and wire[0].get("role") == "system":
                wire[0] = {**wire[0], "content": f"{wire[0].get('content', '')}\n\n{m.get('content', '')}"}
            else:
                wire.append({**m, "role": "user"})
        elif m.get("role") == "assistant":
            clean_m = {k: v for k, v in m.items() if not str(k).startswith("_")}
            if isinstance(clean_m.get("tool_calls"), list):
                clean_m["tool_calls"] = [{k: v for k, v in tc.items() if not str(k).startswith("_")} for tc in clean_m["tool_calls"]]
            wire.append(clean_m)
        else:
            wire.append(m)
    doc_ctx, media_uris = get_active_wire_doc_context(), get_active_wire_images()
    if (doc_ctx or media_uris) and any(w.get("role") == "user" for w in wire):
        u_idx = max(i for i, w in enumerate(wire) if w.get("role") == "user")
        base_txt = str(wire[u_idx].get("content") or "")
        merged_txt = f"{base_txt}\n\n{doc_ctx}".strip() if doc_ctx else base_txt
        if media_uris:
            parts: List[Dict[str, Any]] = [{"type": "text", "text": merged_txt or "Inspect the attached media file(s)."}]
            for uri in media_uris:
                mime, b64 = _split_data_uri(uri)
                if mime in ("audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp3") and b64:
                    parts.append({"type": "input_audio", "input_audio": {"data": b64, "format": "wav" if "wav" in mime else "mp3"}})
                else:
                    parts.append({"type": "image_url", "image_url": {"url": uri}})
            wire[u_idx] = {**wire[u_idx], "content": parts}
        else:
            wire[u_idx] = {**wire[u_idx], "content": merged_txt}
    return wire


def _build_openai_compat_request(
    messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]], tool_choice: str,
    base_url: str, model: str, api_key: str, temperature: float, max_tokens: int, reasoning_effort: str, stream: bool,
) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    url = build_openai_resource_url(base_url, "chat/completions")
    low_url, low_model = base_url.lower(), model.strip().lower()
    is_oai_reasoning = bool(_OPENAI_REASONING_RE.match(low_model) and "gpt-5-chat" not in low_model and "openrouter.ai" not in low_url)
    body: Dict[str, Any] = {"model": model, "messages": inject_openai_turn_attachments(messages)}
    if is_oai_reasoning:
        body["max_completion_tokens"] = max_tokens
        if abs(temperature - 1.0) < 1e-6:
            body["temperature"] = 1.0
    else:
        body["max_tokens"] = max_tokens
        if "deepseek-reasoner" not in low_model:
            body["temperature"] = temperature
    eff = reasoning_effort.strip().lower()
    if eff in ("low", "medium", "high"):
        if "openrouter.ai" in low_url:
            body["reasoning"] = {"effort": eff}
        elif is_oai_reasoning or "api.x.ai" in low_url or low_model.startswith("grok-"):
            body["reasoning_effort"] = eff
    if tools and tool_choice != "none":
        body["tools"], body["tool_choice"] = tools, tool_choice
    if stream:
        body["stream"] = True
    headers: Dict[str, str] = {"Content-Type": "application/json", "User-Agent": "companion-os/3.3"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if "openrouter.ai" in low_url:
        headers["HTTP-Referer"], headers["X-Title"] = "https://companion.local", "Companion AI OS"
    return url, headers, body


def _append_merged_turn(turns: List[Dict[str, Any]], role: str, blocks: List[Dict[str, Any]], key: str) -> None:
    if not blocks:
        return
    if turns and turns[-1]["role"] == role:
        turns[-1][key].extend(blocks)
    else:
        turns.append({"role": role, key: list(blocks)})


def _build_anthropic_request(
    messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]], tool_choice: str,
    base_url: str, model: str, api_key: str, temperature: float, max_tokens: int, reasoning_effort: str, stream: bool,
) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    base = base_url.rstrip("/")
    url = f"{base}/messages" if base.lower().endswith("/v1") else f"{base}/v1/messages"
    sys_parts, turns = [], []
    doc_ctx, media_uris = get_active_wire_doc_context(), get_active_wire_images()
    last_u = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    for idx, m in enumerate(messages):
        role, txt = str(m.get("role") or "user"), _extract_msg_text(m.get("content"))
        if role == "system":
            if txt.strip():
                sys_parts.append(txt.strip())
        elif role == "assistant":
            tcalls = list(m.get("tool_calls") or [])
            pmeta = m.get("_provider_meta") or (tcalls[0].get("_provider_meta") if tcalls and isinstance(tcalls[0], dict) else None) or {}
            blocks = [tb for tb in (pmeta.get("anthropic_thinking") or []) if isinstance(tb, dict) and tb.get("type") in ("thinking", "redacted_thinking")]
            if txt.strip():
                blocks.append({"type": "text", "text": txt})
            for tc in tcalls:
                fn = tc.get("function") or {}
                blocks.append({"type": "tool_use", "id": str(tc.get("id") or "toolu_1"), "name": str(fn.get("name") or "tool"), "input": _parse_tc_args(fn.get("arguments"))})
            _append_merged_turn(turns, "assistant", blocks, "content")
        elif role == "tool":
            _append_merged_turn(turns, "user", [{"type": "tool_result", "tool_use_id": str(m.get("tool_call_id") or "toolu_1"), "content": txt or "(ok)"}], "content")
        else:
            u_blocks: List[Dict[str, Any]] = []
            eff_txt = f"{txt}\n\n{doc_ctx}".strip() if (idx == last_u and doc_ctx) else txt
            if idx == last_u and media_uris:
                for uri in media_uris:
                    mime, b64 = _split_data_uri(uri)
                    if (mime.startswith("image/") or mime == "application/pdf") and b64:
                        btype = "image" if mime.startswith("image/") else "document"
                        u_blocks.append({"type": btype, "source": {"type": "base64", "media_type": mime, "data": b64}})
            u_blocks.append({"type": "text", "text": eff_txt or "Inspect the attached content."})
            _append_merged_turn(turns, "user", u_blocks, "content")
    if not turns or turns[0]["role"] != "user":
        turns.insert(0, {"role": "user", "content": [{"type": "text", "text": "Continue."}]})
    budget = _EFFORT_BUDGETS.get(reasoning_effort.strip().lower(), 0)
    body: Dict[str, Any] = {"model": model, "max_tokens": max(max_tokens, budget + 2048) if budget else max_tokens, "messages": turns}
    if sys_parts:
        body["system"] = "\n\n".join(sys_parts)
    if budget:
        body["thinking"] = {"type": "enabled", "budget_tokens": budget}
    else:
        body["temperature"] = min(1.0, max(0.0, temperature))
    if tools and tool_choice != "none":
        body["tools"] = [
            {"name": t["function"]["name"], "description": t["function"].get("description", ""), "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}}
            for t in tools if isinstance(t, dict) and isinstance(t.get("function"), dict)
        ]
        body["tool_choice"] = {"type": "any" if tool_choice == "required" else "auto"}
    if stream:
        body["stream"] = True
    headers = {"Content-Type": "application/json", "User-Agent": "companion-os/3.3", "anthropic-version": "2023-06-01", **({"x-api-key": api_key} if api_key else {})}
    return url, headers, body


def _sanitize_gemini_schema(node: Any) -> Any:
    if isinstance(node, list):
        return [_sanitize_gemini_schema(x) for x in node]
    if not isinstance(node, dict):
        return node
    return {k: _sanitize_gemini_schema(v) for k, v in node.items() if k not in ("additionalProperties", "$schema", "title") and v is not None}


def _build_gemini_request(
    messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]], tool_choice: str,
    base_url: str, model: str, api_key: str, temperature: float, max_tokens: int, reasoning_effort: str, stream: bool,
) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    base = base_url.rstrip("/")
    if not base.lower().endswith(("/v1beta", "/v1alpha", "/v1")):
        base = f"{base}/v1beta"
    op = "streamGenerateContent?alt=sse" if stream else "generateContent"
    url = f"{base}/models/{model.strip().removeprefix('models/')}:{op}"
    sys_parts, contents, tc_names = [], [], {}
    doc_ctx, media_uris = get_active_wire_doc_context(), get_active_wire_images()
    last_u = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    for idx, m in enumerate(messages):
        role, txt = str(m.get("role") or "user"), _extract_msg_text(m.get("content"))
        if role == "system":
            if txt.strip():
                sys_parts.append(txt.strip())
        elif role == "assistant":
            tcalls = list(m.get("tool_calls") or [])
            pmeta = m.get("_provider_meta") or (tcalls[0].get("_provider_meta") if tcalls and isinstance(tcalls[0], dict) else None) or {}
            parts = [tp for tp in (pmeta.get("gemini_thoughts") or []) if isinstance(tp, dict)]
            if txt.strip():
                parts.append({"text": txt})
            for tc in tcalls:
                fn = tc.get("function") or {}
                fname = str(fn.get("name") or "tool")
                if tc.get("id"):
                    tc_names[str(tc["id"])] = fname
                sig = tc.get("_thought_signature") or ((tc.get("extra_content") or {}).get("google") or {}).get("thought_signature")
                fc_part: Dict[str, Any] = {"functionCall": {"name": fname, "args": _parse_tc_args(fn.get("arguments"))}}
                if sig:
                    fc_part["thoughtSignature"] = str(sig)
                parts.append(fc_part)
            _append_merged_turn(contents, "model", parts, "parts")
        elif role == "tool":
            fname = tc_names.get(str(m.get("tool_call_id") or "")) or str(m.get("name") or "tool_result")
            _append_merged_turn(contents, "user", [{"functionResponse": {"name": fname, "response": {"result": txt or "(ok)"}}}], "parts")
        else:
            u_parts: List[Dict[str, Any]] = []
            eff_txt = f"{txt}\n\n{doc_ctx}".strip() if (idx == last_u and doc_ctx) else txt
            if idx == last_u and media_uris:
                for uri in media_uris:
                    mime, b64 = _split_data_uri(uri)
                    if b64:
                        u_parts.append({"inlineData": {"mimeType": mime, "data": b64}})
            u_parts.append({"text": eff_txt or "Inspect the attached media."})
            _append_merged_turn(contents, "user", u_parts, "parts")
    if not contents or contents[0]["role"] != "user":
        contents.insert(0, {"role": "user", "parts": [{"text": "Continue."}]})
    gen_cfg: Dict[str, Any] = {"temperature": temperature, "maxOutputTokens": max_tokens}
    eff = reasoning_effort.strip().lower()
    budget = _EFFORT_BUDGETS.get(eff, 0)
    if budget:
        is_gem3_plus = bool(re.search(r"gemini-[3-9]", model, re.I))
        gen_cfg["thinkingConfig"] = {"includeThoughts": True, **({"thinkingLevel": eff.upper()} if is_gem3_plus else {"thinkingBudget": budget})}
    body: Dict[str, Any] = {"contents": contents, "generationConfig": gen_cfg}
    if sys_parts:
        body["systemInstruction"] = {"parts": [{"text": "\n\n".join(sys_parts)}]}
    if tools:
        decls = [
            {"name": t["function"]["name"], "description": t["function"].get("description", ""), "parameters": _sanitize_gemini_schema(t["function"].get("parameters") or {"type": "object", "properties": {}})}
            for t in tools if isinstance(t, dict) and isinstance(t.get("function"), dict)
        ]
        if decls and tool_choice != "none":
            body["tools"] = [{"functionDeclarations": decls}]
        elif tool_choice == "none":
            body["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
    headers = {"Content-Type": "application/json", "User-Agent": "companion-os/3.3", **({"x-goog-api-key": api_key} if api_key else {})}
    return url, headers, body


def prepare_wire_request(
    dialect: str, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]], tool_choice: str,
    base_url: str, model: str, api_key: str, temperature: float, max_tokens: int, reasoning_effort: str = "", stream: bool = False,
) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    """Build `(url, headers, body)` for `openai_compat`, `anthropic_messages`, or `gemini_native`."""
    d = (dialect or "openai_compat").strip().lower()
    if d == "anthropic_messages":
        return _build_anthropic_request(messages, tools, tool_choice, base_url, model, api_key, temperature, max_tokens, reasoning_effort, stream)
    if d == "gemini_native":
        return _build_gemini_request(messages, tools, tool_choice, base_url, model, api_key, temperature, max_tokens, reasoning_effort, stream)
    return _build_openai_compat_request(messages, tools, tool_choice, base_url, model, api_key, temperature, max_tokens, reasoning_effort, stream)


def _finalize_normalized_choice(
    text_parts: List[str], thinking_parts: List[str], tool_calls: List[Dict[str, Any]],
    provider_meta: Dict[str, Any], in_tok: int, out_tok: int, finish_reason: str = "stop",
) -> Dict[str, Any]:
    msg: Dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
    if thinking_parts:
        msg["reasoning_content"] = "".join(thinking_parts)
    if provider_meta:
        msg["_provider_meta"] = provider_meta
    if tool_calls:
        if provider_meta:
            tool_calls[0]["_provider_meta"] = provider_meta
        msg["tool_calls"] = tool_calls
    return {
        "choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": in_tok, "completion_tokens": out_tok, "total_tokens": in_tok + out_tok},
    }


def _read_anthropic_json(resp: Any) -> Dict[str, Any]:
    data = json.loads(resp.read().decode("utf-8", errors="replace"))
    if isinstance(data.get("error"), dict):
        raise RuntimeError(f"Anthropic error: {data['error'].get('message') or data['error']}")
    usage = data.get("usage") or {}
    in_tok, out_tok = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
    text_parts, think_parts, think_blocks, tcalls = [], [], [], []
    for blk in data.get("content") or []:
        if not isinstance(blk, dict):
            continue
        btype = blk.get("type")
        if btype == "thinking":
            t_str = str(blk.get("thinking") or "")
            think_parts.append(t_str)
            think_blocks.append({"type": "thinking", "thinking": t_str, **({"signature": blk["signature"]} if blk.get("signature") else {})})
            if has_stream_sink() and t_str:
                emit_stream_event({"type": "thinking_delta", "delta": t_str})
        elif btype == "redacted_thinking":
            think_blocks.append({"type": "redacted_thinking", "data": str(blk.get("data") or "")})
        elif btype == "text":
            c_str = str(blk.get("text") or "")
            text_parts.append(c_str)
            if has_stream_sink() and c_str:
                emit_stream_event({"type": "text_delta", "delta": c_str})
        elif btype == "tool_use":
            tcalls.append({"id": str(blk.get("id") or f"toolu_{len(tcalls)}"), "type": "function", "function": {"name": str(blk.get("name") or ""), "arguments": json.dumps(blk.get("input") or {})}})
    return _finalize_normalized_choice(text_parts, think_parts, tcalls, {"anthropic_thinking": think_blocks} if think_blocks else {}, in_tok, out_tok, str(data.get("stop_reason") or "stop"))


def _read_anthropic_sse(resp: Any) -> Dict[str, Any]:
    blocks_by_idx: Dict[int, Dict[str, Any]] = {}
    in_tok = out_tok = 0
    stop_reason = "stop"
    for raw_line in resp:
        line = (raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, (bytes, bytearray)) else str(raw_line)).strip()
        if not line.startswith("data:"):
            continue
        payload_s = line[5:].strip()
        if not payload_s or payload_s == "[DONE]":
            continue
        try:
            ev = json.loads(payload_s)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        if etype == "error":
            raise RuntimeError(f"Anthropic stream error: {(ev.get('error') or {}).get('message') or ev}")
        if etype == "message_start":
            u = (ev.get("message") or {}).get("usage") or {}
            in_tok, out_tok = int(u.get("input_tokens") or in_tok), int(u.get("output_tokens") or out_tok)
        elif etype == "content_block_start":
            b_idx, cb = int(ev.get("index") or 0), ev.get("content_block") or {}
            blocks_by_idx[b_idx] = {"type": cb.get("type"), "id": cb.get("id", ""), "name": cb.get("name", ""), "text": str(cb.get("text") or ""), "thinking": str(cb.get("thinking") or ""), "signature": str(cb.get("signature") or ""), "data": str(cb.get("data") or ""), "json_buf": ""}
        elif etype == "content_block_delta":
            b_idx, delta = int(ev.get("index") or 0), ev.get("delta") or {}
            slot = blocks_by_idx.setdefault(b_idx, {"type": "text", "text": "", "thinking": "", "signature": "", "json_buf": ""})
            dtype = delta.get("type")
            if dtype == "text_delta" and delta.get("text"):
                slot["text"] += str(delta["text"])
                emit_stream_event({"type": "text_delta", "delta": str(delta["text"])})
            elif dtype == "thinking_delta" and delta.get("thinking"):
                slot["thinking"] += str(delta["thinking"])
                emit_stream_event({"type": "thinking_delta", "delta": str(delta["thinking"])})
            elif dtype == "signature_delta" and delta.get("signature"):
                slot["signature"] += str(delta["signature"])
            elif dtype == "input_json_delta" and delta.get("partial_json"):
                slot["json_buf"] += str(delta["partial_json"])
        elif etype == "message_delta":
            stop_reason = str((ev.get("delta") or {}).get("stop_reason") or stop_reason)
            out_tok = int((ev.get("usage") or {}).get("output_tokens") or out_tok)
    text_parts, think_parts, think_blocks, tcalls = [], [], [], []
    for k in sorted(blocks_by_idx):
        b = blocks_by_idx[k]
        if b["type"] == "thinking":
            think_parts.append(b["thinking"])
            think_blocks.append({"type": "thinking", "thinking": b["thinking"], **({"signature": b["signature"]} if b.get("signature") else {})})
        elif b["type"] == "redacted_thinking":
            think_blocks.append({"type": "redacted_thinking", "data": b.get("data", "")})
        elif b["type"] == "text" and b["text"]:
            text_parts.append(b["text"])
        elif b["type"] == "tool_use":
            tcalls.append({"id": str(b.get("id") or f"toolu_{len(tcalls)}"), "type": "function", "function": {"name": str(b.get("name") or ""), "arguments": b["json_buf"].strip() or "{}"}})
    return _finalize_normalized_choice(text_parts, think_parts, tcalls, {"anthropic_thinking": think_blocks} if think_blocks else {}, in_tok, out_tok, stop_reason)


def _ingest_gemini_chunk(
    chunk: Dict[str, Any], text_parts: List[str], think_parts: List[str], gem_thoughts: List[Dict[str, Any]],
    tcalls: List[Dict[str, Any]], splitter: ThinkTagStreamSplitter, emit_live: bool,
) -> Tuple[int, int, str]:
    if isinstance(chunk.get("error"), dict):
        raise RuntimeError(f"Gemini error: {chunk['error'].get('message') or chunk['error']}")
    u = chunk.get("usageMetadata") or {}
    in_t, out_t = int(u.get("promptTokenCount") or 0), int(u.get("candidatesTokenCount") or 0)
    c0 = (chunk.get("candidates") or [{}])[0] if isinstance((chunk.get("candidates") or [None])[0], dict) else {}
    fin = str(c0.get("finishReason") or "STOP").lower()
    for part in ((c0.get("content") or {}).get("parts") or []):
        if not isinstance(part, dict):
            continue
        sig = str(part.get("thoughtSignature") or "")
        if part.get("thought") is True:
            t_txt = str(part.get("text") or "")
            if t_txt:
                think_parts.append(t_txt)
                if emit_live:
                    emit_stream_event({"type": "thinking_delta", "delta": t_txt})
            gem_thoughts.append({"text": t_txt, "thought": True, **({"thoughtSignature": sig} if sig else {})})
        elif "functionCall" in part and isinstance(part["functionCall"], dict):
            fc = part["functionCall"]
            tc: Dict[str, Any] = {"id": f"gem_call_{len(tcalls) + 1}", "type": "function", "function": {"name": str(fc.get("name") or ""), "arguments": json.dumps(fc.get("args") or {})}}
            if sig:
                tc["_thought_signature"], tc["extra_content"] = sig, {"google": {"thought_signature": sig}}
            tcalls.append(tc)
        elif "text" in part and part["text"] is not None:
            c_txt = str(part["text"])
            text_parts.append(c_txt)
            if emit_live:
                splitter.feed(c_txt)
    return in_t, out_t, fin


def _read_gemini_response(resp: Any) -> Dict[str, Any]:
    headers = getattr(resp, "headers", None)
    ctype = str(headers.get("Content-Type", "") if headers is not None and hasattr(headers, "get") else "").lower()
    text_parts, think_parts, gem_thoughts, tcalls = [], [], [], []
    splitter = ThinkTagStreamSplitter()
    in_tok = out_tok = 0
    fin = "stop"
    if "text/event-stream" not in ctype:
        raw_obj = json.loads(resp.read().decode("utf-8", errors="replace"))
        for ch in (raw_obj if isinstance(raw_obj, list) else [raw_obj]):
            if isinstance(ch, dict):
                i_t, o_t, f_r = _ingest_gemini_chunk(ch, text_parts, think_parts, gem_thoughts, tcalls, splitter, has_stream_sink())
                in_tok, out_tok, fin = i_t or in_tok, o_t or out_tok, f_r or fin
    else:
        for raw_line in resp:
            line = (raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, (bytes, bytearray)) else str(raw_line)).strip()
            if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]"):
                try:
                    ch = json.loads(line[5:].strip())
                    if isinstance(ch, dict):
                        i_t, o_t, f_r = _ingest_gemini_chunk(ch, text_parts, think_parts, gem_thoughts, tcalls, splitter, True)
                        in_tok, out_tok, fin = i_t or in_tok, o_t or out_tok, f_r or fin
                except json.JSONDecodeError:
                    pass
    splitter.flush()
    return _finalize_normalized_choice(text_parts, think_parts, tcalls, {"gemini_thoughts": gem_thoughts} if gem_thoughts else {}, in_tok, out_tok, fin)


def read_wire_response(dialect: str, resp: Any) -> Dict[str, Any]:
    """Read and normalize HTTP completion/SSE response from `openai_compat`, `anthropic_messages`, or `gemini_native`."""
    d = (dialect or "openai_compat").strip().lower()
    if d == "anthropic_messages":
        headers = getattr(resp, "headers", None)
        ctype = str(headers.get("Content-Type", "") if headers is not None and hasattr(headers, "get") else "").lower()
        return _read_anthropic_sse(resp) if "text/event-stream" in ctype else _read_anthropic_json(resp)
    if d == "gemini_native":
        return _read_gemini_response(resp)
    return read_completion_or_sse_stream(resp)


def heal_http_400_request(dialect: str, url: str, body: Dict[str, Any], err_body: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Attempt 1-shot parameter self-healing when an endpoint rejects `stream`, `temperature`, `max_tokens`, or `thinking`."""
    d, low_err, mutated = (dialect or "openai_compat").strip().lower(), (err_body or "").lower(), dict(body)
    if d == "gemini_native":
        gcfg = dict(mutated.get("generationConfig") or {})
        if "thinking" in low_err and "thinkingConfig" in gcfg:
            gcfg.pop("thinkingConfig", None)
            mutated["generationConfig"] = gcfg
            return url, mutated
        if ":streamGenerateContent" in url:
            return url.replace(":streamGenerateContent?alt=sse", ":generateContent").replace(":streamGenerateContent", ":generateContent"), mutated
        return None
    if d == "anthropic_messages":
        if "thinking" in low_err and "thinking" in mutated:
            mutated.pop("thinking", None)
            return url, mutated
        return (url, mutated) if mutated.pop("stream", None) is not None else None
    changed = False
    if "max_completion_tokens" in low_err and "max_tokens" in mutated:
        mutated["max_completion_tokens"] = mutated.pop("max_tokens")
        changed = True
    if "temperature" in low_err and "temperature" in mutated:
        mutated.pop("temperature", None)
        changed = True
    if ("reasoning_effort" in low_err or "reasoning" in low_err) and ("reasoning_effort" in mutated or "reasoning" in mutated):
        mutated.pop("reasoning_effort", None)
        mutated.pop("reasoning", None)
        changed = True
    if not changed and mutated.get("stream"):
        mutated.pop("stream", None)
        changed = True
    return (url, mutated) if changed else None

