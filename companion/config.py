"""
Configuration, Tier Routing & Mechanical WorkspaceContext (A-Frame Infrastructure Layer).

Provides a zero-dependency .env parser, per-tier model & temperature configuration,
and the mechanical WorkspaceContext path lock (`ADR-007`).
"""

import contextlib
import contextvars
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterator, Optional


DEFAULT_ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {
    ".git", "__pycache__", ".companion_backups", "data", "companion-data", "venv", ".venv", "node_modules",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", "dist", "build", "coverage",
}
MODEL_TIERS = ("default", "fast", "coding", "reasoning", "audio", "image", "video")
CAPABILITY_TIERS = ("default", "fast", "coding", "reasoning", "image", "audio", "video", "embedding")
SELF_PROJECT_ID = "companion-self"

__all__ = [
    "CAPABILITY_TIERS", "DEFAULT_ROOT", "MODEL_TIERS", "SELF_PROJECT_ID", "SKIP_DIRS", "Settings", "TierConfig",
    "WorkspaceContext", "agent_tool_turn_context", "execution_origin_context", "get_execution_origin",
    "is_in_agent_tool_turn", "self_dev_override_context", "settings", "slugify_project_id", "workspace_context",
]

_DEFAULT_TIER_TEMPS: Dict[str, float] = {"fast": 0.15, "coding": 0.15, "reasoning": 0.6}
_DEFAULT_TIER_MAX_TOKENS: Dict[str, int] = {"reasoning": 4096}


@dataclass(frozen=True)
class TierConfig:
    """Resolved endpoint, model, dialect, Vault/env key, context window, and fallback route for a logical tier."""

    tier: str
    base_url: str
    model: str
    api_key: str
    context_window_tokens: int
    temperature: float = 0.2
    is_dedicated: bool = False
    max_tokens: int = 4096
    provider_id: str = "local-default"
    dialect: str = "openai_compat"
    reasoning_effort: str = ""
    fallback_base_url: str = ""
    fallback_model: str = ""
    fallback_api_key: str = ""
    fallback_dialect: str = "openai_compat"
    fallback_provider_id: str = ""


@dataclass(frozen=True)
class WorkspaceContext:
    """Mechanical project path lock (`ADR-007`) binding tools and safety guards to the active project."""

    project_id: str
    project_root: Path
    self_root: Path
    is_self_project: bool

    @property
    def backup_dir(self) -> Path:
        base = settings.effective_data_dir if "settings" in globals() else (self.self_root / "data")
        return base / "checkpoints" / self.project_id


_WORKSPACE_OVERRIDE: contextvars.ContextVar[Optional[WorkspaceContext]] = contextvars.ContextVar("companion_workspace_override", default=None)
_TIER_OVERRIDE_MAP: contextvars.ContextVar[Optional[Dict[str, TierConfig]]] = contextvars.ContextVar("companion_tier_override_map", default=None)
_SELF_DEV_OVERRIDE: contextvars.ContextVar[Optional[bool]] = contextvars.ContextVar("companion_self_dev_override", default=None)
_IN_AGENT_TOOL_TURN: contextvars.ContextVar[bool] = contextvars.ContextVar("companion_in_agent_tool_turn", default=False)
_EXECUTION_ORIGIN: contextvars.ContextVar[str] = contextvars.ContextVar("companion_execution_origin", default="direct")


@contextlib.contextmanager
def execution_origin_context(origin: str) -> Iterator[None]:
    """Bind the active execution origin (`'foreground_turn'`, `'background_job'`, `'idle_routine'`, `'subagent'`)."""
    tok = _EXECUTION_ORIGIN.set((origin or "direct").strip().lower())
    try:
        yield
    finally:
        _EXECUTION_ORIGIN.reset(tok)


def get_execution_origin() -> str:
    """Return active execution origin (`'foreground_turn'`, `'background_job'`, `'idle_routine'`, `'subagent'`, `'direct'`)."""
    return _EXECUTION_ORIGIN.get()


