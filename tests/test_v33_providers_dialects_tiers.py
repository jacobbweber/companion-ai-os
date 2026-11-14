"""End-to-end verification tests for Companion 3.3: Multi-Provider Registry, Wire Dialects, Blind Vault & LAN Privacy Lock."""

import base64
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import patch

from companion.agent import handle_slash_command, run_agent_turn
from companion.attachments import ingest_attachment, prepare_turn_attachments
from companion.config import settings
from companion.core_actions import execute_core_action
from companion.intake import select_turn_step_tier
from companion.llm import chat, discover_provider_models, format_model_tiers_status, normalize_assistant_message
from companion.llm_wire import build_openai_resource_url, is_local_or_lan_endpoint, prepare_wire_request
from companion.memory_store import MemoryStore, memory_store_context
from companion.semantic_memory import fetch_optional_embedding
from companion.streaming import stream_event_context, wire_attachment_context
from companion.vault import VaultManager


def _start_mock_server(handler_cls: type) -> Tuple[HTTPServer, str, threading.Thread]:
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    host, port = server.server_address
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, f"http://{host}:{port}", t


class TestOpenAICompatAndReasoningVariants(unittest.TestCase):
    def test_smart_url_builder_and_lan_classifier(self) -> None:
        self.assertEqual(build_openai_resource_url("http://192.168.1.101:11434", "/chat/completions"), "http://192.168.1.101:11434/v1/chat/completions")
        self.assertEqual(build_openai_resource_url("https://api.openai.com/v1", "/chat/completions"), "https://api.openai.com/v1/chat/completions")
        self.assertEqual(build_openai_resource_url("https://openrouter.ai/api/v1/", "/models"), "https://openrouter.ai/api/v1/models")
        self.assertEqual(build_openai_resource_url("https://api.groq.com/openai/v1", "/models"), "https://api.groq.com/openai/v1/models")
        self.assertEqual(
            build_openai_resource_url("https://generativelanguage.googleapis.com/v1beta/openai", "/chat/completions"),
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        )
        self.assertTrue(is_local_or_lan_endpoint("http://192.168.1.100:8099"))
        self.assertTrue(is_local_or_lan_endpoint("http://127.0.0.1:11434"))
        self.assertTrue(is_local_or_lan_endpoint("http://gpu-node.local:8000"))
        self.assertFalse(is_local_or_lan_endpoint("https://api.anthropic.com"))
        self.assertFalse(is_local_or_lan_endpoint("https://generativelanguage.googleapis.com"))
        self.assertFalse(is_local_or_lan_endpoint("https://api.openai.com/v1"))

    def test_openai_o3_gpt5_xai_openrouter_and_audio_payloads(self) -> None:
        msgs = [{"role": "system", "content": "Sys"}, {"role": "user", "content": "Solve this"}]
        # OpenAI o3-mini: max_completion_tokens, omitted temperature, reasoning_effort
        _u1, _h1, b1 = prepare_wire_request(
            dialect="openai_compat", base_url="https://api.openai.com/v1", api_key="sk-test",
            model="o3-mini", messages=msgs, tools=None, tool_choice="auto",
            temperature=0.6, max_tokens=4096, reasoning_effort="high", stream=False,
        )
        self.assertEqual(b1["max_completion_tokens"], 4096)
        self.assertNotIn("max_tokens", b1)
        self.assertNotIn("temperature", b1)
        self.assertEqual(b1["reasoning_effort"], "high")

        # xAI Grok-4 & OpenRouter reasoning shapes
        _u2, _h2, b2 = prepare_wire_request(
            dialect="openai_compat", base_url="https://api.x.ai/v1", api_key="xai-k",
            model="grok-4", messages=msgs, tools=None, tool_choice="auto",
            temperature=0.4, max_tokens=2048, reasoning_effort="medium", stream=False,
        )
        self.assertEqual(b2["reasoning_effort"], "medium")

        _u3, h3, b3 = prepare_wire_request(
            dialect="openai_compat", base_url="https://openrouter.ai/api/v1", api_key="or-k",
            model="anthropic/claude-sonnet-4.6", messages=msgs, tools=None, tool_choice="auto",
            temperature=0.4, max_tokens=2048, reasoning_effort="high", stream=False,
        )
        self.assertEqual(b3["reasoning"], {"effort": "high"})
        self.assertEqual(h3.get("X-Title"), "Companion AI OS")

        # Audio attachment -> input_audio block
        wav_b64 = base64.b64encode(b"RIFF1234WAVEfmt ").decode("ascii")
        with wire_attachment_context("", (f"data:audio/wav;base64,{wav_b64}",)):
            _u4, _h4, b4 = prepare_wire_request(
                dialect="openai_compat", base_url="https://api.openai.com/v1", api_key="sk-test",
                model="gpt-4o-audio-preview", messages=msgs, tools=None, tool_choice="auto",
                temperature=0.2, max_tokens=1024, reasoning_effort="", stream=False,
            )
        user_parts = b4["messages"][-1]["content"]
        self.assertEqual(user_parts[1]["type"], "input_audio")
        self.assertEqual(user_parts[1]["input_audio"]["format"], "wav")

    def test_http_400_unsupported_parameter_self_healing(self) -> None:
        seen_bodies: List[Dict[str, Any]] = []

        class HealHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                return

            def do_POST(self) -> None:
                ln = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(ln).decode("utf-8"))
                seen_bodies.append(body)
                if "temperature" in body:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": {"message": "Unsupported parameter: 'temperature' is not supported with this model."}}).encode("utf-8"))
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "choices": [{"message": {"role": "assistant", "content": "Healed cleanly!", "reasoning_content": "Checked params."}}]
                }).encode("utf-8"))

        srv, url, _t = _start_mock_server(HealHandler)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                data_dir = Path(tmp) / "data"
                store = MemoryStore(db_path=data_dir / "heal.db")
                try:
                    with patch.object(settings, "data_dir", data_dir), memory_store_context(store):
                        execute_core_action("models", {"subaction": "save_provider", "id": "mock-oai", "name": "Mock OAI", "dialect": "openai_compat", "base_url": url}, store=store, actor="web")
                        execute_core_action("models", {"subaction": "save_tier", "tier": "fast", "provider_id": "mock-oai", "model": "custom-reasoner", "temperature": 0.4}, store=store, actor="web")
                        res = chat([{"role": "user", "content": "Ping"}], tier="fast")
                        clean_c, think_c, _tc = normalize_assistant_message(res["choices"][0]["message"])
                        self.assertIn("Healed cleanly!", clean_c)
                        self.assertEqual(think_c, "Checked params.")
                        self.assertEqual(len(seen_bodies), 2)
                        self.assertIn("temperature", seen_bodies[0])
                        self.assertNotIn("temperature", seen_bodies[1])
                finally:
                    store.close()
        finally:
            srv.shutdown()
            srv.server_close()


