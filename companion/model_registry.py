"""
Multi-Provider Registry, Blind Vault Credential Binding & Hot-Reload Capability Tier Matrix
(`companion/model_registry.py` — A-Frame Domain Layer).

Manages:
1. Built-in local & frontier provider presets (`ollama`, `vllm`, `gemini`, `anthropic`, `openai`, `xai`, `openrouter`, etc.)
2. SQLite `llm_providers` & `llm_tier_routes` with write-only `VaultManager` secret binding (zero raw keys in SQLite/UI)
3. Hot-reload runtime `TierConfig` & `local_only` Privacy Lock resolution hooked into `companion.config.settings`
"""

import json
import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

from companion import memory_store as _ms_mod
from companion.config import CAPABILITY_TIERS, MODEL_TIERS, TierConfig, settings
from companion.llm import discover_provider_models, format_model_tiers_status
from companion.llm_wire import VALID_DIALECTS, VALID_PRIVACY_MODES, VALID_REASONING_EFFORTS, is_local_or_lan_endpoint
from companion.memory_store import MemoryStore, get_memory_store
from companion.vault import get_vault_manager, normalize_secret_name

__all__ = [
    "PROVIDER_PRESETS",
    "bump_model_registry_cache",
    "execute_model_registry_action",
    "get_model_matrix_snapshot",
    "parse_providers_slash",
]

