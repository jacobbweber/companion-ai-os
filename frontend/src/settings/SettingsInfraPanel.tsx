import React, { useEffect, useRef, useState } from 'react';
import { Activity, ArchiveRestore, ChevronDown, ChevronRight, Pencil, Plus, RefreshCw, RotateCw, ShieldCheck, Trash2 } from 'lucide-react';
import { apiPost, callAction, fetchTelemetry, reloadCompanionService } from '../api';
import type { FederatedPartner, RemoteEndpoint, SnapshotBackup, TelemetryPayload, VaultAuditEntry, VaultSecretMeta } from '../types';

interface SettingsInfraPanelProps {
  section: 'vault' | 'federation' | 'ops';
  onRefreshStatus: () => Promise<void>;
  onToast: (msg: string) => void;
}

export const SettingsInfraPanel: React.FC<SettingsInfraPanelProps> = ({ section, onRefreshStatus, onToast }) => {
  // Vault & Endpoints
  const [secrets, setSecrets] = useState<VaultSecretMeta[]>([]);
  const [audit, setAudit] = useState<VaultAuditEntry[]>([]);
  const [endpoints, setEndpoints] = useState<RemoteEndpoint[]>([]);
  const [showAddSecret, setShowAddSecret] = useState(false);
  const [showAddEndpoint, setShowAddEndpoint] = useState(false);
  const [showVaultAudit, setShowVaultAudit] = useState(false);
  const [secName, setSecName] = useState('');
  const [secType, setSecType] = useState('bearer_token');
  const [secDesc, setSecDesc] = useState('');
  const secValRef = useRef<HTMLInputElement | null>(null);
  const [epId, setEpId] = useState('');
  const [epProto, setEpProto] = useState<'ssh' | 'winrm' | 'http_api'>('ssh');
  const [epHost, setEpHost] = useState('');
  const [epUser, setEpUser] = useState('');
  const [epPort, setEpPort] = useState('');
  const [epAuth, setEpAuth] = useState('auto');
  const [epCred, setEpCred] = useState('');

  // Federation & Hosted MCP
  const [partners, setPartners] = useState<FederatedPartner[]>([]);
  const [hostedEnabled, setHostedEnabled] = useState(false);
  const [hostedTokenRef, setHostedTokenRef] = useState('hosted-mcp-token');
  const [showAddPartner, setShowAddPartner] = useState(false);
  const [pId, setPId] = useState('');
  const [pUrl, setPUrl] = useState('');
  const [pCred, setPCred] = useState('');

  // Backups, Telemetry & System Doctor
  const [backups, setBackups] = useState<SnapshotBackup[]>([]);
  const [confirmRestoreId, setConfirmRestoreId] = useState('');
  const [doctorReport, setDoctorReport] = useState('');
  const [runningDoctor, setRunningDoctor] = useState(false);
  const [reloading, setReloading] = useState(false);
  const [showTelemetry, setShowTelemetry] = useState(false);
  const [telemetry, setTelemetry] = useState<TelemetryPayload | null>(null);

  const loadSection = async () => {
    if (section === 'vault') {
      const [vRes, aRes, eRes] = await Promise.all([
        callAction<{ secrets?: VaultSecretMeta[] }>('vault', { subaction: 'list' }),
        callAction<{ audit?: VaultAuditEntry[] }>('vault', { subaction: 'audit', limit: 10 }),
        callAction<{ endpoints?: RemoteEndpoint[] }>('endpoints', { subaction: 'list' }),
      ]);
      if (vRes.secrets) setSecrets(vRes.secrets);
      if (aRes.audit) setAudit(aRes.audit);
      if (eRes.endpoints) setEndpoints(eRes.endpoints);
    } else if (section === 'federation') {
      const res = await callAction<{
        partners?: FederatedPartner[];
        hosted_mcp_enabled?: boolean;
        hosted_mcp_token_ref?: string;
      }>('mcp', {
        subaction: 'list',
      });
      if (res.partners) setPartners(res.partners);
      if (typeof res.hosted_mcp_enabled === 'boolean') setHostedEnabled(res.hosted_mcp_enabled);
      if (res.hosted_mcp_token_ref) setHostedTokenRef(res.hosted_mcp_token_ref);
    } else {
      const [bRes, tRes] = await Promise.all([
        callAction<{ snapshots?: SnapshotBackup[] }>('backup', { subaction: 'list' }),
        fetchTelemetry().catch(() => null),
      ]);
      if (bRes.snapshots) setBackups(bRes.snapshots);
      if (tRes && tRes.ok) setTelemetry(tRes);
    }
  };

  useEffect(() => {
    void loadSection();
  }, [section]);

  const storeSecret = async () => {
    const val = secValRef.current?.value || '';
    if (!secName.trim() || !val) return;
    if (secValRef.current) secValRef.current.value = '';
    const res = await callAction('vault', {
      subaction: 'set',
      name: secName.trim(),
      cred_type: secType,
      value: val,
      description: secDesc.trim(),
    });
    if (res.ok) {
      setSecName('');
      setSecDesc('');
      setShowAddSecret(false);
      await loadSection();
      onToast(res.summary || 'Stored credential in Blind Vault.');
    } else {
      onToast(res.error || 'Vault write failed.');
    }
  };

  const addEndpoint = async () => {
    if (!epId.trim() || !epHost.trim()) return;
    const res = await callAction('endpoints', {
      subaction: 'register',
      endpoint_id: epId.trim(),
      protocol: epProto,
      host: epHost.trim(),
      username: epUser.trim(),
      port: epPort.trim() ? Number(epPort.trim()) : 0,
      auth_mode: epAuth === 'auto' ? '' : epAuth,
      credential_ref: epCred.trim(),
      confirmed: true,
    });
    if (res.ok) {
      setEpId('');
      setEpHost('');
      setEpUser('');
      setEpPort('');
      setEpAuth('auto');
      setEpCred('');
      setShowAddEndpoint(false);
      await loadSection();
      onToast(res.summary || 'Registered endpoint.');
    } else {
      onToast(res.error || 'Endpoint registration failed.');
    }
  };

  const editEndpoint = (ep: RemoteEndpoint) => {
    setEpId(ep.endpoint_id);
    setEpProto((ep.protocol === 'winrm' || ep.protocol === 'http_api' ? ep.protocol : 'ssh'));
    setEpHost(ep.host || '');
    setEpUser(ep.username || '');
    setEpPort(ep.port ? String(ep.port) : '');
    setEpAuth(ep.auth_mode || 'auto');
    setEpCred(ep.credential_ref || '');
    setShowAddEndpoint(true);
  };

  if (section === 'vault') {
    return (
      <div>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
          <span className="field-label" style={{ margin: 0 }}>Blind Credential Vault</span>
          <button type="button" className="btn btn-sm" onClick={() => setShowAddSecret((v) => !v)}>
            <Plus size={12} /> Store Secret
          </button>
        </div>

        {showAddSecret && (
          <div className="card-group" style={{ padding: 14 }}>
            <div className="form-grid-2">
              <div>
                <label className="field-label">Symbolic Name</label>
                <input className="input mono" placeholder="e.g. home-assistant-token" value={secName} onChange={(e) => setSecName(e.target.value)} />
              </div>
              <div>
                <label className="field-label">Credential Type</label>
                <select className="select" value={secType} onChange={(e) => setSecType(e.target.value)}>
                  <option value="bearer_token">Bearer Token</option>
                  <option value="api_key">API Key</option>
                  <option value="ssh_key">SSH Private Key</option>
                  <option value="basic_auth">Password / Basic Auth</option>
                </select>
              </div>
              <div>
                <label className="field-label">Secret Value (Write-Only)</label>
                <input ref={secValRef} type="password" className="input" placeholder="Never revealed to LLM or UI" />
              </div>
              <div>
                <label className="field-label">Description</label>
                <input className="input" placeholder="Used for..." value={secDesc} onChange={(e) => setSecDesc(e.target.value)} />
              </div>
            </div>
            <div style={{ marginTop: 10 }}>
              <button type="button" className="btn btn-primary btn-sm" onClick={() => void storeSecret()}>
                Save Blind Secret
              </button>
            </div>
          </div>
        )}

        <div className="card-group">
          {secrets.length === 0 ? (
            <div className="card-row"><span style={{ color: 'var(--text-muted)' }}>No symbolic secrets declared yet.</span></div>
          ) : (
            secrets.map((s) => (
              <div key={s.name} className="card-row">
                <div className="card-row-info">
                  <div className="card-row-title mono">
                    {s.name} <span className="badge" style={{ marginLeft: 6 }}>{s.cred_type}</span>
                  </div>
                  <div className="card-row-desc">{s.description || s.cred_type}</div>
                </div>
                <span className={`badge ${s.is_set ? 'ok' : 'warn'}`}>
                  {s.is_set ? 'SET (Zero-Reveal)' : 'MISSING'}
                </span>
              </div>
            ))
          )}
        </div>

        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
          <span className="field-label" style={{ margin: 0 }}>Remote SSH / WinRM / HTTP Endpoints</span>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => {
              if (!showAddEndpoint) {
                setEpId('');
                setEpHost('');
                setEpUser('');
                setEpPort('');
                setEpAuth('auto');
                setEpCred('');
              }
              setShowAddEndpoint((v) => !v);
            }}
          >
            <Plus size={12} /> Add Endpoint
          </button>
        </div>

        {showAddEndpoint && (
          <div className="card-group" style={{ padding: 14 }}>
            <div className="form-grid-2">
              <div>
                <label className="field-label">Endpoint ID</label>
                <input className="input mono" placeholder="gpu-node" value={epId} onChange={(e) => setEpId(e.target.value)} />
              </div>
              <div>
                <label className="field-label">Protocol</label>
                <select
                  className="select"
                  value={epProto}
                  onChange={(e) => {
                    setEpProto(e.target.value as 'ssh' | 'winrm' | 'http_api');
                    setEpAuth('auto');
                  }}
                >
                  <option value="ssh">SSH</option>
                  <option value="winrm">WinRM</option>
                  <option value="http_api">HTTP API</option>
                </select>
              </div>
              <div>
                <label className="field-label">Host / URL</label>
                <input className="input mono" placeholder="192.168.1.50 or user@host:port" value={epHost} onChange={(e) => setEpHost(e.target.value)} />
              </div>
              {epProto !== 'http_api' && (
                <>
                  <div>
                    <label className="field-label">Username</label>
                    <input className="input mono" placeholder={epProto === 'ssh' ? 'ubuntu' : 'Administrator'} value={epUser} onChange={(e) => setEpUser(e.target.value)} />
                  </div>
                  <div>
                    <label className="field-label">Port (Optional)</label>
                    <input type="number" className="input mono" placeholder={epProto === 'ssh' ? '22' : '5985'} value={epPort} onChange={(e) => setEpPort(e.target.value)} />
                  </div>
                </>
              )}
              <div>
                <label className="field-label">Auth Mode</label>
                <select className="select" value={epAuth} onChange={(e) => setEpAuth(e.target.value)}>
                  <option value="auto">Auto (Match Vault Secret)</option>
                  {epProto === 'ssh' && (
                    <>
                      <option value="ssh_key">SSH Private Key (Vault)</option>
                      <option value="basic_auth">Password / Basic Auth (Vault)</option>
                      <option value="ssh_agent">Local SSH Agent / ~/.ssh</option>
                      <option value="none">None</option>
                    </>
                  )}
                  {epProto === 'winrm' && (
                    <>
                      <option value="basic_auth">Password / Basic Auth (Vault)</option>
                      <option value="none">Current Windows Identity</option>
                    </>
                  )}
                  {epProto === 'http_api' && (
                    <>
                      <option value="bearer_token">Bearer Token (Vault)</option>
                      <option value="api_key_header">API Key Header (Vault)</option>
                      <option value="basic_auth">Basic Auth (Vault)</option>
                      <option value="none">None</option>
                    </>
                  )}
                </select>
              </div>
              <div>
                <label className="field-label">Vault Credential Ref</label>
                <input
                  className="input mono"
                  list="vault-secret-ref-list"
                  placeholder="Optional symbolic ref (e.g. nimopc)"
                  value={epCred}
                  onChange={(e) => setEpCred(e.target.value)}
                />
                <datalist id="vault-secret-ref-list">
                  {secrets.map((s) => (
                    <option key={s.name} value={s.name}>
                      {s.cred_type}
                    </option>
                  ))}
                </datalist>
              </div>
            </div>
            <div style={{ marginTop: 10, display: 'flex', gap: 8 }}>
              <button type="button" className="btn btn-primary btn-sm" onClick={() => void addEndpoint()}>
                Save Endpoint
              </button>
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => setShowAddEndpoint(false)}>
                Cancel
              </button>
            </div>
          </div>
        )}

        <div className="card-group">
          {endpoints.length === 0 ? (
            <div className="card-row"><span style={{ color: 'var(--text-muted)' }}>No remote endpoints registered.</span></div>
          ) : (
            endpoints.map((ep) => (
              <div key={ep.endpoint_id} className="card-row">
                <div className="card-row-info">
                  <div className="card-row-title" style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                    <span>{ep.endpoint_id}</span>
                    <span className="badge">{ep.protocol}</span>
                    <span className="badge mono">
                      {ep.auth_mode}{ep.credential_ref ? `:${ep.credential_ref}` : ''}
                    </span>
                    {ep.last_probe_status && ep.last_probe_status !== 'untested' && (
                      <span className={`badge ${ep.last_probe_status.startsWith('online') ? 'ok' : 'warn'}`}>
                        {ep.last_probe_status}
                      </span>
                    )}
                  </div>
                  <div className="card-row-desc mono">
                    {ep.username ? `${ep.username}@` : ''}{ep.host}{ep.port && !ep.host.includes('://') ? `:${ep.port}` : ''}
                  </div>
                </div>
                <div style={{ display: 'inline-flex', gap: 6 }}>
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={() => editEndpoint(ep)}
                    title="Edit endpoint"
                  >
                    <Pencil size={12} /> Edit
                  </button>
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={async () => {
                      const r = await callAction('endpoints', { subaction: 'test', endpoint_id: ep.endpoint_id });
                      await loadSection();
                      onToast(r.summary || r.error || 'Probed endpoint.');
                    }}
                  >
                    <Activity size={12} /> Test
                  </button>
                  <button
                    type="button"
                    className="btn btn-danger btn-sm"
                    onClick={async () => {
                      const r = await callAction('endpoints', { subaction: 'remove', endpoint_id: ep.endpoint_id });
                      await loadSection();
                      onToast(r.ok === false ? r.error || 'Could not remove endpoint.' : r.summary || 'Removed endpoint.');
                    }}
                  >
                    <Trash2 size={12} />
                  </button>
                </div>
              </div>
            ))
          )}
        </div>

        <button type="button" className="btn btn-ghost btn-sm" onClick={() => setShowVaultAudit((v) => !v)}>
          {showVaultAudit ? <ChevronDown size={13} /> : <ChevronRight size={13} />} Recent Vault Audit Log ({audit.length})
        </button>
        {showVaultAudit && audit.length > 0 && (
          <div className="card-group" style={{ marginTop: 8 }}>
            {audit.map((a, i) => (
              <div key={i} className="card-row" style={{ padding: '8px 14px', fontSize: 12 }}>
                <span className="mono">{a.secret_name} ({a.caller_scope})</span>
                <span className="badge">{a.outcome}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }

  if (section === 'federation') {
    return (
      <div>
        <div className="card-group">
          <div className="card-row">
            <div className="card-row-info">
              <div className="card-row-title">Inbound Hosted MCP Server</div>
              <div className="card-row-desc">Allow token-authenticated partner Companions to delegate bounded tasks.</div>
            </div>
            <button
              type="button"
              className={`switch-toggle ${hostedEnabled ? 'on' : ''}`}
              onClick={async () => {
                const res = await callAction('mcp', {
                  subaction: 'configure_hosted',
                  enabled: !hostedEnabled,
                  token_ref: hostedTokenRef,
                  confirmed: true,
                });
                if (res.ok) {
                  setHostedEnabled(!hostedEnabled);
                  await onRefreshStatus();
                  onToast(res.summary || 'Updated Hosted MCP.');
                } else {
                  onToast(res.error || 'Could not configure Hosted MCP.');
                }
              }}
            />
          </div>
          <div className="card-row">
            <div className="card-row-info">
              <div className="card-row-title">Inbound Token Vault Reference</div>
              <div className="card-row-desc">Symbolic Vault secret required on inbound Bearer requests.</div>
            </div>
            <div style={{ display: 'inline-flex', gap: 6 }}>
              <input className="input mono" style={{ width: 180 }} value={hostedTokenRef} onChange={(e) => setHostedTokenRef(e.target.value)} />
              <button
                type="button"
                className="btn btn-sm"
                onClick={async () => {
                  const res = await callAction('mcp', {
                    subaction: 'configure_hosted',
                    enabled: hostedEnabled,
                    token_ref: hostedTokenRef.trim() || 'hosted-mcp-token',
                    confirmed: true,
                  });
                  onToast(res.ok === false ? res.error || 'Could not save token ref.' : res.summary || 'Saved Hosted MCP token reference.');
                }}
              >
                Save
              </button>
            </div>
          </div>
        </div>

        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
          <span className="field-label" style={{ margin: 0 }}>Partner Federation</span>
          <button type="button" className="btn btn-sm" onClick={() => setShowAddPartner((v) => !v)}>
            <Plus size={12} /> Pair Partner
          </button>
        </div>

        {showAddPartner && (
          <div className="card-group" style={{ padding: 14 }}>
            <div className="form-grid-3">
              <input className="input mono" placeholder="partner-id" value={pId} onChange={(e) => setPId(e.target.value)} />
              <input className="input mono" placeholder="http://192.168.1.x:8000/api/mcp" value={pUrl} onChange={(e) => setPUrl(e.target.value)} />
              <input className="input mono" placeholder="Vault token ref" value={pCred} onChange={(e) => setPCred(e.target.value)} />
            </div>
            <div style={{ marginTop: 10 }}>
              <button
                type="button"
                className="btn btn-primary btn-sm"
                onClick={async () => {
                  const r = await callAction('partners', { subaction: 'register', partner_id: pId.trim(), url: pUrl.trim(), credential_ref: pCred.trim() });
                  if (r.ok) {
                    setPId('');
                    setPUrl('');
                    setShowAddPartner(false);
                    await loadSection();
                    onToast(r.summary || 'Partner paired.');
                  } else {
                    onToast(r.error || 'Could not pair partner.');
                  }
                }}
              >
                Register Partner
              </button>
            </div>
          </div>
        )}

        <div className="card-group">
          {partners.length === 0 ? (
            <div className="card-row"><span style={{ color: 'var(--text-muted)' }}>No federated partners paired yet.</span></div>
          ) : (
            partners.map((p) => (
              <div key={p.partner_id} className="card-row">
                <div className="card-row-info">
                  <div className="card-row-title">
                    {p.label} <span className={`badge ${p.online ? 'ok' : ''}`}>{p.online ? `Online (${p.latency_ms}ms)` : 'Unprobed'}</span>
                  </div>
                  <div className="card-row-desc mono">{p.url}</div>
                </div>
                <button
                  type="button"
                  className="btn btn-sm"
                  onClick={async () => {
                    const r = await callAction('partners', { subaction: 'handshake', partner_id: p.partner_id });
                    await loadSection();
                    await onRefreshStatus();
                    onToast(r.summary || r.error || 'Handshake complete.');
                  }}
                >
                  <RefreshCw size={12} /> Handshake
                </button>
              </div>
            ))
          )}
        </div>
      </div>
    );
  }

  return (
    <div>
      <div className="card-group">
        <div className="card-row">
          <div className="card-row-info">
            <div className="card-row-title">7-Stage System Doctor</div>
            <div className="card-row-desc">Verify AST syntax, A-Frame layers, SDD sync, dead-code hygiene, SQLite &amp; unit tests.</div>
          </div>
          <div style={{ display: 'inline-flex', gap: 6, flexWrap: 'wrap' }}>
            <button
              type="button"
              className="btn btn-sm"
              disabled={runningDoctor}
              onClick={async () => {
                setRunningDoctor(true);
                const r = await apiPost<{ healthy?: boolean; summary?: string }>('/api/doctor', { run_tests: false });
                setRunningDoctor(false);
                setDoctorReport(r.summary || '');
                onToast(r.healthy ? 'Fast Doctor: PASS' : 'Doctor found issues.');
              }}
            >
              <ShieldCheck size={12} /> Quick Check
            </button>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={runningDoctor}
              onClick={async () => {
                setRunningDoctor(true);
                const r = await apiPost<{ healthy?: boolean; summary?: string }>('/api/doctor', { run_tests: true });
                setRunningDoctor(false);
                setDoctorReport(r.summary || '');
                onToast(r.healthy ? 'Full 7-Stage Doctor: PASS' : 'Doctor found issues.');
              }}
            >
              {runningDoctor ? 'Running...' : 'Full Doctor + Tests'}
            </button>
          </div>
        </div>

        <div className="card-row">
          <div className="card-row-info">
            <div className="card-row-title">Reload Companion Service</div>
            <div className="card-row-desc">Gracefully restart the Python service and refresh the UI bundle.</div>
          </div>
          <button
            type="button"
            data-testid="system-reload-service-btn"
            className="btn btn-sm"
            disabled={reloading}
            onClick={async () => {
              setReloading(true);
              const r = await reloadCompanionService();
              if (!r.ok) {
                setReloading(false);
                onToast(r.error || 'Reload failed.');
                return;
              }
              window.setTimeout(() => window.location.reload(), 250);
            }}
          >
            <RotateCw size={12} /> {reloading ? 'Reloading...' : 'Reload Service'}
          </button>
        </div>
      </div>

      {doctorReport && (
        <pre className="mono" style={{ padding: 12, borderRadius: 'var(--radius-sm)', background: 'var(--code-bg)', border: '1px solid var(--border-hairline)', fontSize: 11.5, overflowX: 'auto', marginBottom: 14 }}>
          {doctorReport}
        </pre>
      )}

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <span className="field-label" style={{ margin: 0 }}>Secret-Free Snapshot Backups</span>
        <button type="button" className="btn btn-sm" onClick={async () => {
          const r = await callAction('backup', { subaction: 'create', label: 'manual' });
          await loadSection();
          onToast(r.summary || 'Created snapshot backup.');
        }}>
          <Plus size={12} /> Create Snapshot
        </button>
      </div>

      <div className="card-group">
        {backups.length === 0 ? (
          <div className="card-row"><span style={{ color: 'var(--text-muted)' }}>No snapshot backups created yet.</span></div>
        ) : (
          backups.slice(0, 6).map((b) => (
            <div key={b.snapshot_id} className="card-row">
              <div className="card-row-info">
                <div className="card-row-title mono">{b.snapshot_id}</div>
                <div className="card-row-desc">{b.wiki_files} Wiki files • {b.created_at}</div>
              </div>
              {confirmRestoreId === b.snapshot_id ? (
                <div style={{ display: 'inline-flex', gap: 6 }}>
                  <button
                    type="button"
                    className="btn btn-danger btn-sm"
                    onClick={async () => {
                      setConfirmRestoreId('');
                      const r = await callAction('backup', { subaction: 'restore', snapshot_id: b.snapshot_id });
                      await onRefreshStatus();
                      onToast(r.ok === false ? r.error || 'Restore failed.' : r.summary || 'Restored snapshot.');
                    }}
                  >
                    Confirm Restore
                  </button>
                  <button type="button" className="btn btn-ghost btn-sm" onClick={() => setConfirmRestoreId('')}>
                    Cancel
                  </button>
                </div>
              ) : (
                <button type="button" className="btn btn-sm" onClick={() => setConfirmRestoreId(b.snapshot_id)}>
                  <ArchiveRestore size={12} /> Restore
                </button>
              )}
            </div>
          ))
        )}
      </div>

      <button type="button" className="btn btn-ghost btn-sm" onClick={() => setShowTelemetry((v) => !v)}>
        {showTelemetry ? <ChevronDown size={13} /> : <ChevronRight size={13} />} Telemetry, Traces &amp; System Logs ({telemetry?.traces?.length || 0})
      </button>

      {showTelemetry && telemetry && (
        <div className="card-group" style={{ marginTop: 8 }}>
          {telemetry.traces.slice(0, 6).map((tr) => (
            <div key={tr.trace_id} className="card-row" style={{ fontSize: 12 }}>
              <div className="card-row-info">
                <div className="card-row-title">{tr.name || tr.trace_type}</div>
                <div className="card-row-desc mono">{tr.trace_id} • {Math.round(tr.duration_ms)}ms • {tr.span_count} spans</div>
              </div>
              <span className={`badge ${tr.status === 'ok' ? 'ok' : 'warn'}`}>{tr.status}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};
