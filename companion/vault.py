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

import base64
import fnmatch
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from companion.archive_and_approvals import get_approval_queue
from companion.config import is_in_agent_tool_turn, parse_env_file, settings
from companion.disciplines import walk_pruned_files
from companion.memory_store import MemoryStore, get_memory_store, has_memory_store_override
from companion.telemetry import notify_db_closed, redact_secrets, secret_redactor

__all__ = [
    "VALID_CREDENTIAL_TYPES", "VaultManager", "create_snapshot_backup",
    "get_vault_manager", "list_snapshot_backups", "normalize_secret_name", "restore_snapshot_backup",
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