PROVIDER_PRESETS: Dict[str, Dict[str, Any]] = {
    "local-default": {"id": "local-default", "label": "Local Default (Primary Ollama / LAN)", "dialect": "openai_compat", "base_url": "", "is_local": True, "credential_ref": "", "default_context": 262144, "models": []},
    "ollama": {"id": "ollama", "label": "Ollama (Local / LAN)", "dialect": "openai_compat", "base_url": "http://127.0.0.1:11434", "is_local": True, "credential_ref": "", "default_context": 131072, "models": ["qwen2.5:7b", "llama3.2:latest", "qwen2.5-coder:7b", "deepseek-r1:14b"]},
    "vllm": {"id": "vllm", "label": "vLLM / SGLang Gateway", "dialect": "openai_compat", "base_url": "http://127.0.0.1:8099", "is_local": True, "credential_ref": "", "default_context": 262144, "models": ["deepseek-r1-distill-32b", "qwen2.5-coder-32b-instruct"]},
    "lmstudio": {"id": "lmstudio", "label": "LM Studio / llama.cpp", "dialect": "openai_compat", "base_url": "http://127.0.0.1:1234/v1", "is_local": True, "credential_ref": "", "default_context": 131072, "models": []},
    "gemini": {"id": "gemini", "label": "Google Gemini (Native v1beta)", "dialect": "gemini_native", "base_url": "https://generativelanguage.googleapis.com/v1beta", "is_local": False, "credential_ref": "gemini-api-key", "default_context": 1048576, "models": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-embedding-001"]},
    "anthropic": {"id": "anthropic", "label": "Anthropic Claude (Messages API)", "dialect": "anthropic_messages", "base_url": "https://api.anthropic.com", "is_local": False, "credential_ref": "anthropic-api-key", "default_context": 200000, "models": ["claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5"]},
    "openai": {"id": "openai", "label": "OpenAI (GPT & o-Series)", "dialect": "openai_compat", "base_url": "https://api.openai.com/v1", "is_local": False, "credential_ref": "openai-api-key", "default_context": 200000, "models": ["gpt-5", "gpt-5-mini", "gpt-4.1", "o4-mini", "o3", "text-embedding-3-small"]},
    "xai": {"id": "xai", "label": "xAI Grok", "dialect": "openai_compat", "base_url": "https://api.x.ai/v1", "is_local": False, "credential_ref": "xai-api-key", "default_context": 256000, "models": ["grok-4", "grok-4-fast", "grok-3", "grok-3-mini"]},
    "openrouter": {"id": "openrouter", "label": "OpenRouter (Frontier Hub)", "dialect": "openai_compat", "base_url": "https://openrouter.ai/api/v1", "is_local": False, "credential_ref": "openrouter-api-key", "default_context": 200000, "models": ["anthropic/claude-sonnet-4.6", "google/gemini-2.5-pro", "openai/gpt-5", "x-ai/grok-4", "deepseek/deepseek-r1"]},
    "deepseek": {"id": "deepseek", "label": "DeepSeek Official", "dialect": "openai_compat", "base_url": "https://api.deepseek.com/v1", "is_local": False, "credential_ref": "deepseek-api-key", "default_context": 128000, "models": ["deepseek-chat", "deepseek-reasoner"]},
    "groq": {"id": "groq", "label": "Groq Cloud LPU", "dialect": "openai_compat", "base_url": "https://api.groq.com/openai/v1", "is_local": False, "credential_ref": "groq-api-key", "default_context": 131072, "models": ["llama-3.3-70b-versatile", "qwen/qwen3-32b"]},
    "custom": {"id": "custom", "label": "Custom OpenAI-Compatible", "dialect": "openai_compat", "base_url": "http://127.0.0.1:8080/v1", "is_local": True, "credential_ref": "", "default_context": 131072, "models": []},
}

MODEL_REGISTRY_DDL: Tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS llm_providers (
        provider_id TEXT PRIMARY KEY, label TEXT NOT NULL, preset TEXT NOT NULL DEFAULT 'custom',
        dialect TEXT NOT NULL DEFAULT 'openai_compat', base_url TEXT NOT NULL,
        credential_ref TEXT NOT NULL DEFAULT '', is_local INTEGER NOT NULL DEFAULT 1,
        enabled INTEGER NOT NULL DEFAULT 1, models_json TEXT NOT NULL DEFAULT '[]',
        last_health TEXT NOT NULL DEFAULT '', updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS llm_tier_routes (
        tier TEXT PRIMARY KEY, provider_id TEXT NOT NULL, model TEXT NOT NULL,
        context_window_tokens INTEGER NOT NULL DEFAULT 262144, temperature REAL NOT NULL DEFAULT 0.2,
        max_tokens INTEGER NOT NULL DEFAULT 4096, reasoning_effort TEXT NOT NULL DEFAULT '',
        fallback_provider_id TEXT NOT NULL DEFAULT '', fallback_model TEXT NOT NULL DEFAULT '',
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""",
)

_CACHE_LOCK = threading.RLock()
_CACHE_GEN = 1
_RESOLVED_CACHE: Dict[Tuple[str, int, float], Tuple[Dict[str, TierConfig], str]] = {}


def bump_model_registry_cache() -> None:
    """Invalidate the in-memory runtime tier/Vault resolution cache immediately."""
    global _CACHE_GEN
    with _CACHE_LOCK:
        _CACHE_GEN += 1
        _RESOLVED_CACHE.clear()


def _slugify_provider_id(raw: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", (raw or "").strip().lower()).strip("-")[:48]


def _ensure_schema_and_seed(store: MemoryStore) -> None:
    with store._lock:
        for ddl in MODEL_REGISTRY_DDL:
            store.conn.execute(ddl)
        cur = store.conn.execute("SELECT COUNT(*) FROM llm_providers WHERE provider_id = 'local-default'").fetchone()
        if not cur or int(cur[0]) == 0:
            def_cfg = settings.tiers["default"]
            def_lbl = "Local Default (Ollama)" if ":11434" in def_cfg.base_url else "Local Default (Primary .env)"
            def_models = json.dumps([{"id": def_cfg.model, "label": def_cfg.model, "loaded": False, "context_window": def_cfg.context_window_tokens}]) if def_cfg.model else "[]"
            store.conn.execute(
                """INSERT OR IGNORE INTO llm_providers
                   (provider_id, label, preset, dialect, base_url, credential_ref, is_local, enabled, models_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                ("local-default", def_lbl, "local-default", def_cfg.dialect, def_cfg.base_url, "", 1 if is_local_or_lan_endpoint(def_cfg.base_url) else 0, def_models),
            )
            seen_urls = {def_cfg.base_url.rstrip("/").lower()}
            for t_name, t_cfg in settings.tiers.items():
                norm_u = t_cfg.base_url.rstrip("/").lower()
                if not norm_u or norm_u in seen_urls:
                    continue
                seen_urls.add(norm_u)
                is_oll = ":11434" in norm_u
                pid = "ollama-lan" if is_oll else f"env-{t_name}"
                lbl = f"Ollama LAN ({t_cfg.base_url})" if is_oll else f"Dedicated {t_name.title()} ({t_cfg.base_url})"
                m_json = json.dumps([{"id": t_cfg.model, "label": t_cfg.model, "loaded": False, "context_window": t_cfg.context_window_tokens}])
                store.conn.execute(
                    """INSERT OR IGNORE INTO llm_providers
                       (provider_id, label, preset, dialect, base_url, credential_ref, is_local, enabled, models_json)
                       VALUES (?, ?, ?, ?, ?, '', ?, 1, ?)""",
                    (pid, lbl, "ollama" if is_oll else "vllm", t_cfg.dialect, t_cfg.base_url, 1 if is_local_or_lan_endpoint(t_cfg.base_url) else 0, m_json),
                )
        store.conn.commit()


def _resolve_provider_secret(store: MemoryStore, provider_id: str, cred_ref: str) -> str:
    slug = normalize_secret_name(cred_ref)
    if slug:
        vault = get_vault_manager(store=store)
        ok, val = vault.resolve_for_call(slug, caller_scope=f"llm:{provider_id}")
        if ok and val:
            return val
    env_k = f"COMPANION_{provider_id.upper().replace('-', '_')}_API_KEY"
    if os.environ.get(env_k, "").strip():
        return os.environ[env_k].strip()
    return settings.api_key if provider_id == "local-default" else ""


def _list_provider_rows(store: MemoryStore) -> List[Dict[str, Any]]:
    _ensure_schema_and_seed(store)
    cols = ["provider_id", "label", "preset", "dialect", "base_url", "credential_ref", "is_local", "enabled", "models_json", "last_health", "updated_at"]
    rows = store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM llm_providers ORDER BY is_local DESC, provider_id ASC")
    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            models = json.loads(r.pop("models_json") or "[]")
        except Exception:
            models = []
        r["is_local"] = bool(r["is_local"]) and is_local_or_lan_endpoint(str(r["base_url"]))
        r["enabled"] = bool(r["enabled"])
        r["discovered_models"] = models if isinstance(models, list) else []
        preset_models = PROVIDER_PRESETS.get(str(r["preset"]), {}).get("models", [])
        seen = {str(m.get("id")) for m in r["discovered_models"] if isinstance(m, dict) and m.get("id")}
        r["suggested_models"] = [str(m.get("id")) for m in r["discovered_models"] if isinstance(m, dict) and m.get("id")] + [m for m in preset_models if m not in seen]
        out.append(r)
    return out


def _list_tier_route_rows(store: MemoryStore) -> Dict[str, Dict[str, Any]]:
    _ensure_schema_and_seed(store)
    cols = ["tier", "provider_id", "model", "context_window_tokens", "temperature", "max_tokens", "reasoning_effort", "fallback_provider_id", "fallback_model", "updated_at"]
    rows = store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM llm_tier_routes")
    return {str(r["tier"]): r for r in rows}


def _compute_resolved_map(store: MemoryStore, base_tiers: Dict[str, TierConfig]) -> Tuple[Dict[str, TierConfig], str]:
    prof = store.get_user_profile()
    priv = prof.get("llm_privacy_mode", "").strip().lower()
    eff_priv = priv if priv in VALID_PRIVACY_MODES else settings.privacy_mode
    trig_s, targ_s = prof.get("compact_trigger_pct", ""), prof.get("compact_target_pct", "")
    if trig_s and targ_s:
        try:
            settings.update_compaction_watermarks(float(trig_s), float(targ_s))
        except ValueError:
            pass
    providers = {p["provider_id"]: p for p in _list_provider_rows(store) if p["enabled"]}
    routes = _list_tier_route_rows(store)
    key_cache: Dict[str, str] = {}

    def _pkey(pid: str) -> str:
        if pid not in key_cache:
            p = providers.get(pid)
            key_cache[pid] = _resolve_provider_secret(store, pid, str(p["credential_ref"]) if p else "")
        return key_cache[pid]

    resolved: Dict[str, TierConfig] = {}
    def_base = base_tiers["default"]
    if "default" in routes and routes["default"]["provider_id"] in providers:
        dr = routes["default"]
        dp = providers[dr["provider_id"]]
        fb_p = providers.get(str(dr["fallback_provider_id"])) if dr["fallback_provider_id"] else None
        resolved["default"] = TierConfig(
            tier="default", base_url=str(dp["base_url"]).rstrip("/"), model=str(dr["model"]),
            api_key=_pkey(dp["provider_id"]), context_window_tokens=max(4096, int(dr["context_window_tokens"])),
            temperature=float(dr["temperature"]), is_dedicated=True, max_tokens=max(256, int(dr["max_tokens"])),
            provider_id=dp["provider_id"], dialect=str(dp["dialect"]), reasoning_effort=str(dr["reasoning_effort"]),
            fallback_base_url=str(fb_p["base_url"]).rstrip("/") if fb_p else "",
            fallback_model=str(dr["fallback_model"]) if fb_p else "",
            fallback_api_key=_pkey(fb_p["provider_id"]) if fb_p else "",
            fallback_dialect=str(fb_p["dialect"]) if fb_p else "openai_compat",
            fallback_provider_id=fb_p["provider_id"] if fb_p else "",
        )
    else:
        resolved["default"] = def_base

    eff_def = resolved["default"]
    for t in CAPABILITY_TIERS:
        if t == "default":
            continue
        env_t = base_tiers.get(t, def_base)
        if t in routes and routes[t]["provider_id"] in providers:
            tr = routes[t]
            tp = providers[tr["provider_id"]]
            fb_p = providers.get(str(tr["fallback_provider_id"])) if tr["fallback_provider_id"] else None
            resolved[t] = TierConfig(
                tier=t, base_url=str(tp["base_url"]).rstrip("/"), model=str(tr["model"]),
                api_key=_pkey(tp["provider_id"]), context_window_tokens=max(4096, int(tr["context_window_tokens"])),
                temperature=float(tr["temperature"]), is_dedicated=True, max_tokens=max(256, int(tr["max_tokens"])),
                provider_id=tp["provider_id"], dialect=str(tp["dialect"]), reasoning_effort=str(tr["reasoning_effort"]),
                fallback_base_url=str(fb_p["base_url"]).rstrip("/") if fb_p else "",
                fallback_model=str(tr["fallback_model"]) if fb_p else "",
                fallback_api_key=_pkey(fb_p["provider_id"]) if fb_p else "",
                fallback_dialect=str(fb_p["dialect"]) if fb_p else "openai_compat",
                fallback_provider_id=fb_p["provider_id"] if fb_p else "",
            )
        elif env_t.is_dedicated:
            resolved[t] = env_t
        else:
            resolved[t] = TierConfig(
                tier=t, base_url=eff_def.base_url, model=eff_def.model, api_key=eff_def.api_key,
                context_window_tokens=eff_def.context_window_tokens, temperature=env_t.temperature,
                is_dedicated=False, max_tokens=env_t.max_tokens, provider_id=eff_def.provider_id,
                dialect=eff_def.dialect, reasoning_effort=eff_def.reasoning_effort,
                fallback_base_url=eff_def.fallback_base_url, fallback_model=eff_def.fallback_model,
                fallback_api_key=eff_def.fallback_api_key, fallback_dialect=eff_def.fallback_dialect,
                fallback_provider_id=eff_def.fallback_provider_id,
            )
    return resolved, eff_priv


def _active_store_if_initialized() -> Optional[MemoryStore]:
    override = _ms_mod._STORE_OVERRIDE.get()
    if override is not None:
        return override
    if _ms_mod._store_instance is not None:
        return _ms_mod._store_instance
    return get_memory_store() if settings.db_path.is_file() else None


def _get_cached_state(store: MemoryStore, base_tiers: Dict[str, TierConfig]) -> Tuple[Dict[str, TierConfig], str]:
    sec_path = get_vault_manager(store=store).secrets_path
    sec_mtime = sec_path.stat().st_mtime if sec_path.is_file() else 0.0
    base_sig = tuple((k, v.base_url, v.model, v.is_dedicated) for k, v in sorted(base_tiers.items()))
    ckey = (str(store.db_path.resolve()), _CACHE_GEN, sec_mtime, base_sig)
    with _CACHE_LOCK:
        hit = _RESOLVED_CACHE.get(ckey)
    if hit is not None:
        return hit
    computed = _compute_resolved_map(store, base_tiers)
    with _CACHE_LOCK:
        _RESOLVED_CACHE[ckey] = computed
    return computed


def _runtime_resolve_tier(tier_key: str, base_tiers: Dict[str, TierConfig]) -> Optional[TierConfig]:
    st = _active_store_if_initialized()
    if st is None:
        return None
    try:
        resolved_map, _ = _get_cached_state(st, base_tiers)
        return resolved_map.get(tier_key, resolved_map.get("default"))
    except Exception:
        return None


def _runtime_resolve_privacy(default_privacy: str) -> str:
    st = _active_store_if_initialized()
    if st is None:
        return default_privacy
    try:
        _, eff_priv = _get_cached_state(st, settings.tiers)
        return eff_priv
    except Exception:
        return default_privacy


settings.register_runtime_resolver(_runtime_resolve_tier, _runtime_resolve_privacy)


def get_model_matrix_snapshot(store: Optional[MemoryStore] = None) -> Dict[str, Any]:
    """Build the secret-free Provider, Tier Matrix, Privacy & Compaction payload for Web UI & CLI."""
    mem = store or get_memory_store()
    vault = get_vault_manager(store=mem)
    v_secrets = vault.list_secrets()
    v_map = {s["name"]: bool(s["is_set"]) for s in v_secrets}
    providers = _list_provider_rows(mem)
    prov_by_id = {p["provider_id"]: p for p in providers}
    for p in providers:
        slug = normalize_secret_name(str(p.get("credential_ref") or ""))
        p["credential_ref"] = slug
        p["credential_set"] = bool(v_map.get(slug)) if slug else (bool(settings.api_key) if p["provider_id"] == "local-default" else True)
    routes = _list_tier_route_rows(mem)
    resolved_map, eff_priv = _get_cached_state(mem, settings.tiers)
    prof = mem.get_user_profile()
    active_mode = prof.get("active_model_tier", "auto").strip().lower() or "auto"
    tier_rows: List[Dict[str, Any]] = []
    for t in CAPABILITY_TIERS:
        cfg = resolved_map.get(t, settings.resolve_tier(t))
        ov = routes.get(t)
        matched_pid = ov["provider_id"] if ov else next((p["provider_id"] for p in providers if p["base_url"].rstrip("/").lower() == cfg.base_url.rstrip("/").lower()), "local-default")
        p_info = prov_by_id.get(matched_pid, {"label": matched_pid})
        tier_rows.append({
            "tier": t,
            "provider_id": matched_pid,
            "provider_label": p_info.get("label", matched_pid),
            "dialect": cfg.dialect,
            "base_url": cfg.base_url,
            "model": cfg.model,
            "context_window_tokens": cfg.context_window_tokens,
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
            "reasoning_effort": cfg.reasoning_effort,
            "fallback_provider_id": cfg.fallback_provider_id,
            "fallback_model": cfg.fallback_model,
            "is_overridden": bool(ov),
            "is_dedicated": cfg.is_dedicated,
            "privacy_blocked": eff_priv == "local_only" and not is_local_or_lan_endpoint(cfg.base_url),
        })
    md_lines = [
        f"### Providers ({len(providers)}) | Privacy Lock: `{eff_priv}` | Active Mode: `{active_mode}`",
        *[
            f"- **`{p['provider_id']}`** ({p['label']} | `{p['dialect']}` | {'LAN/Local' if p['is_local'] else 'Cloud'}): `{p['base_url']}`"
            + (f" | Vault: `{p['credential_ref']}` ({'SET' if p['credential_set'] else 'MISSING'})" if p["credential_ref"] else "")
            + (f" | {p['last_health']}" if p.get("last_health") else "")
            for p in providers
        ],
        "",
        format_model_tiers_status(active_mode=active_mode, check_health=False),
    ]
    return {
        "ok": True,
        "active_mode": active_mode,
        "privacy_mode": eff_priv,
        "compaction": {
            "trigger_pct": settings.compact_trigger_pct,
            "target_pct": settings.compact_target_pct,
            "keep_recent_messages": settings.keep_recent_messages,
        },
        "presets": list(PROVIDER_PRESETS.values()),
        "providers": providers,
        "tiers": tier_rows,
        "vault_secrets": [{"name": s["name"], "cred_type": s["cred_type"], "is_set": s["is_set"]} for s in v_secrets],
        "summary": f"Loaded {len(providers)} LLM provider(s) and {len(tier_rows)} capability tiers (mode=`{active_mode}`, privacy=`{eff_priv}`).",
        "markdown_block": "\n".join(md_lines),
    }


def _save_provider(params: Dict[str, Any], actor: str, mem: MemoryStore) -> Dict[str, Any]:
    _ensure_schema_and_seed(mem)
    preset_id = str(params.get("preset") or params.get("preset_id") or "custom").strip().lower()
    preset = PROVIDER_PRESETS.get(preset_id, PROVIDER_PRESETS["custom"])
    pid = _slugify_provider_id(str(params.get("provider_id") or params.get("id") or preset_id))
    if not pid:
        return {"ok": False, "error": "Provider `provider_id` is required."}
    dialect = str(params.get("dialect") or preset["dialect"]).strip().lower()
    if dialect not in VALID_DIALECTS:
        return {"ok": False, "error": f"Invalid dialect `{dialect}`. Valid: {', '.join(VALID_DIALECTS)}."}
    base_url = str(params.get("base_url") or preset["base_url"] or settings.base_url).strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        return {"ok": False, "error": "Provider `base_url` must start with `http://` or `https://`."}
    label = str(params.get("label") or params.get("name") or preset["label"] or pid).strip()[:80]
    is_loc = is_local_or_lan_endpoint(base_url)
    cred_ref = normalize_secret_name(str(params.get("credential_ref") if "credential_ref" in params else preset["credential_ref"]))
    raw_secret = str(params.get("api_key_value") or "").strip()
    vault = get_vault_manager(store=mem)
    if raw_secret:
        eff_slug = cred_ref or normalize_secret_name(f"{pid}-api-key")
        v_res = vault.set_secret(
            eff_slug, raw_secret, cred_type="api_key",
            description=f"API key for LLM provider {label} ({pid})",
            allow_list=[f"llm:{pid}", "llm:*"], actor=actor,
        )
        if not v_res.get("ok"):
            return v_res
        cred_ref = eff_slug
    elif cred_ref:
        vault.declare_placeholder(cred_ref, cred_type="api_key", description=f"API key for LLM provider {label}", allow_list=[f"llm:{pid}", "llm:*"])
        if actor.strip().lower() in ("user", "cli", "web"):
            vault.grant_scope(cred_ref, f"llm:{pid}", actor=actor)
    enabled = 0 if params.get("enabled") is False else 1
    with mem._lock:
        existing = mem.conn.execute("SELECT models_json, last_health FROM llm_providers WHERE provider_id = ?", (pid,)).fetchone()
        m_json = existing[0] if existing else json.dumps([{"id": m, "label": m, "loaded": False, "context_window": preset["default_context"]} for m in preset.get("models", [])])
        last_h = existing[1] if existing else ""
        mem.conn.execute(
            """INSERT INTO llm_providers (provider_id, label, preset, dialect, base_url, credential_ref, is_local, enabled, models_json, last_health, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(provider_id) DO UPDATE SET
                 label=excluded.label, preset=excluded.preset, dialect=excluded.dialect, base_url=excluded.base_url,
                 credential_ref=excluded.credential_ref, is_local=excluded.is_local, enabled=excluded.enabled, updated_at=CURRENT_TIMESTAMP""",
            (pid, label, preset_id, dialect, base_url, cred_ref, 1 if is_loc else 0, enabled, m_json, last_h),
        )
        mem.conn.commit()
    bump_model_registry_cache()
    snap = get_model_matrix_snapshot(mem)
    return {**snap, "provider_id": pid, "summary": f"Saved provider `{pid}` (`{dialect}` @ `{base_url}`{f', Vault=`{cred_ref}`' if cred_ref else ''})."}


def _discover_or_test_provider(params: Dict[str, Any], mem: MemoryStore) -> Dict[str, Any]:
    _ensure_schema_and_seed(mem)
    pid = _slugify_provider_id(str(params.get("provider_id") or "local-default"))
    prows = {p["provider_id"]: p for p in _list_provider_rows(mem)}
    if pid not in prows:
        return {"ok": False, "error": f"Unknown provider `{pid}`."}
    p = prows[pid]
    key = _resolve_provider_secret(mem, pid, str(p.get("credential_ref") or ""))
    res = discover_provider_models(str(p["base_url"]), api_key=key, dialect=str(p["dialect"]), timeout=float(params.get("timeout") or 4.0))
    with mem._lock:
        if res["ok"] and res["models"]:
            mem.conn.execute("UPDATE llm_providers SET models_json = ?, last_health = ?, updated_at = CURRENT_TIMESTAMP WHERE provider_id = ?", (json.dumps(res["models"]), str(res["status_detail"]), pid))
        else:
            mem.conn.execute("UPDATE llm_providers SET last_health = ?, updated_at = CURRENT_TIMESTAMP WHERE provider_id = ?", (str(res["status_detail"]), pid))
        mem.conn.commit()
    bump_model_registry_cache()
    snap = get_model_matrix_snapshot(mem)
    return {**snap, "probe_ok": res["ok"], "latency_ms": res["latency_ms"], "status_detail": res["status_detail"], "discovered": res["models"], "summary": f"Provider `{pid}`: {res['status_detail']}"}


def _save_tier_route(params: Dict[str, Any], mem: MemoryStore) -> Dict[str, Any]:
    _ensure_schema_and_seed(mem)
    tier = str(params.get("tier") or "").strip().lower()
    if tier not in CAPABILITY_TIERS:
        return {"ok": False, "error": f"Invalid tier `{tier}`. Valid: {', '.join(CAPABILITY_TIERS)}."}
    pid = _slugify_provider_id(str(params.get("provider_id") or "local-default"))
    prows = {p["provider_id"]: p for p in _list_provider_rows(mem)}
    if pid not in prows:
        return {"ok": False, "error": f"Provider `{pid}` is not registered."}
    model = str(params.get("model") or "").strip()
    if not model:
        return {"ok": False, "error": f"Model ID is required for tier `{tier}`."}
    cur_cfg = settings.resolve_tier(tier)
    ctx_win = max(4096, int(params.get("context_window_tokens") or cur_cfg.context_window_tokens))
    temp = max(0.0, min(2.0, float(params["temperature"]) if params.get("temperature") is not None and str(params.get("temperature")) != "" else cur_cfg.temperature))
    max_tok = max(256, int(params.get("max_tokens") or cur_cfg.max_tokens))
    effort = str(params.get("reasoning_effort") or "").strip().lower()
    if effort not in VALID_REASONING_EFFORTS:
        return {"ok": False, "error": f"Invalid reasoning_effort `{effort}`. Valid: low, medium, high, or empty."}
    fb_pid = _slugify_provider_id(str(params.get("fallback_provider_id") or ""))
    fb_model = str(params.get("fallback_model") or "").strip() if fb_pid else ""
    with mem._lock:
        mem.conn.execute(
            """INSERT INTO llm_tier_routes
               (tier, provider_id, model, context_window_tokens, temperature, max_tokens, reasoning_effort, fallback_provider_id, fallback_model, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(tier) DO UPDATE SET
                 provider_id=excluded.provider_id, model=excluded.model, context_window_tokens=excluded.context_window_tokens,
                 temperature=excluded.temperature, max_tokens=excluded.max_tokens, reasoning_effort=excluded.reasoning_effort,
                 fallback_provider_id=excluded.fallback_provider_id, fallback_model=excluded.fallback_model, updated_at=CURRENT_TIMESTAMP""",
            (tier, pid, model, ctx_win, temp, max_tok, effort, fb_pid, fb_model),
        )
        mem.conn.commit()
    bump_model_registry_cache()
    snap = get_model_matrix_snapshot(mem)
    return {**snap, "summary": f"Updated `{tier}` tier -> `{model}` via `{pid}` (ctx={ctx_win:,}, temp={temp:.2f}{f', effort={effort}' if effort else ''})."}


def execute_model_registry_action(params: Optional[Dict[str, Any]] = None, actor: str = "user", store: Optional[MemoryStore] = None) -> Dict[str, Any]:
    """Execute Shared Core Action `models` across Web UI, CLI `/providers`, and internal tools."""
    p = dict(params or {})
    mem = store or get_memory_store()
    sub = str(p.get("subaction") or p.get("action") or "status").strip().lower()
    if sub in ("save_provider", "delete_provider", "set_privacy") and actor.strip().lower() not in ("user", "cli", "web"):
        return {"ok": False, "error": f"SECURITY GUARD: `{sub}` requires direct user/CLI/Web authorization."}
    if sub in ("status", "list", "matrix"):
        return get_model_matrix_snapshot(mem)
    if sub == "check_all":
        for prov in _list_provider_rows(mem):
            _discover_or_test_provider({"provider_id": prov["provider_id"], "timeout": 2.5}, mem)
        snap = get_model_matrix_snapshot(mem)
        return {**snap, "summary": "Completed live health & model discovery across all configured providers."}
    if sub == "save_provider":
        return _save_provider(p, actor, mem)
    if sub == "delete_provider":
        pid = _slugify_provider_id(str(p.get("provider_id") or p.get("id") or ""))
        if not pid or pid == "local-default":
            return {"ok": False, "error": "Cannot delete primary `local-default` provider."}
        with mem._lock:
            mem.conn.execute("DELETE FROM llm_providers WHERE provider_id = ?", (pid,))
            mem.conn.execute("DELETE FROM llm_tier_routes WHERE provider_id = ?", (pid,))
            mem.conn.commit()
        bump_model_registry_cache()
        return {**get_model_matrix_snapshot(mem), "summary": f"Removed provider `{pid}`."}
    if sub in ("test_provider", "discover_models"):
        return _discover_or_test_provider(p, mem)
    if sub == "save_tier":
        return _save_tier_route(p, mem)
    if sub == "reset_tier":
        tier = str(p.get("tier") or "").strip().lower()
        with mem._lock:
            mem.conn.execute("DELETE FROM llm_tier_routes WHERE tier = ?", (tier,))
            mem.conn.commit()
        bump_model_registry_cache()
        return {**get_model_matrix_snapshot(mem), "summary": f"Reset `{tier}` tier to default inheritance."}
    if sub == "set_mode":
        mode = str(p.get("mode") or "auto").strip().lower()
        valid = ("auto", *MODEL_TIERS)
        if mode not in valid:
            return {"ok": False, "error": f"Invalid routing mode `{mode}`. Valid: {', '.join(valid)}."}
        mem.set_user_profile("active_model_tier", mode)
        mem.sync_memory_md()
        return {**get_model_matrix_snapshot(mem), "summary": f"Active model routing mode set to `{mode}`."}
    if sub == "set_privacy":
        priv = str(p.get("privacy_mode") or "").strip().lower()
        if priv not in VALID_PRIVACY_MODES:
            return {"ok": False, "error": f"Invalid privacy_mode `{priv}`. Valid: {', '.join(VALID_PRIVACY_MODES)}."}
        mem.set_user_profile("llm_privacy_mode", priv)
        bump_model_registry_cache()
        return {**get_model_matrix_snapshot(mem), "summary": f"LLM Privacy Lock set to `{priv}`."}
    if sub in ("set_compaction", "save_compaction"):
        trig = float(p.get("trigger_pct") or p.get("compact_trigger_pct") or settings.compact_trigger_pct)
        targ = float(p.get("target_pct") or p.get("compact_target_pct") or settings.compact_target_pct)
        settings.update_compaction_watermarks(trig, targ)
        mem.set_user_profile("compact_trigger_pct", f"{settings.compact_trigger_pct:.1f}")
        mem.set_user_profile("compact_target_pct", f"{settings.compact_target_pct:.1f}")
        bump_model_registry_cache()
        return {**get_model_matrix_snapshot(mem), "summary": f"Updated context compaction watermarks: trigger={settings.compact_trigger_pct:.1f}% -> target={settings.compact_target_pct:.1f}%."}
    return {"ok": False, "error": f"Unknown `models` subaction `{sub}`."}


def parse_providers_slash(parts: List[str]) -> Dict[str, Any]:
    """Parse `/providers` CLI arguments into `execute_model_registry_action` parameters."""
    if not parts or parts[0].lower() in ("list", "status", "matrix"):
        return {"subaction": "status"}
    sub, rest = parts[0].lower(), parts[1:]
    if sub in ("check", "ping"):
        return {"subaction": "discover_models", "provider_id": rest[0]} if rest else {"subaction": "check_all"}
    if sub == "privacy" and rest:
        return {"subaction": "set_privacy", "privacy_mode": rest[0]}
    if sub == "add" and rest:
        preset = rest[0].lower()
        return {"subaction": "save_provider", "preset": preset, "provider_id": preset, **({"base_url": rest[1]} if len(rest) > 1 else {}), **({"credential_ref": rest[2]} if len(rest) > 2 else {})}
    if sub == "tier" and len(rest) >= 3:
        return {"subaction": "save_tier", "tier": rest[0], "provider_id": rest[1], "model": rest[2], **({"reasoning_effort": rest[3]} if len(rest) > 3 else {})}
    if sub == "reset" and rest:
        return {"subaction": "reset_tier", "tier": rest[0]}
    return {"subaction": "status"}
