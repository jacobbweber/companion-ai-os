"""
Blind Credential Vault, `vault://` JIT Resolver, Output Scrubber & Secret-Free Snapshot Backups
(`companion/vault.py` — A-Frame Domain Layer).

Enforces a strictly write-only, zero-reveal security model designed for Zero-Login Home LAN use:
1. Raw secret values live ONLY in owner-locked `secrets.env` (never in SQLite, backups, logs, CLI, Web API, or LLM context).
2. SQLite `vault_entries` & `vault_audit_log` track symbolic names, types, allow-lists, expiry, and usage timestamps.
3. `resolve_for_call()` injects secrets Just-In-Time for authorized `caller_scope` connectors and scrubs outputs.
4. `create_snapshot_backup()` & `restore_snapshot_backup()` protect SQLite + Wiki + Identity while skipping `secrets.env`
   and reporting `missing_secrets` after restore.
"""

import ast
import base64
import contextlib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from companion.approvals import get_approval_queue
from companion.config import (
    GENERAL_TRACKED_EXTENSIONS, SELF_PROJECT_ID, SKIP_DIRS,
    is_in_agent_tool_turn, parse_env_file, settings, slugify_project_id, walk_pruned_files,
)
from companion.memory_store import MemoryStore, get_memory_store, has_memory_store_override
from companion.telemetry import notify_db_closed, redact_secrets, secret_redactor

__all__ = [
    "CheckpointManager", "VALID_CREDENTIAL_TYPES", "VaultManager", "checkpoint_manager",
    "create_snapshot_backup", "get_active_checkpoint_manager", "get_self_checkpoint_manager",
    "get_vault_manager", "list_snapshot_backups", "normalize_secret_name", "restore_snapshot_backup",
    "run_system_doctor", "run_unit_tests", "validate_python_syntax",
]

VALID_CREDENTIAL_TYPES = ("api_key", "bearer_token", "basic_auth", "ssh_key", "custom")

VAULT_DDL: Tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS vault_entries (name TEXT PRIMARY KEY, cred_type TEXT NOT NULL DEFAULT 'api_key', description TEXT DEFAULT '', allow_list_json TEXT DEFAULT '[]', expires_at TEXT DEFAULT '', created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, last_used_at DATETIME DEFAULT '')",
    "CREATE TABLE IF NOT EXISTS vault_audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, secret_name TEXT NOT NULL, caller_scope TEXT NOT NULL, outcome TEXT NOT NULL, detail TEXT DEFAULT '', created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
    "CREATE INDEX IF NOT EXISTS idx_vault_audit_name ON vault_audit_log(secret_name, id DESC)",
)

_SECRET_ENV_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASS", "_PAT")
_SECRETS_FILE_LOCK = threading.RLock()


