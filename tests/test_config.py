"""Unit tests for configuration, .env parsing, and A-Frame architecture specs."""

import os
import tempfile
import unittest
from pathlib import Path

from companion.config import MODEL_TIERS, Settings, parse_env_file
from configs import get_architecture_frame, verify_architecture


class TestConfigAndArchitecture(unittest.TestCase):
    def test_parse_env_file_handles_comments_quotes_and_exports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "# Comment line\n"
                "COMPANION_BASE_URL=http://127.0.0.1:9000\n"
                'COMPANION_MODEL="test-model-v1"\n'
                "export COMPANION_MAX_ITERS=15 # inline comment\n",
                encoding="utf-8",
            )
            parsed = parse_env_file(env_file)
            self.assertEqual(parsed["COMPANION_BASE_URL"], "http://127.0.0.1:9000")
            self.assertEqual(parsed["COMPANION_MODEL"], "test-model-v1")
            self.assertEqual(parsed["COMPANION_MAX_ITERS"], "15")

    def test_settings_validation(self) -> None:
        cfg = Settings(reload_env=False)
        issues = cfg.validate()
        self.assertEqual(issues, [])

    def test_architecture_frame_does_not_duplicate_on_repeated_calls(self) -> None:
        frame1 = get_architecture_frame()
        frame2 = get_architecture_frame()
        count1 = sum(len(v) for v in frame1.layers.values())
        count2 = sum(len(v) for v in frame2.layers.values())
        self.assertEqual(count1, count2)
        self.assertGreaterEqual(count1, 8)

    def test_verify_architecture_passes(self) -> None:
        from companion import __version__
        from companion.safety import lint_single_file_edit

        res = verify_architecture()
        self.assertTrue(res["valid"], f"Architecture verification failed: {res}")
        self.assertEqual(res["version"], __version__)
        with tempfile.TemporaryDirectory() as tmp:
            gov = Path(tmp)
            (gov / "companion").mkdir()
            err, _ = lint_single_file_edit(
                gov / "companion" / "config.py",
                '"""Bad package-level upward import."""\nfrom companion import agent\nprint(agent)\n',
                governed_root=gov,
            )
            self.assertIn("ARCHITECTURE GUARD REJECTED", str(err))
            self.assertIn("companion.agent", str(err))

    def test_multi_tier_and_multimodal_endpoint_resolution(self) -> None:
        self.assertEqual(MODEL_TIERS, ("default", "fast", "coding", "reasoning", "audio", "image", "video"))
        env_keys = (
            "COMPANION_CODING_BASE_URL",
            "COMPANION_CODING_MODEL",
            "COMPANION_CODING_TEMP",
            "COMPANION_CODING_CONTEXT_WINDOW",
            "COMPANION_REASONING_BASE_URL",
            "COMPANION_REASONING_MODEL",
            "COMPANION_REASONING_CONTEXT_WINDOW",
            "COMPANION_AUDIO_MODEL",
        )
        saved = {k: os.environ.get(k) for k in env_keys}
        try:
            for k in env_keys:
                os.environ.pop(k, None)
            cfg_default = Settings(reload_env=False)
            for tier in MODEL_TIERS:
                tc = cfg_default.resolve_tier(tier)
                self.assertEqual(tc.base_url, cfg_default.base_url)
                self.assertEqual(tc.model, cfg_default.model)
                self.assertEqual(tc.context_window_tokens, cfg_default.context_window_tokens)
                self.assertEqual(tc.is_dedicated, tier == "default")

            os.environ["COMPANION_CODING_BASE_URL"] = "http://192.168.1.101:11434/"
            os.environ["COMPANION_CODING_MODEL"] = "qwen2.5-coder:7b"
            os.environ["COMPANION_REASONING_BASE_URL"] = "http://192.168.1.50:11434/"
            os.environ["COMPANION_REASONING_MODEL"] = "deepseek-r1:14b"
            os.environ["COMPANION_REASONING_CONTEXT_WINDOW"] = "131072"
            os.environ["COMPANION_AUDIO_MODEL"] = "whisper-large-v3"
            cfg_multi = Settings(reload_env=False)

            c_tier = cfg_multi.resolve_tier("coding")
            self.assertTrue(c_tier.is_dedicated)
            self.assertEqual(c_tier.base_url, "http://192.168.1.101:11434")
            self.assertEqual(c_tier.model, "qwen2.5-coder:7b")
            self.assertEqual(c_tier.temperature, 0.15)

            r_tier = cfg_multi.resolve_tier("reasoning")
            self.assertTrue(r_tier.is_dedicated)
            self.assertEqual(r_tier.base_url, "http://192.168.1.50:11434")
            self.assertEqual(r_tier.model, "deepseek-r1:14b")
            self.assertEqual(r_tier.context_window_tokens, 131072)

            a_tier = cfg_multi.resolve_tier("audio")
            self.assertTrue(a_tier.is_dedicated)
            self.assertEqual(a_tier.base_url, cfg_multi.base_url)
            self.assertEqual(a_tier.model, "whisper-large-v3")

            f_tier = cfg_multi.resolve_tier("fast")
            self.assertFalse(f_tier.is_dedicated)
            self.assertEqual(f_tier.model, cfg_multi.model)
        finally:
            for k, val in saved.items():
                if val is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = val

    def test_wire_message_sanitization_and_external_zero_file_gate(self) -> None:
        """Verify trailing/consecutive `system` messages are normalized for vLLM Jinja and empty external builds fail gate."""
        from unittest.mock import patch

        from companion.autonomy import _evaluate_hard_verification
        from companion.config import workspace_context
        from companion.llm import _sanitize_wire_messages, chat

        raw = [
            {"role": "system", "content": "sys1"},
            {"role": "system", "content": "sys2"},
            {"role": "user", "content": "hello"},
            {"role": "system", "content": "late instruction"},
        ]
        wire = _sanitize_wire_messages(raw)
        self.assertEqual([m["role"] for m in wire], ["system", "user"])
        self.assertIn("sys2", wire[0]["content"])
        self.assertIn("late instruction", wire[1]["content"])

        with patch.dict(os.environ, {"COMPANION_BASE_URL": "http://192.168.1.100:8099", "COMPANION_REASONING_BASE_URL": "http://192.168.1.100:8099", "COMPANION_REASONING_MODEL": "deepseek-r1-distill-32b"}):
            same_box_cfg = Settings(reload_env=False)
            with patch("companion.llm.settings", same_box_cfg), patch("companion.llm._post_completion", side_effect=RuntimeError("HTTP 400 Bad Request: System message")) as m_post:
                with self.assertRaises(RuntimeError):
                    chat([{"role": "user", "content": "hi"}], tier="reasoning")
                self.assertEqual(m_post.call_count, 1)

        with tempfile.TemporaryDirectory() as tmp:
            ext_root = Path(tmp) / "empty_ext"
            ext_root.mkdir()
            with workspace_context(ext_root, "empty-ext"):
                ok, rep = _evaluate_hard_verification(ext_root, {}, run_doctor_tests=True, goal="Build app.py and unit tests")
                self.assertFalse(ok)
                self.assertIn("Zero source files", rep)

    def test_single_endpoint_swap_awareness_mutex_and_sticky_batching(self) -> None:
        """Verify conservative swap_timeout, ModelGate serialization, anti-thrash failover, and sticky batching."""
        import threading
        import time
        from unittest.mock import patch

        from companion.agent import _select_step_tier
        from companion.llm import ModelGate, _resolve_swap_window, chat
        from companion.memory_store import MemoryStore

        keys = (
            "COMPANION_BASE_URL", "COMPANION_MODEL", "COMPANION_REASONING_BASE_URL",
            "COMPANION_REASONING_MODEL", "COMPANION_HTTP_TIMEOUT", "COMPANION_SWAP_TIMEOUT",
        )
        saved = {k: os.environ.get(k) for k in keys}
        try:
            os.environ["COMPANION_BASE_URL"] = "http://192.168.1.100:8099"
            os.environ["COMPANION_MODEL"] = "qwen2.5-coder:7b"
            os.environ["COMPANION_REASONING_BASE_URL"] = "http://192.168.1.100:8099"
            os.environ["COMPANION_REASONING_MODEL"] = "deepseek-r1-distill-32b"
            os.environ.pop("COMPANION_HTTP_TIMEOUT", None)
            os.environ.pop("COMPANION_SWAP_TIMEOUT", None)
            cfg_swap = Settings(reload_env=False)
            self.assertEqual(cfg_swap.http_timeout, 900)
            self.assertEqual(cfg_swap.swap_timeout, 1800)
            self.assertTrue(cfg_swap.tiers_share_endpoint("fast", "reasoning"))

            gate = ModelGate()
            active_overlap = 0
            max_overlap = 0
            lock = threading.Lock()

            def _worker() -> None:
                nonlocal active_overlap, max_overlap
                with gate.acquire_call():
                    with lock:
                        active_overlap += 1
                        max_overlap = max(max_overlap, active_overlap)
                    time.sleep(0.03)
                    with lock:
                        active_overlap -= 1

            t1, t2 = threading.Thread(target=_worker), threading.Thread(target=_worker)
            t1.start()
            t2.start()
            t1.join(timeout=2.0)
            t2.join(timeout=2.0)
            self.assertEqual(max_overlap, 1)

            with patch("companion.llm.settings", cfg_swap), patch("companion.llm.probe_gateway_loaded_model", return_value=("qwen2.5-coder:7b", True)):
                to1, sw1 = _resolve_swap_window(gate, "http://192.168.1.100:8099", "qwen2.5-coder:7b", "", 900)
                self.assertEqual((to1, sw1), (900, False))
                to2, sw2 = _resolve_swap_window(gate, "http://192.168.1.100:8099", "deepseek-r1-distill-32b", "", 900)
                self.assertEqual((to2, sw2), (1800, True))
                self.assertTrue(bool(gate.swap_status()["swapping"]))
                gate.end_swap("deepseek-r1-distill-32b")
                self.assertIsNone(gate.swap_status())

            with patch("companion.llm.settings", cfg_swap), patch("companion.llm._post_completion", side_effect=RuntimeError("could not reach: timed out")) as m_post:
                with self.assertRaises(RuntimeError):
                    chat([{"role": "user", "content": "hi"}], tier="reasoning")
                self.assertEqual(m_post.call_count, 1)

            with tempfile.TemporaryDirectory() as tmp:
                store = MemoryStore(db_path=Path(tmp) / "swap_tier.db")
                try:
                    with patch("companion.intake.settings", cfg_swap):
                        self.assertEqual(_select_step_tier("auto", store, consecutive_errors=3, step=2, intent_mode="GUIDED_CHANGE"), "reasoning")
                        self.assertEqual(_select_step_tier("auto", store, consecutive_errors=0, step=0, intent_mode="DEEP_CONSULT"), "reasoning")
                        self.assertEqual(_select_step_tier("auto", store, consecutive_errors=0, step=1, intent_mode="DEEP_CONSULT", prior_tier="reasoning"), "reasoning")
                finally:
                    store.close()
        finally:
            for k, val in saved.items():
                if val is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = val

    def test_ollama_defaults_data_dir_isolation_and_autofallback(self) -> None:
        from unittest.mock import patch
        from companion.llm import _resolve_ollama_fallback_or_hint

        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "custom_data"
            with patch.dict(os.environ, {"COMPANION_DATA_DIR": str(data_root)}, clear=True):
                fresh = Settings(reload_env=False)
                self.assertEqual(fresh.base_url, "http://127.0.0.1:11434")
                self.assertEqual(fresh.model, "qwen2.5:7b")
                self.assertEqual(fresh.db_path, data_root.resolve() / "conversations.db")
                self.assertEqual(fresh.memory_md_path, data_root.resolve() / "MEMORY.md")
                self.assertEqual(fresh.identity_md_path, data_root.resolve() / "IDENTITY.md")
                self.assertEqual(fresh.secrets_env_path, data_root.resolve() / "secrets.env")
                self.assertEqual(fresh.user_skills_dir, data_root.resolve() / "skills")

        with patch("companion.llm.discover_provider_models", return_value={"ok": True, "models": [{"id": "nomic-embed-text"}, {"id": "llama3.2:latest"}]}):
            fb, hint = _resolve_ollama_fallback_or_hint("http://127.0.0.1:11434", "qwen2.5:7b", "")
            self.assertEqual(fb, "llama3.2:latest")
            self.assertEqual(hint, "")


if __name__ == "__main__":
    unittest.main()