@contextlib.contextmanager
def agent_tool_turn_context() -> Iterator[None]:
    """Mark execution inside an LLM tool dispatch so agent calls can never self-escalate human-only privileges."""
    tok = _IN_AGENT_TOOL_TURN.set(True)
    try:
        yield
    finally:
        _IN_AGENT_TOOL_TURN.reset(tok)


def is_in_agent_tool_turn() -> bool:
    """Return True when code is currently executing inside an LLM tool call (`tools.dispatch`)."""
    return bool(_IN_AGENT_TOOL_TURN.get())


@contextlib.contextmanager
def self_dev_override_context(enabled: bool) -> Iterator[None]:
    """Temporarily override `is_self_dev_enabled()` inside a `ContextVar` scope for deterministic testing."""
    tok = _SELF_DEV_OVERRIDE.set(bool(enabled))
    try:
        yield
    finally:
        _SELF_DEV_OVERRIDE.reset(tok)


def slugify_project_id(raw: str) -> str:
    """Normalize a project name or folder stem into a clean kebab-case project_id."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", (raw or "").strip().lower()).strip("-")
    return cleaned[:48] or "external-project"


def parse_env_file(env_path: Path) -> Dict[str, str]:
    """Parse a .env file using pure Python stdlib without external dependencies."""
    values: Dict[str, str] = {}
    if not env_path.is_file():
        return values

    try:
        text = env_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return values

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if not key:
            continue
        if val and val[0] in ('"', "'"):
            q = val[0]
            out_chars: list[str] = []
            idx = 1
            while idx < len(val):
                ch = val[idx]
                if ch == "\\" and idx + 1 < len(val) and q == '"':
                    nxt = val[idx + 1]
                    out_chars.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}.get(nxt, nxt))
                    idx += 2
                    continue
                if ch == q:
                    break
                out_chars.append(ch)
                idx += 1
            val = "".join(out_chars) if idx < len(val) else val[1:]
        elif " #" in val:
            val = val.split(" #", 1)[0].strip()
        values[key] = val
    return values


def load_dotenv(env_path: Optional[Path] = None, override: bool = False) -> Dict[str, str]:
    """Load variables from .env into os.environ (stdlib replacement for python-dotenv)."""
    if env_path is None:
        env_path = DEFAULT_ROOT / ".env"
    parsed = parse_env_file(Path(env_path))
    for k, v in parsed.items():
        if override or k not in os.environ:
            os.environ[k] = v
    return parsed


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    """Centralized, validated configuration settings and active WorkspaceContext for Companion AI OS."""

    base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:7b"
    api_key: str = ""
    root: Path = DEFAULT_ROOT
    data_dir: Optional[Path] = None
    max_iters: int = 25
    temperature: float = 0.2
    max_tokens: int = 4096
    shell_timeout: int = 60
    http_timeout: int = 900
    swap_timeout: int = 1800
    out_limit: int = 8000
    search_limit: int = 200
    context_window_tokens: int = 262144
    compact_trigger_pct: float = 75.0
    compact_target_pct: float = 35.0
    max_context_chars: int = 786432
    keep_recent_messages: int = 16
    max_backups_per_file: int = 10
    idle_minutes: int = 120
    hobby_cooldown_minutes: int = 30
    hobby_max_iters: int = 20
    max_module_lines: int = 700
    max_function_lines: int = 100
    auto_git_commit: bool = True
    stream_tokens: bool = True
    embedding_base_url: str = ""
    embedding_model: str = ""
    privacy_mode: str = "local_only"
    web_host: str = "127.0.0.1"
    web_token: str = ""
    self_dev_default: bool = False
    immutable_core: bool = False
    self_dev_locked: bool = False
    companion_name: str = ""
    companion_soul: str = ""
    user_name: str = ""
    user_profile: str = ""
    tiers: Dict[str, TierConfig] = field(default_factory=dict)

    def __init__(self, env_path: Optional[Path] = None, reload_env: bool = True):
        pre_env_keys = set(os.environ.keys())
        if reload_env and env_path is not None:
            load_dotenv(env_path=env_path, override=True)
        elif reload_env:
            load_dotenv(env_path=None, override=False)
        root_str = os.environ.get("COMPANION_ROOT", "").strip()
        self.root = Path(root_str).resolve() if root_str else DEFAULT_ROOT
        if reload_env and env_path is None and (self.root / ".env").is_file():
            for k, v in parse_env_file(self.root / ".env").items():
                if k not in pre_env_keys:
                    os.environ[k] = v
        data_str = os.environ.get("COMPANION_DATA_DIR", "").strip()
        self.data_dir = (Path(data_str) if Path(data_str).is_absolute() else (self.root / data_str)).resolve() if data_str else None
        eff_env = (self.data_dir or (self.root / "data")) / ".env"
        if reload_env and env_path is None and eff_env.is_file():
            for k, v in parse_env_file(eff_env).items():
                if k not in pre_env_keys:
                    os.environ[k] = v

        self.base_url = os.environ.get("COMPANION_BASE_URL", "http://127.0.0.1:11434").strip()
        self.model = os.environ.get("COMPANION_MODEL", "qwen2.5:7b").strip()
        self.api_key = os.environ.get("COMPANION_API_KEY", "").strip()
        self.max_iters = max(3, _env_int("COMPANION_MAX_ITERS", 25))
        self.temperature = max(0.0, min(2.0, _env_float("COMPANION_TEMP", 0.2)))
        self.max_tokens = max(256, _env_int("COMPANION_MAX_TOKENS", 4096))
        self.shell_timeout = max(5, _env_int("COMPANION_SHELL_TIMEOUT", 60))
        self.http_timeout = max(10, _env_int("COMPANION_HTTP_TIMEOUT", 900))
        self.swap_timeout = max(60, _env_int("COMPANION_SWAP_TIMEOUT", 1800))
        self.out_limit = max(1000, _env_int("COMPANION_OUT_LIMIT", 8000))
        self.search_limit = max(10, _env_int("COMPANION_SEARCH_LIMIT", 200))
        self.context_window_tokens = max(4096, _env_int("COMPANION_CONTEXT_WINDOW", 262144))
        _raw_trig = _env_float("COMPANION_COMPACT_TRIGGER_PCT", 75.0)
        self.compact_trigger_pct = 75.0 if abs(_raw_trig - 25.0) < 1e-6 else max(5.0, min(90.0, _raw_trig))
        _raw_targ = _env_float("COMPANION_COMPACT_TARGET_PCT", 35.0)
        eff_targ = 35.0 if abs(_raw_targ - 10.0) < 1e-6 and abs(_raw_trig - 25.0) < 1e-6 else _raw_targ
        self.compact_target_pct = max(3.0, min(self.compact_trigger_pct * 0.85, eff_targ))
        default_chars = int(self.context_window_tokens * (self.compact_trigger_pct / 100.0) * 4)
        self.max_context_chars = max(8000, _env_int("COMPANION_MAX_CONTEXT_CHARS", default_chars))
        self.keep_recent_messages = max(4, _env_int("COMPANION_KEEP_RECENT_MESSAGES", 16))
        self.max_backups_per_file = max(3, _env_int("COMPANION_MAX_BACKUPS", 10))
        self.idle_minutes = max(1, _env_int("COMPANION_IDLE_MINUTES", 120))
        self.hobby_cooldown_minutes = max(1, _env_int("COMPANION_HOBBY_COOLDOWN_MINUTES", 30))
        self.hobby_max_iters = max(3, _env_int("COMPANION_HOBBY_MAX_ITERS", 20))
        _raw_mod = _env_int("COMPANION_MAX_MODULE_LINES", 700)
        self.max_module_lines = 700 if _raw_mod == 550 else max(200, _raw_mod)
        self.max_function_lines = max(40, _env_int("COMPANION_MAX_FUNCTION_LINES", 100))
        self.auto_git_commit = _env_bool("COMPANION_AUTO_GIT_COMMIT", True)
        self.stream_tokens = _env_bool("COMPANION_STREAM_TOKENS", True)
        self.embedding_base_url = os.environ.get("COMPANION_EMBEDDING_BASE_URL", "").strip().rstrip("/")
        self.embedding_model = os.environ.get("COMPANION_EMBEDDING_MODEL", "").strip()
        raw_priv = os.environ.get("COMPANION_PRIVACY_MODE", "local_only").strip().lower()
        self.privacy_mode = raw_priv if raw_priv in ("hybrid", "local_only", "cloud_allowed") else "local_only"
        self.web_host = os.environ.get("COMPANION_WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
        self.web_token = os.environ.get("COMPANION_WEB_TOKEN", "").strip()
        self.self_dev_default = _env_bool("COMPANION_SELF_DEV", False)
        self.immutable_core = _env_bool("COMPANION_IMMUTABLE_CORE", False)
        self.self_dev_locked = self.immutable_core or _env_bool("COMPANION_SELF_DEV_LOCKED", False)
        self.companion_name = os.environ.get("COMPANION_NAME", "").strip()[:60]
        self.companion_soul = os.environ.get("COMPANION_SOUL", "").strip()[:300]
        self.user_name = os.environ.get("COMPANION_USER_NAME", "").strip()[:60]
        self.user_profile = os.environ.get("COMPANION_USER_PROFILE", "").strip()[:300]
        self._ws_lock = threading.RLock()
        self._runtime_tier_resolver: Optional[Callable[[str, Dict[str, TierConfig]], Optional[TierConfig]]] = None
        self._runtime_privacy_resolver: Optional[Callable[[str], str]] = None
        self._runtime_self_dev_resolver: Optional[Callable[[bool], bool]] = None
        self._workspace = WorkspaceContext(
            project_id=SELF_PROJECT_ID,
            project_root=self.root.resolve(),
            self_root=self.root.resolve(),
            is_self_project=True,
        )
        self.tiers: Dict[str, TierConfig] = {}
        self._load_tiers()

    def _load_tiers(self) -> None:
        """Load configurable model tiers (`default`, `fast`, `coding`, `reasoning`, `audio`, `image`, `video`, `embedding`)."""
        def_dialect = os.environ.get("COMPANION_DIALECT", "openai_compat").strip().lower() or "openai_compat"
        self.tiers["default"] = TierConfig(
            tier="default", base_url=self.base_url.rstrip("/"), model=self.model, api_key=self.api_key,
            context_window_tokens=self.context_window_tokens, temperature=self.temperature, max_tokens=self.max_tokens,
            is_dedicated=True, dialect=def_dialect,
        )
        for t in ("fast", "coding", "reasoning", "audio", "image", "video", "embedding"):
            pfx = f"COMPANION_{t.upper()}_"
            raw_url, raw_model = os.environ.get(f"{pfx}BASE_URL", "").strip(), os.environ.get(f"{pfx}MODEL", "").strip()
            raw_key, raw_dialect = os.environ.get(f"{pfx}API_KEY", "").strip(), os.environ.get(f"{pfx}DIALECT", "").strip().lower()
            raw_effort = os.environ.get(f"{pfx}REASONING_EFFORT", "").strip().lower()
            raw_ctx, raw_temp_str = _env_int(f"{pfx}CONTEXT_WINDOW", 0), os.environ.get(f"{pfx}TEMP", "").strip()
            tier_temp = max(0.0, min(2.0, _env_float(f"{pfx}TEMP", _DEFAULT_TIER_TEMPS.get(t, self.temperature))))
            tier_max_tok = max(256, _env_int(f"{pfx}MAX_TOKENS", _DEFAULT_TIER_MAX_TOKENS.get(t, self.max_tokens)))
            self.tiers[t] = TierConfig(
                tier=t, base_url=(raw_url or self.base_url).rstrip("/"), model=raw_model or self.model,
                api_key=raw_key if raw_key else self.api_key,
                context_window_tokens=max(4096, raw_ctx) if raw_ctx > 0 else self.context_window_tokens,
                temperature=tier_temp, max_tokens=tier_max_tok,
                is_dedicated=bool(raw_url or raw_model or raw_ctx > 0 or raw_temp_str or raw_dialect or raw_effort),
                dialect=raw_dialect or def_dialect,
                reasoning_effort=raw_effort if raw_effort in ("low", "medium", "high") else "",
            )

    def register_runtime_resolver(
        self,
        tier_fn: Callable[[str, Dict[str, TierConfig]], Optional[TierConfig]],
        privacy_fn: Callable[[str], str],
    ) -> None:
        """Bind Domain-layer dynamic SQLite/Vault tier & privacy resolvers without upward A-Frame imports."""
        self._runtime_tier_resolver = tier_fn
        self._runtime_privacy_resolver = privacy_fn

    def effective_privacy_mode(self) -> str:
        """Return active privacy policy (`hybrid`, `local_only`, or `cloud_allowed`)."""
        if self._runtime_privacy_resolver is not None:
            return self._runtime_privacy_resolver(self.privacy_mode)
        return self.privacy_mode

    def register_self_dev_resolver(self, resolver_fn: Callable[[bool], bool]) -> None:
        """Bind Domain-layer `MemoryStore` resolver for `Self-Dev Mode` without upward A-Frame imports."""
        self._runtime_self_dev_resolver = resolver_fn

    def get_self_dev_override(self) -> Optional[bool]:
        return _SELF_DEV_OVERRIDE.get()

    def is_self_dev_locked_by_env(self) -> bool:
        """Return True when deployment env (`COMPANION_IMMUTABLE_CORE=1` or `COMPANION_SELF_DEV_LOCKED=1`) pins Self-Dev Mode."""
        return bool(self.immutable_core or self.self_dev_locked)

    def self_dev_lock_reason(self) -> str:
        """Describe active deployment env lock on `Self-Dev Mode` (or empty string when runtime-toggleable)."""
        if self.immutable_core:
            return "Pinned OFF by COMPANION_IMMUTABLE_CORE=1"
        if self.self_dev_locked:
            return f"Pinned {'ON' if self.self_dev_default else 'OFF'} by COMPANION_SELF_DEV_LOCKED=1"
        return ""

    def is_self_dev_enabled(self) -> bool:
        """Return effective `Self-Dev Mode` honoring ContextVar overrides, immutable/locked deployment envs, and SQLite."""
        override = _SELF_DEV_OVERRIDE.get()
        if override is not None:
            return bool(override)
        if self.immutable_core:
            return False
        if self.self_dev_locked:
            return bool(self.self_dev_default)
        if self._runtime_self_dev_resolver is not None:
            return bool(self._runtime_self_dev_resolver(self.self_dev_default))
        return bool(self.self_dev_default)

    def update_compaction_watermarks(self, trigger_pct: float, target_pct: float) -> None:
        """Hot-reload global context compaction trigger/target percentages in memory."""
        with self._ws_lock:
            self.compact_trigger_pct = max(5.0, min(90.0, float(trigger_pct)))
            self.compact_target_pct = max(3.0, min(self.compact_trigger_pct * 0.85, float(target_pct)))
            def_ctx = self.tiers.get("default").context_window_tokens if "default" in self.tiers else self.context_window_tokens
            self.max_context_chars = max(8000, int(def_ctx * (self.compact_trigger_pct / 100.0) * 4))

    def resolve_tier(self, tier: Optional[str] = None) -> TierConfig:
        """Resolve a logical model tier (`fast`, `coding`, `reasoning`, `audio`, `image`, `video`, `embedding`, `default`, or `auto`)."""
        key = (tier or "fast").strip().lower()
        if key in ("", "auto"):
            key = "fast"
        overrides = _TIER_OVERRIDE_MAP.get()
        if overrides and key in overrides:
            return overrides[key]
        if self._runtime_tier_resolver is not None:
            dyn = self._runtime_tier_resolver(key, self.tiers)
            if dyn is not None:
                return dyn
        return self.tiers.get(key, self.tiers["default"])

    @contextlib.contextmanager
    def tier_override_context(self, tier_name: str, cfg: TierConfig) -> Iterator[None]:
        """Thread-safely override a tier configuration inside a `ContextVar` scope without mutating `self.tiers`."""
        cur = dict(_TIER_OVERRIDE_MAP.get() or {})
        cur[tier_name.strip().lower()] = cfg
        tok = _TIER_OVERRIDE_MAP.set(cur)
        try:
            yield
        finally:
            _TIER_OVERRIDE_MAP.reset(tok)

    def tiers_share_endpoint(self, tier_a: str, tier_b: str) -> bool:
        """Return True when two tiers use different models on the same HTTP endpoint (container/VRAM swap gateway)."""
        cfg_a, cfg_b = self.resolve_tier(tier_a), self.resolve_tier(tier_b)
        return cfg_a.base_url.rstrip("/").lower() == cfg_b.base_url.rstrip("/").lower() and cfg_a.model != cfg_b.model

    def get_workspace(self) -> WorkspaceContext:
        """Return the active mechanical WorkspaceContext (respecting any scoped contextvar override)."""
        override = _WORKSPACE_OVERRIDE.get()
        if override is not None:
            return override
        with self._ws_lock:
            self_resolved = self.root.resolve()
            if self._workspace.self_root != self_resolved and self._workspace.is_self_project:
                self._workspace = WorkspaceContext(SELF_PROJECT_ID, self_resolved, self_resolved, True)
            return self._workspace

    def build_workspace_context(self, project_root: Path, project_id: Optional[str] = None) -> WorkspaceContext:
        """Construct a resolved `WorkspaceContext` distinguishing `companion-self` from external projects."""
        self_resolved = self.root.resolve()
        target_resolved = Path(project_root).resolve()
        is_self = target_resolved == self_resolved
        eff_id = SELF_PROJECT_ID if is_self else slugify_project_id(project_id or target_resolved.name)
        return WorkspaceContext(eff_id, target_resolved, self_resolved, is_self)

    def set_workspace(self, project_root: Path, project_id: Optional[str] = None) -> WorkspaceContext:
        """Mechanically lock tools and path guards to `project_root` (`companion-self` vs external project)."""
        ws = self.build_workspace_context(project_root, project_id)
        with self._ws_lock:
            self._workspace = ws
        return ws

    def reset_workspace(self) -> WorkspaceContext:
        """Reset the active WorkspaceContext back to `companion-self` (`self.root`)."""
        return self.set_workspace(self.root, SELF_PROJECT_ID)

    @property
    def active_project_root(self) -> Path:
        return self.get_workspace().project_root

    @property
    def effective_data_dir(self) -> Path:
        return self.data_dir if self.data_dir else (self.root / "data")

    @property
    def db_path(self) -> Path:
        return self.effective_data_dir / "conversations.db"

    @property
    def memory_md_path(self) -> Path:
        return self.effective_data_dir / "MEMORY.md"

    @property
    def identity_md_path(self) -> Path:
        return self.effective_data_dir / "IDENTITY.md"

    @property
    def specs_dir(self) -> Path:
        return self.root / "specs"

    @property
    def skills_dir(self) -> Path:
        return self.root / "skills"

    @property
    def user_skills_dir(self) -> Path:
        return self.effective_data_dir / "skills"

    @property
    def backup_dir(self) -> Path:
        return self.effective_data_dir / ".companion_backups"

    @property
    def secrets_env_path(self) -> Path:
        override = os.environ.get("COMPANION_SECRETS_PATH", "").strip()
        if override:
            p = Path(override)
            return p.resolve() if p.is_absolute() else (self.root / p).resolve()
        return self.effective_data_dir / "secrets.env"

    @property
    def wiki_dir(self) -> Path:
        override = os.environ.get("COMPANION_WIKI_DIR", "").strip()
        if override:
            p = Path(override)
            return p.resolve() if p.is_absolute() else (self.root / p).resolve()
        return self.effective_data_dir / "wiki"

    @property
    def snapshots_dir(self) -> Path:
        return self.effective_data_dir / "snapshots"

    @property
    def sandboxes_dir(self) -> Path:
        return self.effective_data_dir / "sandboxes"

    @property
    def compact_trigger_tokens(self) -> int:
        return int(self.context_window_tokens * (self.compact_trigger_pct / 100.0))

    @property
    def compact_target_tokens(self) -> int:
        return int(self.context_window_tokens * (self.compact_target_pct / 100.0))

    def validate(self) -> list[str]:
        """Validate configuration and return a list of any warnings/issues."""
        issues: list[str] = []
        if not self.base_url.startswith(("http://", "https://")):
            issues.append(f"COMPANION_BASE_URL must start with http:// or https:// (got '{self.base_url}')")
        if not self.model:
            issues.append("COMPANION_MODEL must not be empty")
        for t_name, t_cfg in self.tiers.items():
            if not t_cfg.base_url.startswith(("http://", "https://")):
                issues.append(f"Tier '{t_name}' base_url must start with http:// or https:// (got '{t_cfg.base_url}')")
            if not t_cfg.model:
                issues.append(f"Tier '{t_name}' model must not be empty")
        if not self.root.is_dir():
            issues.append(f"COMPANION_ROOT directory does not exist: {self.root}")
        return issues

    def to_dict(self) -> Dict[str, object]:
        ws = self.get_workspace()
        return {
            "base_url": self.base_url,
            "model": self.model,
            "api_key_set": bool(self.api_key),
            "web_host": self.web_host,
            "web_token_set": bool(self.web_token),
            "privacy_mode": self.effective_privacy_mode(),
            "secrets_env_present": self.secrets_env_path.is_file(),
            "root": str(self.root),
            "data_dir": str(self.effective_data_dir),
            "wiki_dir": str(self.wiki_dir),
            "active_project_id": ws.project_id,
            "active_project_root": str(ws.project_root),
            "is_self_project": ws.is_self_project,
            "self_dev_enabled": self.is_self_dev_enabled(),
            "self_dev_locked": self.is_self_dev_locked_by_env(),
            "self_dev_lock_reason": self.self_dev_lock_reason(),
            "max_iters": self.max_iters,
            "temperature": self.temperature,
            "fast_temperature": self.resolve_tier("fast").temperature,
            "coding_temperature": self.resolve_tier("coding").temperature,
            "reasoning_temperature": self.resolve_tier("reasoning").temperature,
            "auto_git_commit": self.auto_git_commit,
            "shell_timeout": self.shell_timeout,
            "http_timeout": self.http_timeout,
            "out_limit": self.out_limit,
            "search_limit": self.search_limit,
            "context_window_tokens": self.context_window_tokens,
            "compact_trigger_pct": self.compact_trigger_pct,
            "compact_target_pct": self.compact_target_pct,
            "compact_trigger_tokens": self.compact_trigger_tokens,
            "max_context_chars": self.max_context_chars,
            "idle_minutes": self.idle_minutes,
            "hobby_cooldown_minutes": self.hobby_cooldown_minutes,
            "hobby_max_iters": self.hobby_max_iters,
            "max_module_lines": self.max_module_lines,
            "max_function_lines": self.max_function_lines,
        }

    def __repr__(self) -> str:
        return f"Settings(model={self.model!r}, base_url={self.base_url!r}, root={str(self.root)!r})"


@contextlib.contextmanager
def workspace_context(project_root: Path, project_id: Optional[str] = None) -> Iterator[WorkspaceContext]:
    """Temporarily bind a scoped WorkspaceContext for isolated execution or testing."""
    ws = settings.build_workspace_context(project_root, project_id)
    tok = _WORKSPACE_OVERRIDE.set(ws)
    try:
        yield ws
    finally:
        _WORKSPACE_OVERRIDE.reset(tok)


settings = Settings()