def _lock_windows_owner_dacl(target: Path) -> None:
    """Restrict a sensitive credential file to the current Windows user only via `icacls` (best-effort)."""
    if os.name != "nt":
        return
    user = os.environ.get("USERNAME", "").strip()
    domain = os.environ.get("USERDOMAIN", "").strip()
    principal = f"{domain}\\{user}" if domain and user else user
    if not principal:
        return
    try:
        subprocess.run(
            ["icacls", str(target), "/inheritance:r", "/grant:r", f"{principal}:F"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


def normalize_secret_name(raw: str) -> str:
    """Normalize a symbolic credential reference (stripping optional `vault://`) into a clean kebab-case slug."""
    cleaned = (raw or "").strip()
    if cleaned.lower().startswith("vault://"):
        cleaned = cleaned[8:]
    return re.sub(r"[^a-z0-9_-]+", "-", cleaned.strip().lower()).strip("-")[:64]


def _encode_secret_for_env(raw_val: str) -> str:
    if "\n" in raw_val or "\r" in raw_val or '"' in raw_val or "#" in raw_val:
        b64 = base64.b64encode(raw_val.encode("utf-8")).decode("ascii")
        return f'"b64:{b64}"'
    return f'"{raw_val}"'


def _decode_secret_from_env(stored_val: str) -> str:
    if stored_val.startswith("b64:"):
        try:
            return base64.b64decode(stored_val[4:].encode("ascii")).decode("utf-8")
        except Exception:
            return stored_val
    return stored_val


def _scope_matches(caller_scope: str, allow_list: List[str]) -> bool:
    scope = (caller_scope or "").strip().lower()
    if not scope:
        return False
    for pat in allow_list:
        p = str(pat or "").strip().lower()
        if p == "*" or p == scope or fnmatch.fnmatchcase(scope, p):
            return True
    return False


class VaultManager:
    """Write-only, zero-reveal Credential Vault backed by `secrets.env` + SQLite metadata/audit tables."""

    def __init__(self, store: Optional[MemoryStore] = None, secrets_path: Optional[Path] = None):
        self._store = store
        self._secrets_path = secrets_path

    @property
    def store(self) -> MemoryStore:
        mem = self._store or get_memory_store()
        mem.ensure_schema("vault", lambda m: ([m.conn.execute(d) for d in VAULT_DDL], m.conn.commit()) and None)
        return mem

    @property
    def secrets_path(self) -> Path:
        if self._secrets_path is not None:
            return Path(self._secrets_path).resolve()
        mem = self._store or (get_memory_store() if has_memory_store_override() else None)
        if mem is not None and mem.db_path.resolve() != settings.db_path.resolve():
            return (mem.db_path.parent / "secrets.env").resolve()
        return settings.secrets_env_path.resolve()

    def _read_raw_map(self) -> Dict[str, str]:
        raw_map = parse_env_file(self.secrets_path)
        return {normalize_secret_name(k): _decode_secret_from_env(v) for k, v in raw_map.items() if normalize_secret_name(k)}

    def _write_raw_map(self, values: Dict[str, str]) -> None:
        target = self.secrets_path
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Companion Blind Credential Vault (`secrets.env` — Owner-Locked, Write-Only, Never Committed)",
            "# Values are never returned to CLI, Web UI, logs, backups, or LLM tools.",
        ]
        for k in sorted(values):
            lines.append(f"{k}={_encode_secret_for_env(values[k])}")
        content = "\n".join(lines) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=".secrets_", suffix=".tmp", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
            try:
                os.chmod(tmp_name, 0o600)
            except OSError:
                pass
            os.replace(tmp_name, target)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
            _lock_windows_owner_dacl(target)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    def _register_redaction_values(self, raw_secret: str) -> None:
        candidates = [raw_secret.strip()]
        for ln in raw_secret.splitlines():
            s = ln.strip()
            if len(s) >= 8 and not s.startswith("-----"):
                candidates.append(s)
        for cand in candidates:
            if len(cand) >= 6:
                secret_redactor.register_secret(cand)

    def _audit(self, secret_name: str, caller_scope: str, outcome: str, detail: str = "") -> None:
        with self.store._lock:
            self.store.conn.execute(
                "INSERT INTO vault_audit_log (secret_name, caller_scope, outcome, detail) VALUES (?, ?, ?, ?)",
                (secret_name[:64], caller_scope[:80], outcome[:32], redact_secrets(detail)[:240]),
            )
            self.store.conn.commit()

    def declare_placeholder(
        self, name: str, cred_type: str = "api_key", description: str = "", allow_list: Optional[List[str]] = None, actor: str = "user",
    ) -> Dict[str, Any]:
        """Register symbolic secret metadata (without any value) without ever widening an existing secret's scope."""
        slug = normalize_secret_name(name)
        ctype = (cred_type or "api_key").strip().lower()
        if not slug:
            return {"ok": False, "error": "Secret `name` is required."}
        if ctype not in VALID_CREDENTIAL_TYPES:
            ctype = "custom"
        existing = self._get_entry_row(slug)
        clean_desc = redact_secrets(description.strip())[:240]
        if existing is not None:
            if clean_desc and not existing.get("description"):
                with self.store._lock:
                    self.store.conn.execute("UPDATE vault_entries SET description = ?, updated_at = CURRENT_TIMESTAMP WHERE name = ?", (clean_desc, slug))
                    self.store.conn.commit()
            return {"ok": True, "entry": self.get_metadata(slug)}
        is_human = actor.strip().lower() in ("user", "cli", "web") and not is_in_agent_tool_turn()
        init_allow = sorted({str(s).strip() for s in (allow_list or []) if str(s).strip() and (is_human or str(s).strip() != "*")})
        with self.store._lock:
            self.store.conn.execute(
                "INSERT OR IGNORE INTO vault_entries (name, cred_type, description, allow_list_json, updated_at) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (slug, ctype, clean_desc, json.dumps(init_allow)),
            )
            self.store.conn.commit()
        return {"ok": True, "entry": self.get_metadata(slug)}

    def set_secret(
        self,
        name: str,
        raw_value: str,
        cred_type: str = "api_key",
        description: str = "",
        allow_list: Optional[List[str]] = None,
        expires_at: str = "",
        actor: str = "user",
    ) -> Dict[str, Any]:
        """Write a secret into `secrets.env` (user/CLI/Web only — mechanically blocked for `companion`). Never echoes value."""
        if actor.strip().lower() not in ("user", "cli", "web") or is_in_agent_tool_turn():
            return {"ok": False, "error": "VAULT GUARD REJECTED: Secrets are write-only by the user; Companion cannot write raw secret values."}
        slug = normalize_secret_name(name)
        if not slug or not (raw_value or "").strip():
            return {"ok": False, "error": "Both symbolic secret `name` and non-empty value are required."}
        ctype = (cred_type or "api_key").strip().lower()
        if ctype not in VALID_CREDENTIAL_TYPES:
            return {"ok": False, "error": f"Invalid credential type '{cred_type}'. Valid: {', '.join(VALID_CREDENTIAL_TYPES)}."}

        secret_clean = raw_value.replace("\r\n", "\n").strip("\r")
        self._register_redaction_values(secret_clean)
        with _SECRETS_FILE_LOCK:
            raw_map = self._read_raw_map()
            raw_map[slug] = secret_clean
            self._write_raw_map(raw_map)

        existing = self._get_entry_row(slug)
        eff_allow = [str(s).strip() for s in allow_list if str(s).strip()] if allow_list is not None else (existing["allow_list"] if existing else ["*"])
        eff_desc = description.strip() if description.strip() else (existing["description"] if existing else "")
        eff_exp = expires_at.strip() if expires_at else (existing["expires_at"] if existing else "")
        with self.store._lock:
            self.store.conn.execute(
                """INSERT INTO vault_entries (name, cred_type, description, allow_list_json, expires_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(name) DO UPDATE SET
                     cred_type = excluded.cred_type, description = excluded.description,
                     allow_list_json = excluded.allow_list_json, expires_at = excluded.expires_at,
                     updated_at = CURRENT_TIMESTAMP""",
                (slug, ctype, redact_secrets(eff_desc)[:240], json.dumps(sorted(set(eff_allow))), eff_exp),
            )
            self.store.conn.commit()
        self._audit(slug, f"actor:{actor}", "set", f"type={ctype} scopes={len(eff_allow)}")
        meta = self.get_metadata(slug)
        return {"ok": True, "secret": meta, "summary": f"Stored blind credential `{slug}` (`{ctype}`, set=True, scopes={meta['allow_list'] if meta else []})."}

    def delete_secret(self, name: str, remove_metadata: bool = False, actor: str = "user") -> Dict[str, Any]:
        """Remove a secret value from `secrets.env` (user/CLI/Web only)."""
        if actor.strip().lower() not in ("user", "cli", "web") or is_in_agent_tool_turn():
            return {"ok": False, "error": "VAULT GUARD REJECTED: Only the user can delete Vault secrets."}
        slug = normalize_secret_name(name)
        with _SECRETS_FILE_LOCK:
            raw_map = self._read_raw_map()
            existed = slug in raw_map
            if existed:
                raw_map.pop(slug, None)
                self._write_raw_map(raw_map)
        if remove_metadata:
            with self.store._lock:
                self.store.conn.execute("DELETE FROM vault_entries WHERE name = ?", (slug,))
                self.store.conn.commit()
        self._audit(slug, f"actor:{actor}", "deleted", f"remove_metadata={remove_metadata}")
        return {"ok": True, "name": slug, "was_set": existed, "summary": f"Cleared secret value for `{slug}` (metadata_removed={remove_metadata})."}

    def _get_entry_row(self, slug: str) -> Optional[Dict[str, Any]]:
        cols = ["name", "cred_type", "description", "allow_list_json", "expires_at", "created_at", "updated_at", "last_used_at"]
        rows = self.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM vault_entries WHERE name = ?", (slug,))
        if not rows:
            return None
        r = rows[0]
        try:
            allow = json.loads(r.pop("allow_list_json") or "[]")
        except Exception:
            allow = []
        r["allow_list"] = [str(x) for x in allow] if isinstance(allow, list) else []
        return r

    def get_metadata(self, name: str) -> Optional[Dict[str, Any]]:
        """Return strictly non-secret metadata for `name` (`is_set`, `cred_type`, `allow_list`, `last_used_at`)."""
        slug = normalize_secret_name(name)
        raw_map = self._read_raw_map()
        row = self._get_entry_row(slug)
        if row is None and slug not in raw_map:
            return None
        base = row or {
            "name": slug, "cred_type": "custom", "description": "Declared in secrets.env",
            "allow_list": ["*"], "expires_at": "", "created_at": "", "updated_at": "", "last_used_at": "",
        }
        return {**base, "is_set": bool(raw_map.get(slug))}

    def list_secrets(self) -> List[Dict[str, Any]]:
        """List all Vault entries with `is_set` status — NEVER includes raw secret values."""
        raw_map = self._read_raw_map()
        for val in raw_map.values():
            self._register_redaction_values(val)
        cols = ["name", "cred_type", "description", "allow_list_json", "expires_at", "created_at", "updated_at", "last_used_at"]
        rows = self.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM vault_entries ORDER BY name ASC")
        seen = set()
        items: List[Dict[str, Any]] = []
        for r in rows:
            seen.add(r["name"])
            try:
                allow = json.loads(r.pop("allow_list_json") or "[]")
            except Exception:
                allow = []
            items.append({**r, "allow_list": allow if isinstance(allow, list) else [], "is_set": bool(raw_map.get(r["name"]))})
        for env_slug in sorted(set(raw_map) - seen):
            items.append({
                "name": env_slug, "cred_type": "custom", "description": "Present in secrets.env",
                "allow_list": ["*"], "expires_at": "", "created_at": "", "updated_at": "", "last_used_at": "", "is_set": True,
            })
        return items

    def list_missing_secrets(self) -> List[Dict[str, str]]:
        """Return declared secret references across Vault, Endpoints, MCP servers, and Partners that have no value set."""
        raw_map = self._read_raw_map()
        missing: Dict[str, Dict[str, str]] = {}
        for item in self.list_secrets():
            if not item["is_set"]:
                missing[item["name"]] = {"name": item["name"], "cred_type": item["cred_type"], "needed_by": "vault_entries"}

        ref_queries = (
            ("remote_endpoints", "SELECT endpoint_id, credential_ref FROM remote_endpoints WHERE credential_ref != ''"),
            ("mcp_servers", "SELECT server_id, credential_ref FROM mcp_servers WHERE credential_ref != ''"),
            ("federated_partners", "SELECT partner_id, credential_ref FROM federated_partners WHERE credential_ref != ''"),
            ("llm_providers", "SELECT provider_id, credential_ref FROM llm_providers WHERE credential_ref != ''"),
            ("projects", "SELECT project_id, git_credential_ref FROM projects WHERE git_credential_ref != ''"),
        )
        with self.store._lock:
            existing_tables = {r[0] for r in self.store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            for tbl, sql in ref_queries:
                if tbl not in existing_tables:
                    continue
                for owner_id, cred_ref in self.store.conn.execute(sql).fetchall():
                    slug = normalize_secret_name(str(cred_ref))
                    if slug and not raw_map.get(slug):
                        entry = missing.setdefault(slug, {"name": slug, "cred_type": "custom", "needed_by": f"{tbl}:{owner_id}"})
                        if f"{tbl}:{owner_id}" not in entry["needed_by"]:
                            entry["needed_by"] += f", {tbl}:{owner_id}"
        return [missing[k] for k in sorted(missing)]

    def grant_scope(self, name: str, caller_scope: str, actor: str = "companion", confirmed: bool = False) -> Dict[str, Any]:
        """Grant `caller_scope` permission to use `name` (always approval-gated when requested by Companion)."""
        slug, scope = normalize_secret_name(name), (caller_scope or "").strip().lower()
        if not slug or not scope:
            return {"ok": False, "error": "Both `name` and `caller_scope` are required."}
        meta = self.get_metadata(slug)
        if meta is None:
            self.declare_placeholder(slug)
            meta = self.get_metadata(slug) or {"allow_list": []}
        if _scope_matches(scope, meta.get("allow_list", [])):
            return {"ok": True, "name": slug, "allow_list": meta["allow_list"], "summary": f"Scope `{scope}` is already authorized for `{slug}`."}

        if actor.strip().lower() not in ("user", "cli", "web"):
            allowed, apr = get_approval_queue(self.store).check_or_enqueue(
                category="vault_scope",
                action_name="grant_scope",
                target=slug,
                summary=f"Allow `{scope}` to use Vault credential `{slug}` at execution time.",
                payload={"name": slug, "caller_scope": scope},
                requester=actor,
                confirmed=confirmed,
                always_require_in_autonomous=True,
            )
            if not allowed:
                return {
                    "ok": False,
                    "awaiting_approval": True,
                    "approval_id": apr["approval_id"],
                    "error": f"VAULT SCOPE APPROVAL REQUIRED (`{apr['approval_id']}`): Granting `{scope}` access to `{slug}` awaits user approval.",
                }

        new_allow = sorted(set(meta.get("allow_list", []) + [scope]))
        with self.store._lock:
            self.store.conn.execute("UPDATE vault_entries SET allow_list_json = ?, updated_at = CURRENT_TIMESTAMP WHERE name = ?", (json.dumps(new_allow), slug))
            self.store.conn.commit()
        self._audit(slug, scope, "scope_granted", f"by={actor}")
        return {"ok": True, "name": slug, "allow_list": new_allow, "summary": f"Authorized `{scope}` to use Vault credential `{slug}`."}

    def resolve_for_call(
        self, ref: str, caller_scope: str, expected_types: Optional[Tuple[str, ...]] = None,
    ) -> Tuple[bool, str]:
        """Internal connector-only JIT resolver. Validates `is_set`, expiry, `allow_list`, and registers redaction."""
        slug = normalize_secret_name(ref)
        if not slug:
            return False, "Empty credential_ref."
        raw_map = self._read_raw_map()
        secret_val = raw_map.get(slug, "")
        meta = self.get_metadata(slug)
        if not secret_val or meta is None:
            self._audit(slug, caller_scope, "denied_missing", "Secret value not set in secrets.env")
            return False, f"Vault credential `{slug}` is missing/unset. Set it via `/vault set {slug} <type> <value>`."
        exp = str(meta.get("expires_at") or "").strip()
        if exp:
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if exp[:10] < now_iso:
                self._audit(slug, caller_scope, "denied_expired", f"expired={exp}")
                return False, f"Vault credential `{slug}` expired on `{exp}`."
        if expected_types and meta.get("cred_type") not in (*expected_types, "custom"):
            return False, f"Vault credential `{slug}` has type `{meta.get('cred_type')}` (expected one of {expected_types})."
        if not _scope_matches(caller_scope, meta.get("allow_list", [])):
            self._audit(slug, caller_scope, "denied_scope", f"allow_list={meta.get('allow_list')}")
            return False, f"Vault credential `{slug}` is not authorized for `{caller_scope}` (request scope via `vault_manage`)."

        self._register_redaction_values(secret_val)
        with self.store._lock:
            self.store.conn.execute("UPDATE vault_entries SET last_used_at = CURRENT_TIMESTAMP WHERE name = ?", (slug,))
            self.store.conn.commit()
        self._audit(slug, caller_scope, "resolved", "JIT injected")
        return True, secret_val

    def scrub_output(self, text: str, extra_secrets: Optional[List[str]] = None) -> str:
        """Scrub all Vault secrets (including basic-auth base64 & multiline key lines) from connector outputs."""
        if not text:
            return ""
        for val in self._read_raw_map().values():
            self._register_redaction_values(val)
        for ex in extra_secrets or []:
            if ex:
                self._register_redaction_values(ex)
        return redact_secrets(text)

    def get_audit_log(self, limit: int = 25) -> List[Dict[str, Any]]:
        cols = ["id", "secret_name", "caller_scope", "outcome", "detail", "created_at"]
        return self.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM vault_audit_log ORDER BY id DESC LIMIT ?", (max(1, int(limit)),))

    def format_status(self) -> str:
        items = self.list_secrets()
        missing = self.list_missing_secrets()
        lines = [f"=== Blind Credential Vault (`{self.secrets_path.name}` — Zero-Reveal Policy Active) ==="]
        if not items and not missing:
            lines.append("  (No credentials registered yet. Add one with `/vault set <name> <api_key|ssh_key|basic_auth|bearer_token> <value>`.)")
            return "\n".join(lines)
        for it in items:
            badge = "SET" if it["is_set"] else "MISSING"
            used = f" | last_used={it['last_used_at']}" if it.get("last_used_at") else ""
            lines.append(f"  * `{it['name']}` [{badge} | {it['cred_type']}] scopes={it['allow_list']}{used} — {it.get('description') or 'no description'}")
        if missing:
            lines.append("\nMissing Declared Credentials:")
            for m in missing:
                lines.append(f"  ! `{m['name']}` ({m['cred_type']}) needed by {m['needed_by']}")
        return "\n".join(lines)


def get_vault_manager(store: Optional[MemoryStore] = None, secrets_path: Optional[Path] = None) -> VaultManager:
    return VaultManager(store=store, secrets_path=secrets_path)


def create_snapshot_backup(
    label: str = "manual", store: Optional[MemoryStore] = None, snapshots_dir: Optional[Path] = None, wiki_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Create a secret-free snapshot backup of SQLite, Wiki Warehouse, Identity, and non-secret config."""
    mem = store or get_memory_store()
    vault = get_vault_manager(store=mem)
    snap_root = Path(snapshots_dir or settings.snapshots_dir).resolve()
    eff_wiki = Path(wiki_dir or settings.wiki_dir).resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    safe_lbl = re.sub(r"[^a-zA-Z0-9_-]+", "_", (label or "manual").strip())[:32] or "manual"
    snap_id = f"snap_{stamp}_{safe_lbl}"
    dest = snap_root / snap_id
    dest.mkdir(parents=True, exist_ok=True)

    db_dest = dest / "conversations.db"
    with mem._lock:
        dst_conn = sqlite3.connect(str(db_dest))
        try:
            mem.conn.backup(dst_conn)
        finally:
            dst_conn.close()

    wiki_copied = 0
    if eff_wiki.is_dir():
        shutil.copytree(eff_wiki, dest / "wiki", dirs_exist_ok=True)
        wiki_copied = len(walk_pruned_files(dest / "wiki"))

    for doc_p, name in ((settings.memory_md_path, "MEMORY.md"), (settings.identity_md_path, "IDENTITY.md")):
        if doc_p.is_file():
            shutil.copy2(doc_p, dest / name)

    env_file = (settings.root / ".env") if (settings.root / ".env").is_file() else (settings.effective_data_dir / ".env")
    if env_file.is_file():
        safe_env = [
            f"{k}={v}"
            for k, v in parse_env_file(env_file).items()
            if not k.upper().endswith(_SECRET_ENV_SUFFIXES)
        ]
        (dest / "config_non_secret.env").write_text("\n".join(safe_env) + "\n", encoding="utf-8")

    declared = [{"name": s["name"], "cred_type": s["cred_type"], "allow_list": s["allow_list"]} for s in vault.list_secrets()]
    manifest = {
        "snapshot_id": snap_id,
        "label": label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "wiki_files": wiki_copied,
        "secrets_included": False,
        "declared_secrets": declared,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "snapshot_id": snap_id,
        "path": str(dest),
        "wiki_files": wiki_copied,
        "declared_secrets_count": len(declared),
        "summary": f"Created secret-free snapshot `{snap_id}` (SQLite + {wiki_copied} Wiki files; `secrets.env` excluded).",
    }


def list_snapshot_backups(snapshots_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    snap_root = Path(snapshots_dir or settings.snapshots_dir).resolve()
    if not snap_root.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for d in sorted(snap_root.iterdir(), key=lambda p: p.name, reverse=True):
        mf = d / "manifest.json"
        if d.is_dir() and mf.is_file():
            try:
                out.append(json.loads(mf.read_text(encoding="utf-8")))
            except Exception:
                continue
    return out


def restore_snapshot_backup(
    snapshot_id: str, actor: str = "user", store: Optional[MemoryStore] = None,
    snapshots_dir: Optional[Path] = None, wiki_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Restore a snapshot backup and return the checklist of missing Vault secrets needing re-entry."""
    if actor.strip().lower() not in ("user", "cli", "web") or is_in_agent_tool_turn():
        return {"ok": False, "error": "BACKUP GUARD REJECTED: Restoring a full snapshot requires direct user invocation."}
    snap_root = Path(snapshots_dir or settings.snapshots_dir).resolve()
    clean_id = (snapshot_id or "").strip()
    if not clean_id:
        return {"ok": False, "error": "Snapshot `snapshot_id` is required."}
    src = (snap_root / clean_id).resolve()
    if src == snap_root or not src.is_relative_to(snap_root) or not src.is_dir() or not (src / "manifest.json").is_file():
        return {"ok": False, "error": f"Snapshot `{snapshot_id}` not found in `{snap_root}`."}

    mem = store or get_memory_store()
    db_src = src / "conversations.db"
    if db_src.is_file():
        with mem._lock:
            if mem.conn.in_transaction:
                mem.conn.rollback()
            notify_db_closed(mem.db_path)
            src_conn = sqlite3.connect(str(db_src))
            try:
                src_conn.backup(mem.conn)
                mem._initialized_schemas.clear()
            finally:
                src_conn.close()

    eff_wiki = Path(wiki_dir or settings.wiki_dir).resolve()
    if (src / "wiki").is_dir():
        if eff_wiki.is_dir():
            shutil.rmtree(eff_wiki, ignore_errors=True)
        shutil.copytree(src / "wiki", eff_wiki, dirs_exist_ok=True)

    missing = get_vault_manager(store=mem).list_missing_secrets()
    miss_names = [m["name"] for m in missing]
    miss_note = f" Missing secrets to re-enter: {', '.join(miss_names)}." if miss_names else " All declared secrets are present."
    return {
        "ok": True,
        "snapshot_id": snapshot_id,
        "missing_secrets": missing,
        "summary": f"Restored snapshot `{snapshot_id}`.{miss_note}",
    }



def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_secret_env_name(name: str) -> bool:
    low = name.lower()
    return low in (".env", "secrets.env") or (low.startswith(".env.") and low not in (".env.example", ".env.sample", ".env.template"))


def validate_python_syntax(source: str, filename: str = "<string>") -> Tuple[bool, Optional[str]]:
    """Validate that Python source code parses and compiles without SyntaxError."""
    try:
        tree = ast.parse(source, filename=filename)
        compile(tree, filename=filename, mode="exec")
        return True, None
    except SyntaxError as exc:
        return False, f"SyntaxError at line {exc.lineno}, col {exc.offset}: {exc.msg}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _check_py_file_syntax(path: Path) -> Tuple[bool, Optional[str]]:
    try:
        return validate_python_syntax(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


class CheckpointManager:
    """Manages Copy-on-Write file ledgers, external content-addressed checkpoints, and read-only tree guards."""

    SKIP_DIRS = SKIP_DIRS
    TRACKED_EXTENSIONS = set(GENERAL_TRACKED_EXTENSIONS)
    _RO_SKIP_SUFFIXES = {".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3"}
    _RO_SKIP_RELS = {"MEMORY.md", "IDENTITY.md", "secrets.env"}

    def __init__(self, root: Optional[Path] = None, backup_dir: Optional[Path] = None, store: Optional[Any] = None):
        self.root = (root or settings.root).resolve()
        pid = SELF_PROJECT_ID if self.root == settings.root.resolve() else slugify_project_id(self.root.name)
        self.backup_dir = (Path(backup_dir) if backup_dir is not None else (settings.effective_data_dir / "checkpoints" / pid)).resolve()
        self._store = store
        self.file_backups_dir = self.backup_dir / "files"
        self.checkpoints_dir = self.backup_dir / "checkpoints"
        self.blobs_dir = self.backup_dir / "blobs"
        self.ledger_path = self.backup_dir / "touched_ledger.jsonl"
        self._ledger_lock = threading.RLock()
        self._ro_stat_cache: Dict[str, Tuple[int, int, str]] = {}
        self._savepoints_cache: Optional[Tuple[Tuple[float, float, int], List[Dict[str, str]]]] = None

    def _get_store(self) -> Optional[Any]:
        if self._store is False:
            return None
        if self._store is not None:
            return self._store
        ws = settings.get_workspace()
        if self.root in (settings.root.resolve(), ws.project_root):
            return get_memory_store()
        return None

    def _safe_rel_key(self, path: Path) -> str:
        rel = self._rel_posix(path)
        return rel.replace("/", "__") if rel else path.name

    def _rel_posix(self, path: Path) -> Optional[str]:
        try:
            return str(path.resolve().relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return None

    def _store_blob(self, data: bytes) -> str:
        sha = _sha256_bytes(data)
        blob_path = self.blobs_dir / sha[:2] / sha
        if not blob_path.is_file():
            blob_path.parent.mkdir(parents=True, exist_ok=True)
            blob_path.write_bytes(data)
        return sha

    def _read_blob(self, sha: str) -> Optional[bytes]:
        blob_path = (self.blobs_dir / sha[:2] / sha) if sha else None
        return blob_path.read_bytes() if (blob_path and blob_path.is_file()) else None

    def _load_ledger(self) -> List[Dict[str, Any]]:
        if not self.ledger_path.is_file():
            return []
        entries: List[Dict[str, Any]] = []
        with self._ledger_lock:
            for ln in self.ledger_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if ln.strip():
                    with contextlib.suppress(Exception):
                        entries.append(json.loads(ln))
        return entries

    def record_file_touch(
        self, path: Path, existed_before: bool, before_bytes: Optional[bytes], after_bytes: Optional[bytes], op: str = "write_file",
    ) -> Optional[Dict[str, Any]]:
        rel = self._rel_posix(path)
        if not rel or _is_secret_env_name(path.name):
            return None
        with self._ledger_lock:
            before_sha = self._store_blob(before_bytes) if (existed_before and before_bytes is not None) else ""
            after_sha = self._store_blob(after_bytes) if after_bytes is not None else ""
            entry = {
                "seq": len(self._load_ledger()) + 1, "ts": datetime.now().isoformat(), "rel_path": rel,
                "existed_before": bool(existed_before), "before_sha256": before_sha, "after_sha256": after_sha, "op": op,
            }
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with self.ledger_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
            return entry

    def backup_file(self, path: Path) -> Optional[Path]:
        target = path.resolve()
        if not target.is_file() or not target.is_relative_to(self.root) or _is_secret_env_name(target.name):
            return None
        dest_dir = self.file_backups_dir / self._safe_rel_key(target)
        dest_dir.mkdir(parents=True, exist_ok=True)
        backup_path = dest_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{target.name}"
        shutil.copy2(target, backup_path)
        self._store_blob(target.read_bytes())
        backups = sorted(dest_dir.glob(f"*_{target.name}"), key=lambda p: p.stat().st_mtime)
        while len(backups) > settings.max_backups_per_file:
            with contextlib.suppress(Exception):
                backups.pop(0).unlink()
        return backup_path

    def restore_latest_file_backup(self, path: Path) -> Optional[Path]:
        target = path.resolve()
        dest_dir = self.file_backups_dir / self._safe_rel_key(target)
        backups = sorted(dest_dir.glob(f"*_{target.name}"), key=lambda p: p.stat().st_mtime) if dest_dir.is_dir() else []
        if not backups:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backups[-1], target)
        return backups[-1]

    def _iter_workspace_files(self) -> List[Path]:
        out: List[Path] = []
        if not self.root.is_dir():
            return out
        for cur_r, dirs, fnames in os.walk(self.root):
            cur_p = Path(cur_r)
            dirs[:] = sorted(d for d in dirs if d not in self.SKIP_DIRS and not (cur_p / d).is_symlink())
            for fn in sorted(fnames):
                cand = cur_p / fn
                if cand.is_symlink():
                    try:
                        if not cand.resolve().is_relative_to(self.root):
                            continue
                    except OSError:
                        continue
                out.append(cand)
        return out

    def _is_trackable_file(self, item: Path) -> bool:
        return not _is_secret_env_name(item.name) and (
            item.suffix.lower() in self.TRACKED_EXTENSIONS or item.name in (".gitignore", ".env.example", "Dockerfile", "Makefile")
        )

    def create_checkpoint(self, label: str = "auto", paths: Optional[List[Path]] = None) -> Dict[str, Any]:
        safe_label = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in label)[:40]
        cp_id = f"cp_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{safe_label}"
        cp_dir = self.checkpoints_dir / cp_id
        cp_dir.mkdir(parents=True, exist_ok=True)

        candidates = [Path(p).resolve() for p in paths] if paths is not None else self._iter_workspace_files()
        manifest_files = [
            {"path": str(item.relative_to(self.root)).replace("\\", "/"), "sha256": self._store_blob(item.read_bytes())}
            for item in candidates if item.is_file() and item.is_relative_to(self.root) and self._is_trackable_file(item)
        ]
        manifest: Dict[str, Any] = {
            "checkpoint_id": cp_id, "label": label, "created_at": datetime.now().isoformat(),
            "ledger_seq": len(self._load_ledger()), "file_count": len(manifest_files), "files": manifest_files,
        }
        store = self._get_store()
        if store is not None and hasattr(store, "export_state_snapshot"):
            manifest["db_snapshot"] = store.export_state_snapshot(project_id=SELF_PROJECT_ID if self.root == settings.root.resolve() else None)
        (cp_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        all_cps = sorted([d for d in self.checkpoints_dir.iterdir() if d.is_dir() and (d / "manifest.json").is_file()], key=lambda d: d.stat().st_mtime)
        while len(all_cps) > 15:
            shutil.rmtree(all_cps.pop(0), ignore_errors=True)
        return manifest

    def list_checkpoints(self) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        if not self.checkpoints_dir.is_dir():
            return results
        for cp_dir in sorted(self.checkpoints_dir.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True):
            mf = cp_dir / "manifest.json"
            if mf.is_file():
                with contextlib.suppress(Exception):
                    d = json.loads(mf.read_text(encoding="utf-8"))
                    results.append({"checkpoint_id": d.get("checkpoint_id", cp_dir.name), "label": d.get("label", ""), "created_at": d.get("created_at", ""), "file_count": d.get("file_count", 0)})
        return results

    def _restore_entry_bytes(self, cp_dir: Path, rel_str: str, sha: str) -> Optional[bytes]:
        blob = self._read_blob(sha)
        if blob is not None:
            return blob
        legacy = (cp_dir / "workspace" / Path(rel_str)).resolve()
        return legacy.read_bytes() if (legacy.is_file() and legacy.is_relative_to((cp_dir / "workspace").resolve())) else None

    def rollback_checkpoint(self, checkpoint_id: Optional[str] = None, reason: str = "", force: bool = False) -> Dict[str, Any]:
        if checkpoint_id is None:
            cps = self.list_checkpoints()
            if not cps:
                return {"restored": False, "error": "No checkpoints available to rollback."}
            checkpoint_id = cps[0]["checkpoint_id"]
        cp_dir = self.checkpoints_dir / checkpoint_id
        if not (cp_dir / "manifest.json").is_file():
            return {"restored": False, "error": f"Checkpoint '{checkpoint_id}' not found."}

        manifest = json.loads((cp_dir / "manifest.json").read_text(encoding="utf-8"))
        cp_seq = int(manifest.get("ledger_seq", 0))
        post_touches: Dict[str, Dict[str, Any]] = {}
        for item in self._load_ledger():
            rel_p = str(item.get("rel_path") or "") if int(item.get("seq", 0)) > cp_seq else ""
            if not rel_p:
                continue
            if rel_p not in post_touches:
                post_touches[rel_p] = {"existed_before": bool(item.get("existed_before")), "before_sha256": str(item.get("before_sha256") or ""), "after_sha256": str(item.get("after_sha256") or "")}
            else:
                post_touches[rel_p]["after_sha256"] = str(item.get("after_sha256") or "")

        restored_files, removed_files, skipped_user_modified = [], [], []
        manifest_map = {str(e["path"]): str(e.get("sha256") or "") for e in manifest.get("files", [])}
        for rel_str, target_sha in manifest_map.items():
            dst = (self.root / Path(rel_str)).resolve()
            if not dst.is_relative_to(self.root) or (dst.is_file() and _sha256_bytes(dst.read_bytes()) == target_sha):
                continue
            orig = self._restore_entry_bytes(cp_dir, rel_str, target_sha)
            if orig is not None:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(orig)
                restored_files.append(rel_str)

        for rel_str, touch in post_touches.items():
            dst = (self.root / Path(rel_str)).resolve()
            if rel_str in manifest_map or not dst.is_relative_to(self.root):
                continue
            if touch["existed_before"] and (orig := self._read_blob(touch["before_sha256"])) is not None:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(orig)
                restored_files.append(rel_str)
            elif not touch["existed_before"] and dst.is_file():
                if not force and touch["after_sha256"] and _sha256_bytes(dst.read_bytes()) != touch["after_sha256"]:
                    skipped_user_modified.append(rel_str)
                else:
                    with contextlib.suppress(Exception):
                        dst.unlink()
                        removed_files.append(rel_str)

        store = self._get_store()
        if "db_snapshot" in manifest and store is not None and hasattr(store, "restore_state_snapshot"):
            store.restore_state_snapshot(manifest["db_snapshot"], rollback_reason=reason or f"Rolled back workspace to checkpoint `{checkpoint_id}` ({manifest.get('label', 'manual')}).")
        return {
            "restored": True, "checkpoint_id": checkpoint_id, "label": manifest.get("label", ""),
            "restored_count": len(restored_files), "restored_files": restored_files,
            "removed_files": removed_files, "skipped_user_modified": skipped_user_modified,
        }

    def _stat_hash(self, item: Path, rel: str) -> Optional[Tuple[int, int, str]]:
        try:
            st = item.stat()
        except OSError:
            return None
        cached = self._ro_stat_cache.get(rel)
        if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size and (self.blobs_dir / cached[2][:2] / cached[2]).is_file():
            return cached
        sha = self._store_blob(item.read_bytes())
        entry = (st.st_mtime_ns, st.st_size, sha)
        self._ro_stat_cache[rel] = entry
        return entry

    def snapshot_py_files(self) -> Dict[Path, str]:
        snap: Dict[Path, str] = {}
        for item in self._iter_workspace_files():
            if item.suffix.lower() == ".py":
                with contextlib.suppress(Exception):
                    snap[item.resolve()] = item.read_text(encoding="utf-8", errors="replace")
        return snap

    def guard_py_files_after_exec(self, pre_snapshot: Dict[Path, str]) -> Optional[str]:
        healed: List[str] = []
        for item in self._iter_workspace_files():
            resolved = item.resolve()
            if item.suffix.lower() != ".py" or (resolved in pre_snapshot and pre_snapshot[resolved] == item.read_text(encoding="utf-8", errors="replace")):
                continue
            ok_syn, syn_err = _check_py_file_syntax(resolved)
            if not ok_syn:
                resolved.write_text(pre_snapshot[resolved], encoding="utf-8") if resolved in pre_snapshot else resolved.unlink(missing_ok=True)
                healed.append(f"{resolved.name} ({syn_err})")
        return f"SYNTAX GUARD REJECTED subprocess file modification: {', '.join(healed)}. Corrupted Python file(s) were automatically restored. Use `edit_file` or `write_file` instead." if healed else None

    def snapshot_py_stats(self) -> Dict[Path, Tuple[int, int, str]]:
        stats: Dict[Path, Tuple[int, int, str]] = {}
        for item in self._iter_workspace_files():
            if item.suffix.lower() == ".py" and (sh := self._stat_hash(item.resolve(), self._rel_posix(item) or item.name)):
                stats[item.resolve()] = sh
        return stats

    def guard_py_stats_after_exec(self, pre_stats: Dict[Path, Tuple[int, int, str]]) -> Optional[str]:
        healed: List[str] = []
        for item in self._iter_workspace_files():
            resolved = item.resolve()
            if item.suffix.lower() != ".py":
                continue
            try:
                st = resolved.stat()
            except OSError:
                continue
            prev = pre_stats.get(resolved)
            if prev is not None and prev[0] == st.st_mtime_ns and prev[1] == st.st_size:
                continue
            ok_syn, syn_err = _check_py_file_syntax(resolved)
            if not ok_syn:
                orig = self._read_blob(prev[2]) if prev is not None else None
                resolved.write_bytes(orig) if orig is not None else resolved.unlink(missing_ok=True)
                healed.append(f"{resolved.name} ({syn_err})")
        return f"SYNTAX GUARD REJECTED subprocess file modification: {', '.join(healed)}. Corrupted Python file(s) were automatically restored. Use `edit_file` or `write_file` instead." if healed else None

    def _iter_git_guard_files(self) -> List[Path]:
        git_dir = self.root / ".git"
        if not git_dir.is_dir():
            return []
        cfg, hooks_dir = git_dir / "config", git_dir / "hooks"
        guarded = [cfg] if (cfg.is_file() and not cfg.is_symlink()) else []
        if hooks_dir.is_dir() and not hooks_dir.is_symlink():
            guarded.extend(hp for hp in sorted(hooks_dir.iterdir()) if hp.is_file() and not hp.is_symlink())
        return guarded

    def snapshot_readonly_tree(self) -> Dict[str, Any]:
        snap: Dict[str, Tuple[int, int, str]] = {}
        for item in [*self._iter_workspace_files(), *self._iter_git_guard_files()]:
            rel = str(item.relative_to(self.root)).replace("\\", "/")
            if rel not in self._RO_SKIP_RELS and not rel.startswith("data/") and item.suffix.lower() not in self._RO_SKIP_SUFFIXES:
                if (sh := self._stat_hash(item, rel)) is not None:
                    snap[rel] = sh
        return snap

    def enforce_readonly_tree(self, baseline: Dict[str, Any]) -> Optional[str]:
        reverted, current_rels = [], set()
        for item in [*self._iter_workspace_files(), *self._iter_git_guard_files()]:
            rel = str(item.relative_to(self.root)).replace("\\", "/")
            if rel in self._RO_SKIP_RELS or rel.startswith("data/") or item.suffix.lower() in self._RO_SKIP_SUFFIXES:
                continue
            current_rels.add(rel)
            if rel not in baseline:
                item.unlink(missing_ok=True)
                reverted.append(f"{rel} (deleted unauthorized file)")
                continue
            base_val = baseline[rel]
            with contextlib.suppress(OSError):
                if isinstance(base_val, bytes):
                    if item.read_bytes() != base_val:
                        item.write_bytes(base_val)
                        reverted.append(f"{rel} (restored modified file)")
                else:
                    b_mtime, b_size, b_sha = base_val
                    st = item.stat()
                    if (st.st_mtime_ns != b_mtime or st.st_size != b_size) and _sha256_bytes(item.read_bytes()) != b_sha and (orig := self._read_blob(b_sha)) is not None:
                        item.write_bytes(orig)
                        reverted.append(f"{rel} (restored modified file)")
        for rel, base_val in baseline.items():
            if rel not in current_rels and (orig := (base_val if isinstance(base_val, bytes) else self._read_blob(base_val[2]))) is not None:
                dst = self.root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(orig)
                reverted.append(f"{rel} (restored deleted file)")
        if not reverted:
            return None
        why = "permanently (`installed app/ immutability`)" if settings.get_workspace().is_self_project else "while working on an external project"
        return f"SAFETY GUARD REJECTED subprocess mutation: Companion core (`{self.root}`) is mechanically locked READ-ONLY {why}. Reverted {len(reverted)} unauthorized change(s): {', '.join(reverted[:5])}."

    def _run_git(self, args: List[str], timeout: int = 20) -> Tuple[int, str]:
        if shutil.which("git") is None:
            return -1, "git executable not found on PATH"
        try:
            proc = subprocess.run(
                ["git", *args], cwd=str(self.root), env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return proc.returncode, ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        except Exception as exc:
            return -1, f"{type(exc).__name__}: {exc}"

    def create_git_savepoint(self, message: str = "Verified save-point", init_if_needed: bool = False, paths: Optional[List[str]] = None) -> Dict[str, Any]:
        if shutil.which("git") is None:
            return {"committed": False, "reason": "git_unavailable", "summary": "Git not installed; skipped Git save-point."}
        freshly_inited = False
        if not (self.root / ".git").exists():
            if not init_if_needed:
                return {"committed": False, "reason": "not_a_git_repo", "summary": "Workspace is not a Git repository; skipped Git save-point."}
            rc_init, out_init = self._run_git(["init"])
            if rc_init != 0:
                return {"committed": False, "reason": "git_init_failed", "summary": f"Git init failed: {out_init[:160]}"}
            freshly_inited, gi = True, self.root / ".gitignore"
            if not gi.exists():
                gi.write_text("__pycache__/\n*.py[cod]\n.env\nsecrets.env\n*.db\n*.db-wal\n*.db-shm\n.companion_backups/\n", encoding="utf-8")

        stage_rels = list(dict.fromkeys(paths or [str(e.get("rel_path")) for e in self._load_ledger() if e.get("rel_path")]))
        if freshly_inited and not stage_rels:
            stage_rels = [str(p.relative_to(self.root)).replace("\\", "/") for p in self._iter_workspace_files() if self._is_trackable_file(p)]
        valid_rels = [
            r for r in stage_rels
            if not _is_secret_env_name(Path(r).name) and not Path(r).name.lower().endswith((".db", ".db-wal", ".db-shm", ".sqlite")) and ".companion_backups" not in Path(r).parts
        ]
        self._run_git(["add", "--", *valid_rels] if valid_rels else ["add", "-u"])
        rc_cached, cached_out = self._run_git(["diff", "--cached", "--name-only"])
        staged = [ln.strip() for ln in cached_out.splitlines() if ln.strip()] if rc_cached == 0 else []
        if not staged:
            return {"committed": False, "reason": "clean_worktree", "summary": "Git save-point: working tree already clean."}

        clean_msg = " ".join((message or "Verified save-point").splitlines()).strip()[:120]
        full_msg = clean_msg if clean_msg.startswith("[Companion]") else f"[Companion] {clean_msg}"
        rc_c, out_c = self._run_git(["commit", "-m", full_msg])
        if rc_c != 0 and any(k in out_c for k in ("user.email", "user.name", "Please tell me who you are")):
            self._run_git(["config", "user.name", "Companion AI OS"])
            self._run_git(["config", "user.email", "companion@local"])
            rc_c, out_c = self._run_git(["commit", "-m", full_msg])
        if rc_c != 0:
            return {"committed": False, "reason": "commit_failed", "summary": f"Git save-point skipped: {out_c[:180]}"}

        _, sha = self._run_git(["rev-parse", "--short", "HEAD"])
        _, branch = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"])
        sha_s, br_s = sha.strip(), branch.strip() or "HEAD"
        return {"committed": True, "commit": sha_s, "branch": br_s, "files_committed": len(staged), "message": full_msg, "summary": f"Git save-point committed: `{sha_s}` on `{br_s}` ({len(staged)} file(s)) — {full_msg}"}

    def list_git_savepoints(self, limit: int = 10) -> List[Dict[str, str]]:
        git_dir = self.root / ".git"
        if not git_dir.exists():
            return []
        eff_lim = max(1, min(int(limit), 50))
        head_mt = (git_dir / "HEAD").stat().st_mtime if (git_dir / "HEAD").is_file() else 0.0
        logs_mt = (git_dir / "logs" / "HEAD").stat().st_mtime if (git_dir / "logs" / "HEAD").is_file() else 0.0
        cache_key = (head_mt, logs_mt, eff_lim)
        if self._savepoints_cache is not None and self._savepoints_cache[0] == cache_key:
            return list(self._savepoints_cache[1])
        rc, out = self._run_git(["log", f"-n{eff_lim}", "--pretty=format:%h|%cs|%s"])
        if rc != 0 or not out:
            return []
        entries = [{"commit": p[0].strip(), "date": p[1].strip(), "subject": p[2].strip()} for ln in out.splitlines() if len(p := ln.split("|", 2)) == 3]
        self._savepoints_cache = (cache_key, entries)
        return list(entries)


checkpoint_manager = CheckpointManager()
_EXT_CP_CACHE: Dict[str, CheckpointManager] = {}
_EXT_CP_LOCK = threading.Lock()


def get_self_checkpoint_manager() -> CheckpointManager:
    ws = settings.get_workspace()
    target_bdir = (settings.effective_data_dir / "checkpoints" / SELF_PROJECT_ID).resolve()
    if ws.self_root == checkpoint_manager.root and checkpoint_manager.backup_dir == target_bdir:
        return checkpoint_manager
    key = f"self::{ws.self_root}::{target_bdir}"
    with _EXT_CP_LOCK:
        if key not in _EXT_CP_CACHE:
            _EXT_CP_CACHE[key] = CheckpointManager(root=ws.self_root, backup_dir=target_bdir)
        return _EXT_CP_CACHE[key]


def get_active_checkpoint_manager() -> CheckpointManager:
    ws = settings.get_workspace()
    if ws.is_self_project:
        return get_self_checkpoint_manager()
    target_bdir = (settings.effective_data_dir / "checkpoints" / ws.project_id).resolve()
    key = f"{ws.project_root}::{target_bdir}"
    with _EXT_CP_LOCK:
        if key not in _EXT_CP_CACHE:
            _EXT_CP_CACHE[key] = CheckpointManager(root=ws.project_root, backup_dir=target_bdir)
        return _EXT_CP_CACHE[key]


def run_unit_tests(root: Optional[Path] = None, timeout: int = 90, pattern: str = "test_*.py") -> Dict[str, Any]:
    """Run the workspace's unittest suite (or a specific `pattern`) in a clean UTF-8 subprocess."""
    target_root = (root or settings.active_project_root).resolve()
    start_dir = "tests" if (target_root / "tests").is_dir() else ("." if list(target_root.glob("test_*.py")) else "")
    if not start_dir:
        return {"passed": True, "ran": 0, "output": "No tests/ directory found."}
    env = {
        **os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
        "PYTHONPATH": str(target_root), "COMPANION_IN_DOCTOR_TESTS": "1", "COMPANION_TEST_MODE": "1",
        "NO_PROXY": "127.0.0.1,localhost,::1", "no_proxy": "127.0.0.1,localhost,::1",
    }
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", start_dir, "-p", pattern, "-v"],
            cwd=str(target_root), env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        raw_out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        m_ran = re.search(r"Ran\s+(\d+)\s+test", raw_out)
        out = raw_out[-3000:].split("\n", 1)[-1] if len(raw_out) > 3000 else raw_out
        return {"passed": proc.returncode == 0, "ran": int(m_ran.group(1)) if m_ran else 0, "returncode": proc.returncode, "output": out}
    except Exception as exc:
        return {"passed": False, "ran": 0, "returncode": -1, "output": f"Failed to run unit tests: {type(exc).__name__}: {exc}"}


def _inspect_doctor_db(target_root: Path) -> Tuple[str, int, Dict[str, str]]:
    db_file = settings.db_path if target_root == settings.root.resolve() else (target_root / "data" / "conversations.db")
    if db_file.is_file():
        with contextlib.suppress(Exception), contextlib.closing(sqlite3.connect(f"file:{db_file.as_posix()}?mode=ro", uri=True)) as ro_conn:
            prof = dict(ro_conn.execute("SELECT key, value FROM user_profile").fetchall())
            ok_chk = ro_conn.execute("PRAGMA quick_check").fetchone()
            if prof and ok_chk and ok_chk[0] == "ok":
                return db_file.name, len(prof), prof
    mem = MemoryStore(db_path=db_file)
    try:
        prof = mem.get_user_profile()
        return mem.db_path.name, len(prof), prof
    finally:
        mem.close()


def run_system_doctor(root: Optional[Path] = None, run_tests: bool = True) -> Dict[str, Any]:
    """Run 7-stage operational Runtime Health Doctor (zero repo self-policing or builder contamination)."""
    target_root = (root or settings.root).resolve()
    checks: List[Dict[str, Any]] = []

    cfg_issues = settings.validate()
    checks.append({"check": "1. Configuration & .env", "passed": len(cfg_issues) == 0, "details": "Settings valid" if not cfg_issues else "; ".join(cfg_issues)})

    py_files = [p for p in walk_pruned_files(target_root) if p.suffix.lower() == ".py" and ".agents" not in p.parts]
    bad_syntax: List[str] = []
    for pf in py_files:
        ok_syn, syn_err = validate_python_syntax(pf.read_text(encoding="utf-8", errors="replace"), filename=str(pf))
        if not ok_syn:
            bad_syntax.append(f"{pf.relative_to(target_root)}: {syn_err}")
    checks.append({
        "check": "2. Runtime Python Syntax Integrity",
        "passed": len(bad_syntax) == 0,
        "details": f"All {len(py_files)} runtime Python files compiled cleanly." if not bad_syntax else f"Syntax errors: {'; '.join(bad_syntax[:5])}",
    })

    static_dir = target_root / "surfaces" / "static"
    required_static = ("index.html", "styles.css", "app.js")
    missing_static = [fn for fn in required_static if not (static_dir / fn).is_file()] if (target_root / "surfaces").is_dir() else []
    checks.append({
        "check": "3. Web Surface Static Bundle",
        "passed": len(missing_static) == 0,
        "details": "Compiled Web UI assets (`index.html`, `styles.css`, `app.js`) verified." if not missing_static else f"Missing static assets: {missing_static}",
    })

    skills_dir = target_root / "skills"
    leaked_builder = [d.name for d in (skills_dir.iterdir() if skills_dir.is_dir() else ()) if d.is_dir() and d.name in {"anti-sprawl-gate", "product-intent-dogfood", "sdd-compliance", "web-surface-dev", "release-readiness", "devops-ops"}]
    checks.append({
        "check": "4. Runtime Skills & Builder Boundary",
        "passed": len(leaked_builder) == 0,
        "details": "Clean runtime boundary (zero `.agents` builder skills leaked into `skills/`)." if not leaked_builder else f"Builder skill leak in `skills/`: {leaked_builder}",
    })

    domains_root = settings.domains_dir
    bad_domain_dbs: List[str] = []
    domain_db_count = 0
    if domains_root.is_dir():
        for db_p in domains_root.rglob("*.db"):
            domain_db_count += 1
            try:
                with contextlib.closing(sqlite3.connect(f"file:{db_p.as_posix()}?mode=ro", uri=True)) as dconn:
                    qc = dconn.execute("PRAGMA quick_check").fetchone()
                    if not qc or qc[0] != "ok":
                        bad_domain_dbs.append(db_p.name)
            except Exception as exc:
                bad_domain_dbs.append(f"{db_p.name} ({exc})")
    checks.append({
        "check": "5. Domain Capability Pack SQLite Isolation",
        "passed": len(bad_domain_dbs) == 0,
        "details": f"{domain_db_count} domain SQLite DB(s) healthy." if not bad_domain_dbs else f"Corrupt domain DB(s): {bad_domain_dbs}",
    })

    db_name, prof_cnt, profile = _inspect_doctor_db(target_root)
    checks.append({
        "check": "6. Core SQLite WAL Store & Blind Vault",
        "passed": prof_cnt > 0,
        "details": f"DB healthy ({db_name}): {prof_cnt} profile keys, background_autonomy={profile.get('background_autonomy', 'off')}.",
    })

    if run_tests:
        if os.environ.get("COMPANION_IN_DOCTOR_TESTS") == "1":
            checks.append({"check": "7. Automated Unit Test Suite", "passed": True, "details": "Nested test suite invocation skipped inside active unittest run."})
        else:
            test_res = run_unit_tests(target_root)
            test_out = str(test_res["output"])
            ran_lines = [ln.strip() for ln in test_out.splitlines() if ln.strip().startswith("Ran ")]
            details_str = (f"{ran_lines[-1]} — OK" if ran_lines else "All unit tests passed.") if test_res["passed"] else test_out
            checks.append({"check": "7. Automated Unit Test Suite", "passed": bool(test_res["passed"]), "details": details_str})

    all_passed = all(c["passed"] for c in checks)
    lines = [f"=== Companion Runtime Doctor: {'HEALTHY (PASS)' if all_passed else 'ISSUES DETECTED (FAIL)'} ==="]
    lines.extend(f"[{'PASS' if c['passed'] else 'FAIL'}] {c['check']}: {c['details']}" for c in checks)
    return {"healthy": all_passed, "checks": checks, "stages": checks, "summary": "\n".join(lines)}
