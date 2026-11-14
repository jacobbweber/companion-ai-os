import React, { useEffect, useRef, useState } from 'react';
import {
  Activity,
  Cpu,
  KeyRound,
  Lock,
  Plus,
  RefreshCw,
  Server,
  ShieldCheck,
  Trash2,
} from 'lucide-react';
import { callAction } from '../api';
import type {
  MissingVaultSecret,
  RemoteEndpoint,
  VaultAuditEntry,
  VaultSecretMeta,
} from '../types';

interface VaultEndpointsStudioProps {
  onToast: (msg: string) => void;
}

export const VaultEndpointsStudio: React.FC<VaultEndpointsStudioProps> = ({ onToast }) => {
  const [secrets, setSecrets] = useState<VaultSecretMeta[]>([]);
  const [missing, setMissing] = useState<MissingVaultSecret[]>([]);
  const [audit, setAudit] = useState<VaultAuditEntry[]>([]);
  const [endpoints, setEndpoints] = useState<RemoteEndpoint[]>([]);

  // Vault write-only form (uncontrolled password ref so raw secrets never enter React state or DOM HTML attributes)
  const [secName, setSecName] = useState('');
  const [secType, setSecType] = useState('bearer_token');
  const [hasSecVal, setHasSecVal] = useState(false);
  const secInputRef = useRef<HTMLInputElement | null>(null);
  const [secScopes, setSecScopes] = useState('');
  const [secDesc, setSecDesc] = useState('');

  // Endpoint registration form
  const [epId, setEpId] = useState('');
  const [epLabel, setEpLabel] = useState('');
  const [epProto, setEpProto] = useState<'ssh' | 'winrm' | 'http_api'>('ssh');
  const [epHost, setEpHost] = useState('');
  const [epUser, setEpUser] = useState('');
  const [epAuth, setEpAuth] = useState('ssh_agent');
  const [epCred, setEpCred] = useState('');
  const [busyEp, setBusyEp] = useState('');

  const loadAll = async () => {
    const [vRes, aRes, eRes] = await Promise.all([
      callAction<{ secrets?: VaultSecretMeta[]; missing?: MissingVaultSecret[] }>('vault', {
        subaction: 'list',
      }),
      callAction<{ audit?: VaultAuditEntry[] }>('vault', { subaction: 'audit', limit: 12 }),
      callAction<{ endpoints?: RemoteEndpoint[] }>('endpoints', { subaction: 'list' }),
    ]);
    if (vRes.secrets) setSecrets(vRes.secrets);
    if (vRes.missing) setMissing(vRes.missing);
    if (aRes.audit) setAudit(aRes.audit);
    if (eRes.endpoints) setEndpoints(eRes.endpoints);
  };

  useEffect(() => {
    void loadAll();
  }, []);

  const handleStoreSecret = async () => {
    const rawSecret = secInputRef.current?.value || '';
    if (!secName.trim() || !rawSecret) return;
    // Wipe raw secret from DOM input immediately before network call
    if (secInputRef.current) secInputRef.current.value = '';
    setHasSecVal(false);
    const res = await callAction('vault', {
      subaction: 'set',
      name: secName.trim(),
      cred_type: secType,
      value: rawSecret,
      allow_list: secScopes,
      description: secDesc.trim(),
    });
    if (res.ok) {
      setSecName('');
      setSecScopes('');
      setSecDesc('');
      await loadAll();
      onToast(res.summary || 'Stored blind credential in secrets.env.');
    } else {
      onToast(res.error || 'Vault write failed.');
    }
  };

  const handleRegisterEndpoint = async () => {
    if (!epId.trim() || !epHost.trim()) return;
    const res = await callAction('endpoints', {
      subaction: 'register',
      endpoint_id: epId.trim(),
      label: epLabel.trim() || epId.trim(),
      protocol: epProto,
      host: epHost.trim(),
      username: epUser.trim(),
      auth_mode: epAuth,
      credential_ref: epCred.trim(),
    });
    if (res.ok) {
      onToast(res.summary || 'Endpoint registered.');
      setEpId('');
      setEpLabel('');
      setEpHost('');
      setEpUser('');
      setEpCred('');
      void loadAll();
    } else {
      onToast(res.error || 'Could not register endpoint.');
    }
  };

  const probeEndpoint = async (endpointId: string) => {
    setBusyEp(endpointId);
    const res = await callAction('endpoints', { subaction: 'test', endpoint_id: endpointId });
    await loadAll();
    setBusyEp('');
    onToast(res.summary || res.error || `Probed ${endpointId}`);
  };

  const inspectEndpoint = async (endpointId: string) => {
    setBusyEp(endpointId);
    const res = await callAction('endpoints', {
      subaction: 'inspect_host',
      endpoint_id: endpointId,
    });
    await loadAll();
    setBusyEp('');
    onToast(res.summary || res.error || `Profiled ${endpointId}`);
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div className="studio-header">
        <div className="studio-title-group">
          <h1>Blind Credential Vault &amp; Universal Endpoints</h1>
          <p>
            Zero-reveal <code>secrets.env</code> vault (write-only, JIT-scoped, output-scrubbed)
            &amp; SSH / WinRM / HTTP LAN connectors
          </p>
        </div>
        <div className="studio-actions">
          <button type="button" className="btn" onClick={() => void loadAll()}>
            <RefreshCw size={13} /> Refresh Registry
          </button>
        </div>
      </div>

      <div className="studio-scroll">
        {missing.length > 0 && (
          <div className="panel" style={{ borderColor: 'var(--status-warn)' }}>
            <div className="panel-head">
              <span className="panel-title" style={{ color: 'var(--status-warn)' }}>
                Missing Declared Secrets ({missing.length})
              </span>
            </div>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              {missing.map((m) => {
                const neededLabel = Array.isArray(m.needed_by)
                  ? m.needed_by.join(', ')
                  : String(m.needed_by || '');
                return (
                  <button
                    key={m.name}
                    type="button"
                    className="btn"
                    onClick={() => {
                      setSecName(m.name);
                      setSecType(m.cred_type || 'bearer_token');
                      setSecScopes(neededLabel === 'vault_entries' ? '*' : neededLabel);
                    }}
                  >
                    Populate <code className="mono">{m.name}</code> ({neededLabel})
                  </button>
                );
              })}
            </div>
          </div>
        )}

        <div className="grid-2">
          {/* Left: Zero-Reveal Blind Credential Vault */}
          <div>
            <div className="panel">
              <div className="panel-head">
                <span className="panel-title">
                  <Lock size={13} /> Store Secret (Write-Only Zero-Reveal)
                </span>
                <span className="badge ok">NEVER SENT TO LLM</span>
              </div>
              <div className="grid-2" style={{ marginBottom: 8 }}>
                <input
                  className="input"
                  placeholder="Symbolic name (e.g. node-ssh-key)"
                  value={secName}
                  onChange={(e) => setSecName(e.target.value)}
                />
                <select
                  aria-label="Credential type"
                  className="select"
                  value={secType}
                  onChange={(e) => setSecType(e.target.value)}
                >
                  <option value="bearer_token">bearer_token</option>
                  <option value="api_key">api_key</option>
                  <option value="ssh_key">ssh_key</option>
                  <option value="basic_auth">basic_auth</option>
                  <option value="custom">custom</option>
                </select>
              </div>
              <input
                ref={secInputRef}
                type="password"
                autoComplete="new-password"
                className="input"
                style={{ width: '100%', marginBottom: 8 }}
                placeholder="Raw secret value (sealed into 0600 secrets.env, never displayed again)"
                defaultValue=""
                onChange={(e) => setHasSecVal(Boolean(e.target.value))}
              />
              <div className="grid-2" style={{ marginBottom: 8 }}>
                <input
                  className="input"
                  placeholder="Allowed scopes (e.g. endpoint:gpu-node, hosted_mcp)"
                  value={secScopes}
                  onChange={(e) => setSecScopes(e.target.value)}
                />
                <input
                  className="input"
                  placeholder="Description (optional)"
                  value={secDesc}
                  onChange={(e) => setSecDesc(e.target.value)}
                />
              </div>
              <button
                type="button"
                className="btn btn-primary"
                disabled={!secName.trim() || !hasSecVal}
                onClick={() => void handleStoreSecret()}
              >
                <KeyRound size={13} /> Seal Secret in Blind Vault
              </button>
            </div>

            <div className="panel">
              <div className="panel-head">
                <span className="panel-title">
                  <ShieldCheck size={13} /> Vault Metadata Registry ({secrets.length})
                </span>
              </div>
              {secrets.length === 0 ? (
                <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>
                  No Vault entries registered yet.
                </div>
              ) : (
                secrets.map((s) => (
                  <div
                    key={s.name}
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      padding: '8px 10px',
                      border: '1px solid var(--border-hairline)',
                      background: 'var(--bg-elevated)',
                      marginBottom: 6,
                    }}
                  >
                    <div>
                      <code className="mono" style={{ fontWeight: 600 }}>
                        {s.name}
                      </code>{' '}
                      <span className={`badge ${s.is_set ? 'ok' : 'warn'}`}>
                        {s.is_set ? 'SET' : 'MISSING'}
                      </span>{' '}
                      <span className="badge">{s.cred_type}</span>
                      <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>
                        Scopes: {(s.allow_list || []).join(', ') || '(none)'}
                      </div>
                    </div>
                    <button
                      type="button"
                      className="btn btn-danger"
                      onClick={async () => {
                        await callAction('vault', {
                          subaction: 'delete',
                          name: s.name,
                          remove_metadata: true,
                        });
                        void loadAll();
                      }}
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                ))
              )}

              {audit.length > 0 && (
                <details style={{ marginTop: 10 }}>
                  <summary className="mono" style={{ cursor: 'pointer', fontSize: 11 }}>
                    Zero-Reveal Access Audit Log ({audit.length})
                  </summary>
                  <div style={{ marginTop: 6, fontSize: 11.5 }}>
                    {audit.map((a, idx) => (
                      <div key={idx} className="mono" style={{ padding: '3px 0' }}>
                        [{a.created_at}] {a.secret_name} &rarr; {a.outcome} ({a.caller_scope})
                      </div>
                    ))}
                  </div>
                </details>
              )}
            </div>
          </div>

          {/* Right: Universal Remote Endpoint Manager */}
          <div>
            <div className="panel">
              <div className="panel-head">
                <span className="panel-title">
                  <Plus size={13} /> Register Remote Endpoint (SSH / WinRM / HTTP)
                </span>
              </div>
              <div className="grid-2" style={{ marginBottom: 8 }}>
                <input
                  className="input"
                  placeholder="Endpoint ID (e.g. gpu-node)"
                  value={epId}
                  onChange={(e) => setEpId(e.target.value)}
                />
                <input
                  className="input"
                  placeholder="Display Label (e.g. Local GPU Server)"
                  value={epLabel}
                  onChange={(e) => setEpLabel(e.target.value)}
                />
              </div>
              <div className="grid-3" style={{ marginBottom: 8 }}>
                <select
                  aria-label="Endpoint protocol"
                  className="select"
                  value={epProto}
                  onChange={(e) => {
                    const pr = e.target.value as 'ssh' | 'winrm' | 'http_api';
                    setEpProto(pr);
                    setEpAuth(pr === 'ssh' ? 'ssh_agent' : pr === 'winrm' ? 'basic_auth' : 'none');
                  }}
                >
                  <option value="ssh">ssh</option>
                  <option value="winrm">winrm</option>
                  <option value="http_api">http_api</option>
                </select>
                <input
                  className="input"
                  placeholder="Host / Alias / URL (e.g. 192.168.1.100)"
                  value={epHost}
                  onChange={(e) => setEpHost(e.target.value)}
                />
                <input
                  className="input"
                  placeholder="Username (e.g. ubuntu / admin)"
                  value={epUser}
                  onChange={(e) => setEpUser(e.target.value)}
                />
              </div>
              <div className="grid-2" style={{ marginBottom: 8 }}>
                <select
                  aria-label="Auth mode"
                  className="select"
                  value={epAuth}
                  onChange={(e) => setEpAuth(e.target.value)}
                >
                  <option value="ssh_agent">ssh_agent</option>
                  <option value="ssh_key">ssh_key</option>
                  <option value="bearer_token">bearer_token</option>
                  <option value="api_key_header">api_key_header</option>
                  <option value="basic_auth">basic_auth</option>
                  <option value="none">none</option>
                </select>
                <input
                  className="input"
                  placeholder="Vault credential_ref (optional)"
                  value={epCred}
                  onChange={(e) => setEpCred(e.target.value)}
                />
              </div>
              <button
                type="button"
                className="btn btn-primary"
                disabled={!epId.trim() || !epHost.trim()}
                onClick={() => void handleRegisterEndpoint()}
              >
                <Server size={13} /> Save Endpoint
              </button>
            </div>

            <div className="panel">
              <div className="panel-head">
                <span className="panel-title">
                  <Server size={13} /> Registered Endpoints ({endpoints.length})
                </span>
              </div>
              {endpoints.length === 0 ? (
                <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>
                  No endpoints registered yet. Add an SSH server, Windows workstation, or LAN API
                  endpoint above!
                </div>
              ) : (
                endpoints.map((ep) => (
                  <div
                    key={ep.endpoint_id}
                    style={{
                      border: '1px solid var(--border-hairline)',
                      background: 'var(--bg-elevated)',
                      padding: '10px 12px',
                      marginBottom: 8,
                    }}
                  >
                    <div
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                        gap: 8,
                      }}
                    >
                      <div>
                        <strong>{ep.label}</strong>{' '}
                        <code className="mono" style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                          ({ep.endpoint_id})
                        </code>
                      </div>
                      <div style={{ display: 'flex', gap: 6 }}>
                        <span className="badge accent">{ep.protocol}</span>
                        <span
                          className={`badge ${ep.last_probe_status.startsWith('online') ? 'ok' : 'warn'}`}
                        >
                          {ep.last_probe_status}
                          {ep.last_probe_ms ? ` (${ep.last_probe_ms}ms)` : ''}
                        </span>
                      </div>
                    </div>

                    <div className="mono" style={{ fontSize: 11.5, marginTop: 4 }}>
                      Target: {ep.username ? `${ep.username}@` : ''}
                      {ep.host}
                      {ep.host.includes('://') ? '' : `:${ep.port}`} • auth={ep.auth_mode}
                      {ep.credential_ref ? ` (vault:${ep.credential_ref})` : ''}
                    </div>

                    {ep.host_profile?.hostname && (
                      <div
                        style={{
                          marginTop: 6,
                          padding: '6px 8px',
                          background: 'var(--code-bg)',
                          border: '1px solid var(--border-hairline)',
                          fontSize: 11.5,
                        }}
                        className="mono"
                      >
                        Host: {ep.host_profile.hostname} • {ep.host_profile.os_kernel} • CPU:{' '}
                        {ep.host_profile.cpu_cores} • RAM: {ep.host_profile.ram_total}
                        <div style={{ color: 'var(--accent-primary)', marginTop: 2 }}>
                          GPU: {ep.host_profile.gpu_summary}
                        </div>
                      </div>
                    )}

                    <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                      <button
                        type="button"
                        className="btn"
                        disabled={busyEp === ep.endpoint_id}
                        onClick={() => void probeEndpoint(ep.endpoint_id)}
                      >
                        <Activity size={12} /> Probe Latency
                      </button>
                      {ep.protocol !== 'http_api' && (
                        <button
                          type="button"
                          className="btn"
                          disabled={busyEp === ep.endpoint_id}
                          onClick={() => void inspectEndpoint(ep.endpoint_id)}
                        >
                          <Cpu size={12} /> Inspect Hardware
                        </button>
                      )}
                      <button
                        type="button"
                        className="btn btn-danger"
                        onClick={async () => {
                          await callAction('endpoints', {
                            subaction: 'remove',
                            endpoint_id: ep.endpoint_id,
                          });
                          void loadAll();
                        }}
                      >
                        <Trash2 size={12} />
                      </button>
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