class TestAnthropicMessagesDialect(unittest.TestCase):
    def test_anthropic_sse_extended_thinking_and_multiturn_signature_replay(self) -> None:
        captured_requests: List[Tuple[Dict[str, str], Dict[str, Any]]] = []

        class AnthropicMockHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                return

            def do_POST(self) -> None:
                ln = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(ln).decode("utf-8"))
                hdrs = {k.lower(): v for k, v in self.headers.items()}
                captured_requests.append((hdrs, body))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if len(captured_requests) == 1:
                    events = [
                        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
                        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Inspecting workspace first."}},
                        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig-claude-opaque-001"}},
                        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_01", "name": "list_dir", "input": {}}},
                        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"path": "specs"}'}},
                        {"type": "message_stop"},
                    ]
                else:
                    events = [
                        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Anthropic verified all 4 specs."}},
                        {"type": "message_stop"},
                    ]
                for ev in events:
                    self.wfile.write(f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n".encode("utf-8"))
                self.wfile.flush()

        srv, url, _t = _start_mock_server(AnthropicMockHandler)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                data_dir = Path(tmp) / "data"
                store = MemoryStore(db_path=data_dir / "conversations.db")
                try:
                    with patch.object(settings, "data_dir", data_dir), memory_store_context(store):
                        execute_core_action(
                            "models",
                            {"subaction": "save_provider", "id": "claude-local-mock", "name": "Claude Mock", "dialect": "anthropic_messages", "base_url": url, "api_key_value": "sk-ant-mock-secret-777"},
                            store=store, actor="web",
                        )
                        execute_core_action(
                            "models",
                            {"subaction": "save_tier", "tier": "fast", "provider_id": "claude-local-mock", "model": "claude-sonnet-4-6", "max_tokens": 8192, "reasoning_effort": "medium"},
                            store=store, actor="web",
                        )
                        execute_core_action(
                            "models",
                            {"subaction": "save_tier", "tier": "reasoning", "provider_id": "claude-local-mock", "model": "claude-sonnet-4-6", "max_tokens": 8192, "reasoning_effort": "medium"},
                            store=store, actor="web",
                        )
                        streamed_events: List[Dict[str, Any]] = []
                        with stream_event_context(lambda ev: streamed_events.append(ev)):
                            ans = run_agent_turn([{"role": "user", "content": "Check the specs directory."}], max_iters=3, chat_fn=chat, store=store, verbose=False)

                        self.assertEqual(ans, "Anthropic verified all 4 specs.")
                        self.assertEqual(len(captured_requests), 2)
                        h1, b1 = captured_requests[0]
                        self.assertEqual(h1.get("x-api-key"), "sk-ant-mock-secret-777")
                        self.assertEqual(h1.get("anthropic-version"), "2023-06-01")
                        self.assertIn("system", b1)
                        self.assertEqual(b1["thinking"]["type"], "enabled")
                        self.assertNotIn("temperature", b1)
                        self.assertGreater(b1["max_tokens"], b1["thinking"]["budget_tokens"])

                        # Step 2 must replay thinking + signature block on the assistant tool_use message!
                        _h2, b2 = captured_requests[1]
                        assistant_turns = [m for m in b2["messages"] if m["role"] == "assistant"]
                        self.assertGreaterEqual(len(assistant_turns), 1)
                        blocks = assistant_turns[-1]["content"]
                        self.assertEqual(blocks[0]["type"], "thinking")
                        self.assertEqual(blocks[0]["signature"], "sig-claude-opaque-001")
                        self.assertEqual(blocks[1]["type"], "tool_use")
                        self.assertTrue(any(ev.get("type") == "thinking_delta" for ev in streamed_events))
                        self.assertTrue(any(ev.get("type") == "text_delta" for ev in streamed_events))
                finally:
                    store.close()
        finally:
            srv.shutdown()
            srv.server_close()


def _make_gemini_mock_handler(
    captured_paths: List[str], captured_headers: List[Dict[str, str]], captured_bodies: List[Dict[str, Any]],
) -> type:
    class GeminiMockHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            return

        def do_GET(self) -> None:
            captured_paths.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "models": [
                    {"name": "models/gemini-2.5-pro", "inputTokenLimit": 1048576, "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/gemini-embedding-001", "supportedGenerationMethods": ["embedContent"]},
                ]
            }).encode("utf-8"))

        def do_POST(self) -> None:
            ln = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(ln).decode("utf-8"))
            captured_paths.append(self.path)
            captured_headers.append({k.lower(): v for k, v in self.headers.items()})
            captured_bodies.append(body)
            if ":embedContent" in self.path:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"embedding": {"values": [0.25, 0.5, 0.75]}}).encode("utf-8"))
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            gen_calls = [p for p in captured_paths if "generatecontent" in p.lower()]
            if len(gen_calls) == 1:
                chunk = {
                    "candidates": [{
                        "content": {
                            "role": "model",
                            "parts": [
                                {"text": "Gemini 3 thinking trace.", "thought": True},
                                {"functionCall": {"name": "list_dir", "args": {"path": "specs"}}, "thoughtSignature": "gem3-thought-sig-999"},
                            ],
                        }
                    }]
                }
            else:
                chunk = {"candidates": [{"content": {"role": "model", "parts": [{"text": "Gemini completed tool loop cleanly."}]}}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
            self.wfile.flush()

    return GeminiMockHandler


class TestGeminiNativeDialectAndMultimodal(unittest.TestCase):
    def test_gemini_native_sse_thought_signature_discovery_and_embeddings(self) -> None:
        captured_paths: List[str] = []
        captured_headers: List[Dict[str, str]] = []
        captured_bodies: List[Dict[str, Any]] = []
        srv, url, _t = _start_mock_server(_make_gemini_mock_handler(captured_paths, captured_headers, captured_bodies))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                data_dir = Path(tmp) / "data"
                store = MemoryStore(db_path=data_dir / "conversations.db")
                try:
                    with patch.object(settings, "data_dir", data_dir), memory_store_context(store):
                        execute_core_action(
                            "models",
                            {"subaction": "save_provider", "id": "gemini-mock", "name": "Gemini Mock", "dialect": "gemini_native", "base_url": url, "api_key_value": "AIzaSyMockKey12345"},
                            store=store, actor="web",
                        )
                        for t_slot, m_id in (("fast", "gemini-3-flash-preview"), ("reasoning", "gemini-3.1-pro-preview"), ("video", "gemini-2.5-pro"), ("embedding", "gemini-embedding-001")):
                            execute_core_action(
                                "models",
                                {"subaction": "save_tier", "tier": t_slot, "provider_id": "gemini-mock", "model": m_id, "reasoning_effort": "high"},
                                store=store, actor="web",
                            )

                        disc = discover_provider_models(url, api_key="AIzaSyMockKey12345", dialect="gemini_native")
                        self.assertTrue(disc["ok"])
                        m_by_id = {m["id"]: m for m in disc["models"]}
                        self.assertIn("gemini-2.5-pro", m_by_id)
                        self.assertEqual(m_by_id["gemini-2.5-pro"]["context_window"], 1048576)

                        # Multimodal video attachment ingestion + automatic `video` tier selection
                        vid_b64 = base64.b64encode(b"\x00\x00\x00\x18ftypmp42mockvideo").decode("ascii")
                        vid_res = ingest_attachment("demo.mp4", vid_b64, "video/mp4", store=store)
                        self.assertTrue(vid_res["ok"])
                        prepared = prepare_turn_attachments("Inspect specs and video.", [vid_res["attachment"]["id"]], store=store)
                        self.assertEqual(len(prepared.wire_image_uris), 1)

                        with wire_attachment_context(prepared.wire_doc_context, prepared.wire_image_uris), stream_event_context(lambda _ev: None):
                            picked_tier = select_turn_step_tier("auto", None, 0)
                            self.assertEqual(picked_tier, "video")
                            ans = run_agent_turn([{"role": "user", "content": "Inspect specs and video."}], max_iters=3, chat_fn=chat, store=store, verbose=False)
                        self.assertEqual(ans, "Gemini completed tool loop cleanly.")

                        # Verify key never leaked in URL, schema sanitized, inlineData sent, Gemini 2.5 vs 3 thinkingConfig, and thoughtSignature replayed!
                        for p in captured_paths:
                            self.assertNotIn("AIzaSyMockKey12345", p)
                        self.assertEqual(captured_headers[0].get("x-goog-api-key"), "AIzaSyMockKey12345")
                        first_body = captured_bodies[0]
                        self.assertIn("systemInstruction", first_body)
                        self.assertEqual(first_body["generationConfig"]["thinkingConfig"]["thinkingBudget"], 8192)
                        _gu, _gh, g3_body = prepare_wire_request(
                            "gemini_native", [{"role": "user", "content": "Hi"}], None, "auto",
                            url, "gemini-3.1-pro-preview", "AIzaSyMockKey12345", 0.6, 4096, "high", False,
                        )
                        self.assertEqual(g3_body["generationConfig"]["thinkingConfig"]["thinkingLevel"], "HIGH")
                        raw_tools_json = json.dumps(first_body.get("tools", []))
                        self.assertNotIn("additionalProperties", raw_tools_json)
                        first_user_parts = first_body["contents"][0]["parts"]
                        self.assertTrue(any("inlineData" in pt and pt["inlineData"]["mimeType"] == "video/mp4" for pt in first_user_parts))

                        second_body = captured_bodies[1]
                        model_turns = [c for c in second_body["contents"] if c.get("role") == "model"]
                        fc_parts = [pt for pt in model_turns[-1]["parts"] if "functionCall" in pt]
                        self.assertEqual(fc_parts[0]["thoughtSignature"], "gem3-thought-sig-999")

                        vec = fetch_optional_embedding("semantic recall query")
                        self.assertIsNotNone(vec)
                        self.assertEqual(len(vec or b""), 12)
                finally:
                    store.close()
        finally:
            srv.shutdown()
            srv.server_close()


class TestModelRegistryBlindVaultAndPrivacyLock(unittest.TestCase):
    def test_blind_vault_zero_reveal_actor_guards_and_local_only_privacy_lock(self) -> None:
        class LocalFallbackHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                return

            def do_POST(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"choices": [{"message": {"role": "assistant", "content": "Served by local LAN fallback."}}]}).encode("utf-8"))

        srv, local_url, _t = _start_mock_server(LocalFallbackHandler)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                data_dir = Path(tmp) / "data"
                store = MemoryStore(db_path=data_dir / "conversations.db")
                orig_trig, orig_targ = settings.compact_trigger_pct, settings.compact_target_pct
                try:
                    raw_secret = "sk-ant-ultra-secret-do-not-reveal-123456"
                    with patch.object(settings, "data_dir", data_dir), memory_store_context(store):
                        # 1. Seal frontier provider with Blind Vault secret (also tests `action` alias)
                        saved = execute_core_action(
                            "models",
                            {"action": "save_provider", "preset_id": "anthropic", "api_key_value": raw_secret},
                            store=store, actor="web",
                        )
                        self.assertTrue(saved["ok"])
                        self.assertNotIn(raw_secret, json.dumps(saved))

                        status_dump = json.dumps(execute_core_action("models", {"subaction": "status"}, store=store, actor="web"))
                        self.assertNotIn(raw_secret, status_dump)
                        self.assertNotIn(raw_secret, format_model_tiers_status())
                        db_rows = store.conn.execute("SELECT * FROM llm_providers").fetchall()
                        self.assertNotIn(raw_secret, json.dumps([list(r) for r in db_rows]))

                        # 2. Actor guard blocks prompt-injected companion mutations
                        blocked_priv = execute_core_action("models", {"subaction": "set_privacy", "privacy_mode": "cloud_allowed"}, store=store, actor="companion")
                        self.assertFalse(blocked_priv["ok"])
                        blocked_prov = execute_core_action("models", {"subaction": "save_provider", "preset": "openai"}, store=store, actor="companion")
                        self.assertFalse(blocked_prov["ok"])

                        # 3. Point default to local loopback mock, and reasoning to cloud Anthropic
                        execute_core_action("models", {"subaction": "save_provider", "id": "local-lan", "name": "Local LAN", "dialect": "openai_compat", "base_url": local_url}, store=store, actor="web")
                        execute_core_action("models", {"subaction": "save_tier", "tier": "default", "provider_id": "local-lan", "model": "local-qwen"}, store=store, actor="web")
                        execute_core_action("models", {"subaction": "save_tier", "tier": "reasoning", "provider_id": "anthropic", "model": "claude-opus-4-6"}, store=store, actor="web")
                        self.assertEqual(settings.resolve_tier("reasoning").dialect, "anthropic_messages")
                        self.assertEqual(settings.resolve_tier("reasoning").api_key, raw_secret)

                        # 4. Activate Mechanical `local_only` Outbound Privacy Lock
                        priv_res = execute_core_action("models", {"subaction": "set_privacy", "privacy_mode": "local_only"}, store=store, actor="user")
                        self.assertTrue(priv_res["ok"])
                        self.assertEqual(settings.effective_privacy_mode(), "local_only")

                        # Cloud discovery is mechanically blocked without touching the network
                        cloud_disc = discover_provider_models("https://api.anthropic.com", api_key=raw_secret, dialect="anthropic_messages")
                        self.assertFalse(cloud_disc["ok"])
                        self.assertIn("Privacy Lock", cloud_disc["status_detail"])

                        # Cloud reasoning tier is mechanically blocked and fails over cleanly to local LAN default!
                        out = chat([{"role": "user", "content": "Test local privacy failover"}], tier="reasoning")
                        self.assertEqual(out["choices"][0]["message"]["content"], "Served by local LAN fallback.")

                        # Compaction watermarks + CLI `/providers` slash parity
                        comp_res = execute_core_action("models", {"subaction": "set_compaction", "trigger_pct": 30, "target_pct": 12}, store=store, actor="web")
                        self.assertTrue(comp_res["ok"])
                        self.assertEqual(settings.compact_trigger_pct, 30)
                        slash_txt = handle_slash_command("/providers", [], store=store)
                        self.assertIn("Privacy Lock: `local_only`", str(slash_txt))
                        self.assertNotIn(raw_secret, str(slash_txt))

                        # Secret-free backup restore check: deleting secrets.env surfaces missing LLM provider secret
                        vm = VaultManager(store=store)
                        vm.secrets_path.unlink(missing_ok=True)
                        missing_refs = {m["name"] for m in vm.list_missing_secrets()}
                        self.assertIn("anthropic-api-key", missing_refs)
                finally:
                    settings.update_compaction_watermarks(orig_trig, orig_targ)
                    store.close()
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
