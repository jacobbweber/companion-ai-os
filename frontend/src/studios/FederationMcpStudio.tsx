import React, { useEffect, useState } from 'react';
import {
  CheckCircle2,
  Globe,
  Link2,
  Plug,
  Radio,
  RefreshCw,
  Send,
  Trash2,
} from 'lucide-react';
import { callAction } from '../api';
import type { FederatedPartner, OutboundMcpServer, WebStatus } from '../types';

interface FederationMcpStudioProps {
  status: WebStatus | null;
  onRefreshStatus: () => void;
  onToast: (msg: string) => void;
}

export const FederationMcpStudio: React.FC<FederationMcpStudioProps> = ({
  status,
  onRefreshStatus,
  onToast,
}) => {
  const [partners, setPartners] = useState<FederatedPartner[]>([]);
  const [servers, setServers] = useState<OutboundMcpServer[]>([]);
  const [hostedEnabled, setHostedEnabled] = useState(false);
  const [hostedTokenRef, setHostedTokenRef] = useState('hosted-mcp-token');
  const [allowWikiRead, setAllowWikiRead] = useState(false);

  // Partner form + Delegation
  const [pId, setPId] = useState('');
  const [pLabel, setPLabel] = useState('');
  const [pUrl, setPUrl] = useState('');
  const [pCred, setPCred] = useState('');
  const [delPartner, setDelPartner] = useState('');
  const [delGoal, setDelGoal] = useState('');
  const [remoteResult, setRemoteResult] = useState('');

  // Outbound MCP server form
  const [sId, setSId] = useState('');
  const [sTransport, setSTransport] = useState<'stdio' | 'http'>('stdio');
  const [sCmdOrUrl, setSCmdOrUrl] = useState('');
  const [sArgs, setSArgs] = useState('');

  const loadAll = async () => {
    const res = await callAction<{
      servers?: OutboundMcpServer[];
      partners?: FederatedPartner[];
      hosted_mcp_enabled?: boolean;
    }>('mcp', { subaction: 'list' });
    if (res.servers) setServers(res.servers);
    if (res.partners) {
      setPartners(res.partners);
      if (!delPartner && res.partners.length > 0) {
        setDelPartner(res.partners[0].partner_id);
      }
    }
    if (typeof res.hosted_mcp_enabled === 'boolean') {
      setHostedEnabled(res.hosted_mcp_enabled);
    }
  };

  useEffect(() => {
    void loadAll();
  }, []);

  const toggleHostedMcp = async (nextEnabled: boolean) => {
    const res = await callAction('mcp', {
      subaction: 'configure_hosted',
      enabled: nextEnabled,
      token_ref: hostedTokenRef,
      allow_wiki_read: allowWikiRead,
    });
    if (res.ok) {
      setHostedEnabled(nextEnabled);
      onRefreshStatus();
      onToast(res.summary || 'Updated Inbound Hosted MCP Order Counter.');
    } else {
      onToast(res.error || 'Configure hosted MCP failed.');
    }
  };

  const addPartner = async () => {
    if (!pId.trim() || !pUrl.trim()) return;
    const res = await callAction('partners', {
      subaction: 'register',
      partner_id: pId.trim(),
      label: pLabel.trim() || pId.trim(),
      url: pUrl.trim(),
      credential_ref: pCred.trim(),
    });
    if (res.ok) {
      onToast(res.summary || 'Partner registered.');
      setPId('');
      setPLabel('');
      setPUrl('');
      setPCred('');
      void loadAll();
      onRefreshStatus();
    } else {
      onToast(res.error || 'Could not register partner.');
    }
  };

  const handshakePartner = async (partnerId: string) => {
    const res = await callAction('partners', { subaction: 'handshake', partner_id: partnerId });
    onToast(res.summary || res.error || `Handshaked ${partnerId}`);
    void loadAll();
    onRefreshStatus();
  };

  const delegateJob = async () => {
    if (!delPartner || !delGoal.trim()) return;
    const res = await callAction<{ job_id?: string }>('partners', {
      subaction: 'delegate',
      partner_id: delPartner,
      title: delGoal.trim().slice(0, 44),
      goal: delGoal.trim(),
    });
    if (res.ok && res.job_id) {
      setRemoteResult(`Dispatched remote job ${res.job_id} on ${delPartner} — polling result...`);
      setDelGoal('');
      const rjid = res.job_id;
      window.setTimeout(async () => {
        const chk = await callAction<{ status?: string; envelope_summary?: string }>('partners', {
          subaction: 'check_job',
          partner_id: delPartner,
          job_id: rjid,
        });
        setRemoteResult(
          `[${chk.status?.toUpperCase() || 'RUNNING'}] ${chk.envelope_summary || rjid}`
        );
      }, 3500);
    } else {
      onToast(res.error || 'Partner delegation failed.');
    }
  };

  const registerMcpServer = async () => {
    if (!sId.trim() || !sCmdOrUrl.trim()) return;
    const payload: Record<string, unknown> = {
      subaction: 'register_server',
      server_id: sId.trim(),
      transport: sTransport,
    };
    if (sTransport === 'stdio') {
      payload.command = sCmdOrUrl.trim();
      payload.args = sArgs
        .split(',')
        .map((x) => x.trim())
        .filter(Boolean);
    } else {
      payload.url = sCmdOrUrl.trim();
    }
    const res = await callAction('mcp', payload);
    if (res.ok) {
      setSId('');
      setSCmdOrUrl('');
      setSArgs('');
      onToast(res.summary || 'Registered outbound MCP server.');
      void loadAll();
    } else {
      onToast(res.error || 'Could not register MCP server.');
    }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div className="studio-header">
        <div className="studio-title-group">
          <h1>Partner Federation &amp; MCP Hub</h1>
          <p>
            Partner-aware Companion mesh (live OS/GPU handshake + job delegation), Inbound Order
            Counter (<code>/api/mcp</code>), and Outbound SHA-256 fingerprinted MCP tools
          </p>
        </div>
        <div className="studio-actions">
          <button type="button" className="btn" onClick={() => void loadAll()}>
            <RefreshCw size={13} /> Refresh Mesh
          </button>
        </div>
      </div>

      <div className="studio-scroll">
        {/* Inbound Order Counter Banner */}
        <div className="panel">
          <div className="panel-head">
            <span className="panel-title">
              <Radio size={13} /> Inbound Hosted MCP &ldquo;Order Counter&rdquo; (
              <code>{status?.lan_url || ''}/api/mcp</code>)
            </span>
            <span className={`badge ${hostedEnabled ? 'ok' : 'warn'}`}>
              {hostedEnabled ? 'ONLINE (TOKEN GATED)' : 'DISABLED BY DEFAULT'}
            </span>
          </div>
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
            <input
              className="input"
              style={{ width: 220 }}
              placeholder="Vault token_ref"
              value={hostedTokenRef}
              onChange={(e) => setHostedTokenRef(e.target.value)}
            />
            <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12 }}>
              <input
                type="checkbox"
                checked={allowWikiRead}
                onChange={(e) => setAllowWikiRead(e.target.checked)}
              />
              Allow passive Wiki read tools
            </label>
            <button
              type="button"
              className={`btn ${hostedEnabled ? 'btn-danger' : 'btn-primary'}`}
              onClick={() => void toggleHostedMcp(!hostedEnabled)}
            >
              {hostedEnabled ? 'Disable Hosted MCP' : 'Enable Hosted MCP'}
            </button>
          </div>
        </div>

        <div className="grid-2">
          {/* Left: Federated Companion Partners */}
          <div>
            <div className="panel">
              <div className="panel-head">
                <span className="panel-title">
                  <Globe size={13} /> Pair Remote Companion Partner
                </span>
              </div>
              <div className="grid-2" style={{ marginBottom: 8 }}>
                <input
                  className="input"
                  placeholder="Partner ID (e.g. lab-companion)"
                  value={pId}
                  onChange={(e) => setPId(e.target.value)}
                />
                <input
                  className="input"
                  placeholder="Label (e.g. Lab GPU Companion)"
                  value={pLabel}
                  onChange={(e) => setPLabel(e.target.value)}
                />
              </div>
              <div className="grid-2" style={{ marginBottom: 8 }}>
                <input
                  className="input"
                  placeholder="URL (http://192.168.1.100:8000/api/mcp)"
                  value={pUrl}
                  onChange={(e) => setPUrl(e.target.value)}
                />
                <input
                  className="input"
                  placeholder="Vault credential_ref"
                  value={pCred}
                  onChange={(e) => setPCred(e.target.value)}
                />
              </div>
              <button
                type="button"
                className="btn btn-primary"
                disabled={!pId.trim() || !pUrl.trim()}
                onClick={() => void addPartner()}
              >
                <Link2 size={13} /> Pair Partner Instance
              </button>
            </div>

            <div className="panel">
              <div className="panel-head">
                <span className="panel-title">Connected Companion Partners ({partners.length})</span>
              </div>
              {partners.length === 0 ? (
                <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>
                  No federated Companion partners paired yet.
                </div>
              ) : (
                partners.map((p) => (
                  <div
                    key={p.partner_id}
                    style={{
                      border: '1px solid var(--border-hairline)',
                      background: 'var(--bg-elevated)',
                      padding: '10px 12px',
                      marginBottom: 8,
                    }}
                  >
                    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
                      <div>
                        <strong>{p.peer_profile?.companion_name || p.label}</strong>{' '}
                        <code className="mono" style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                          ({p.partner_id})
                        </code>
                      </div>
                      <span className={`badge ${p.online ? 'ok' : 'warn'}`}>
                        {p.online ? `ONLINE (${p.latency_ms}ms)` : 'UNVERIFIED'}
                      </span>
                    </div>
                    <div className="mono" style={{ fontSize: 11.5, marginTop: 4 }}>
                      {p.url}
                    </div>
                    {p.peer_profile?.hostname && (
                      <div
                        className="mono"
                        style={{
                          marginTop: 6,
                          padding: '6px 8px',
                          background: 'var(--code-bg)',
                          border: '1px solid var(--border-hairline)',
                          fontSize: 11.5,
                        }}
                      >
                        {p.peer_profile.hostname} • {p.peer_profile.os_platform} • RAM:{' '}
                        {p.peer_profile.ram_gb} GB
                        <div style={{ color: 'var(--accent-primary)' }}>
                          GPU: {p.peer_profile.gpu_summary} • Fast: {p.peer_profile.fast_model}
                        </div>
                      </div>
                    )}
                    <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                      <button
                        type="button"
                        className="btn"
                        onClick={() => void handshakePartner(p.partner_id)}
                      >
                        Handshake &amp; Sync HUD
                      </button>
                      <button
                        type="button"
                        className="btn btn-danger"
                        onClick={async () => {
                          await callAction('partners', {
                            subaction: 'remove',
                            partner_id: p.partner_id,
                          });
                          void loadAll();
                          onRefreshStatus();
                        }}
                      >
                        <Trash2 size={12} />
                      </button>
                    </div>
                  </div>
                ))
              )}

              {partners.length > 0 && (
                <div style={{ marginTop: 12, paddingTop: 10, borderTop: '1px solid var(--border-hairline)' }}>
                  <div className="mono" style={{ fontSize: 11, marginBottom: 6 }}>
                    DELEGATE REMOTE JOB TO PARTNER
                  </div>
                  <div style={{ display: 'flex', gap: 6, marginBottom: 6 }}>
                    <select
                      aria-label="Target partner"
                      className="select"
                      value={delPartner}
                      onChange={(e) => setDelPartner(e.target.value)}
                    >
                      {partners.map((p) => (
                        <option key={p.partner_id} value={p.partner_id}>
                          {p.partner_id}
                        </option>
                      ))}
                    </select>
                    <input
                      className="input"
                      style={{ flex: 1 }}
                      placeholder="Remote job objective..."
                      value={delGoal}
                      onChange={(e) => setDelGoal(e.target.value)}
                    />
                    <button
                      type="button"
                      className="btn btn-primary"
                      onClick={() => void delegateJob()}
                    >
                      <Send size={12} /> Delegate
                    </button>
                  </div>
                  {remoteResult && (
                    <div className="mono" style={{ fontSize: 11.5, color: 'var(--accent-primary)' }}>
                      {remoteResult}
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>

          {/* Right: Outbound MCP Client Servers */}
          <div>
            <div className="panel">
              <div className="panel-head">
                <span className="panel-title">
                  <Plug size={13} /> Register Outbound MCP Server (Discovery &ne; Authorization)
                </span>
              </div>
              <div className="grid-2" style={{ marginBottom: 8 }}>
                <input
                  className="input"
                  placeholder="Server ID (e.g. github-mcp)"
                  value={sId}
                  onChange={(e) => setSId(e.target.value)}
                />
                <select
                  aria-label="MCP transport"
                  className="select"
                  value={sTransport}
                  onChange={(e) => setSTransport(e.target.value as 'stdio' | 'http')}
                >
                  <option value="stdio">stdio (local subprocess)</option>
                  <option value="http">http (Streamable HTTP / SSE)</option>
                </select>
              </div>
              <input
                className="input"
                style={{ width: '100%', marginBottom: 8 }}
                placeholder={
                  sTransport === 'stdio'
                    ? 'Command executable (e.g. npx or python)'
                    : 'MCP HTTP URL (https://...)'
                }
                value={sCmdOrUrl}
                onChange={(e) => setSCmdOrUrl(e.target.value)}
              />
              {sTransport === 'stdio' && (
                <input
                  className="input"
                  style={{ width: '100%', marginBottom: 8 }}
                  placeholder="Arguments comma-separated (optional)"
                  value={sArgs}
                  onChange={(e) => setSArgs(e.target.value)}
                />
              )}
              <button
                type="button"
                className="btn btn-primary"
                disabled={!sId.trim() || !sCmdOrUrl.trim()}
                onClick={() => void registerMcpServer()}
              >
                Register MCP Server
              </button>
            </div>

            <div className="panel">
              <div className="panel-head">
                <span className="panel-title">Outbound MCP Servers ({servers.length})</span>
              </div>
              {servers.length === 0 ? (
                <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>
                  No outbound MCP servers configured.
                </div>
              ) : (
                servers.map((s) => (
                  <div
                    key={s.server_id}
                    style={{
                      border: '1px solid var(--border-hairline)',
                      background: 'var(--bg-elevated)',
                      padding: '10px 12px',
                      marginBottom: 8,
                    }}
                  >
                    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
                      <div>
                        <strong>{s.server_id}</strong>{' '}
                        <span className="badge">{s.transport}</span>
                      </div>
                      <span className={`badge ${s.is_approved ? 'ok' : 'warn'}`}>
                        {s.is_approved ? 'FINGERPRINT APPROVED' : 'AWAITS APPROVAL'}
                      </span>
                    </div>
                    <div className="mono" style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 4 }}>
                      Tools: {s.discovered_tools?.length || 0} • sha256:{' '}
                      {s.tools_fingerprint || 'undiscovered'}
                    </div>
                    <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                      <button
                        type="button"
                        className="btn"
                        onClick={async () => {
                          const r = await callAction('mcp', {
                            subaction: 'discover',
                            server_id: s.server_id,
                          });
                          onToast(r.summary || r.error || 'Discovery complete.');
                          void loadAll();
                        }}
                      >
                        Discover Tools
                      </button>
                      {s.tools_fingerprint && !s.is_approved && (
                        <button
                          type="button"
                          className="btn btn-primary"
                          onClick={async () => {
                            const r = await callAction('mcp', {
                              subaction: 'approve_server',
                              server_id: s.server_id,
                            });
                            onToast(r.summary || 'Approved MCP tool fingerprint.');
                            void loadAll();
                          }}
                        >
                          <CheckCircle2 size={12} /> Approve Fingerprint
                        </button>
                      )}
                      <button
                        type="button"
                        className="btn btn-danger"
                        onClick={async () => {
                          await callAction('mcp', {
                            subaction: 'remove_server',
                            server_id: s.server_id,
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
