---
name: remote-endpoints
description: Safely inspect, profile, and interact with remote SSH, Windows WinRM, HTTP API, and Federated Companion endpoints using Blind Credential Vault references.
required_tools: [endpoint_manage, vault_manage, mcp_manage, wiki_manage]
---

# Remote Endpoints & Federated Partner Playbook

## When to Use This Skill
Use this skill whenever the user asks to:
- Connect to, inspect, or run diagnostics on a remote Linux/GPU server (`ssh`), Windows workstation (`winrm`), or homelab REST API (`http_api`).
- Check whether a required Vault credential is set (`vault_manage` -> `check_secret` / `list_missing`) without ever asking the user to paste secrets into chat.
- Pair with, handshake, or delegate a background job to a Federated Partner Companion (such as a remote lab GPU node or secondary workstation) via `mcp_manage`.

## Step-by-Step Procedure
1. **Verify Endpoint & Blind Credential Status First**:
   - Call `endpoint_manage(action="list")` or `mcp_manage(action="list_partners")` to inspect registered targets and their symbolic `credential_ref`.
   - If a `credential_ref` is unset, call `vault_manage(action="check_secret", name=...)` and politely ask the user to store the secret via `/vault set <name> <type> <value>` (never in chat).
2. **Probe Connectivity & Hardware Telemetry**:
   - Run `endpoint_manage(action="test", endpoint_id=...)` or `endpoint_manage(action="inspect_host", endpoint_id=...)` to verify reachability, OS, CPU/RAM, and GPU VRAM.
   - For paired Companion instances, run `mcp_manage(action="handshake_partner", partner_id=...)` to refresh live peer hardware & health telemetry.
3. **Execute Bounded Read-Before-Write Operations**:
   - Prefer `ssh_read_file` or read-only diagnostics before proposing any remote service or package change.
   - Any remote mutation or unapproved scope grant will pause cleanly in the Unified Approvals Queue (`apr_...`) — report that approval ID clearly to the user.
4. **Capture Durable Host Notes in Wiki Inbox**:
   - Save reusable host findings or topology summaries via `wiki_manage(action="capture_inbox", ...)` so future turns ground automatically.
