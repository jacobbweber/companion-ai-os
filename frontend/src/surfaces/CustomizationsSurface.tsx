import React, { useEffect, useState } from 'react';
import {
  FileCode2,
  GitBranch,
  PackagePlus,
  Play,
  Plug,
  Plus,
  RefreshCw,
  RotateCcw,
  Trash2,
} from 'lucide-react';
import { callAction } from '../api';
import type {
  AuxInspectorTarget,
  CustomizationsSnapshot,
  DagRunSummary,
  OutboundMcpServer,
  SkillPackSummary,
  VerifiedDagSummary,
} from '../types';

interface CustomizationsSurfaceProps {
  refreshNonce: number;
  onInspect: (target: AuxInspectorTarget) => void;
  onToast: (msg: string) => void;
}

export const CustomizationsSurface: React.FC<CustomizationsSurfaceProps> = ({
  refreshNonce,
  onInspect,
  onToast,
}) => {
  const [tab, setTab] = useState<'skills' | 'dags' | 'mcp'>('skills');
  const [skills, setSkills] = useState<SkillPackSummary[]>([]);
  const [dags, setDags] = useState<VerifiedDagSummary[]>([]);
  const [dagRuns, setDagRuns] = useState<DagRunSummary[]>([]);
  const [mcpServers, setMcpServers] = useState<OutboundMcpServer[]>([]);

  // Quick Add MCP Server form (tucked behind button)
  const [showAddMcp, setShowAddMcp] = useState(false);
  const [mcpId, setMcpId] = useState('');
  const [mcpTransport, setMcpTransport] = useState<'stdio' | 'http'>('stdio');
  const [mcpTarget, setMcpTarget] = useState('');

  const loadAll = async () => {
    const [cRes, mRes] = await Promise.all([
      callAction<CustomizationsSnapshot>('customizations', { subaction: 'list' }),
      callAction<{ servers?: OutboundMcpServer[] }>('mcp', { subaction: 'list' }),
    ]);
    if (cRes.skills) setSkills(cRes.skills);
    if (cRes.dags) setDags(cRes.dags);
    if (cRes.dag_runs) setDagRuns(cRes.dag_runs);
    if (mRes.servers) setMcpServers(mRes.servers);
  };

  useEffect(() => {
    void loadAll();
  }, [refreshNonce]);

  const runDagQuick = async (dag: VerifiedDagSummary) => {
    const res = await callAction('customizations', {
      subaction: 'run_dag',
      dag: dag.name,
      params: {},
    });
    await loadAll();
    onToast(res.summary || res.error || `Executed DAG ${dag.name}.`);
  };

  const resumeRun = async (runId: string) => {
    const res = await callAction('customizations', {
      subaction: 'resume_dag',
      run_id: runId,
    });
    await loadAll();
    onToast(res.summary || res.error || `Resumed ${runId}.`);
  };

  const harvestMcpPack = async (srv: OutboundMcpServer) => {
    const res = await callAction('mcp', {
      subaction: 'harvest_skill',
      server_id: srv.server_id,
    });
    await loadAll();
    onToast(res.summary || res.error || `Harvested ${srv.server_id} into Capability Pack.`);
  };

  const addMcpServer = async () => {
    if (!mcpId.trim() || !mcpTarget.trim()) return;
    const res = await callAction('mcp', {
      subaction: 'register_server',
      server_id: mcpId.trim(),
      transport: mcpTransport,
      command: mcpTransport === 'stdio' ? mcpTarget.trim() : '',
      url: mcpTransport === 'http' ? mcpTarget.trim() : '',
      confirmed: true,
    });
    if (res.ok) {
      setMcpId('');
      setMcpTarget('');
      setShowAddMcp(false);
      await loadAll();
      onToast(res.summary || 'Registered MCP server.');
    } else {
      onToast(res.error || 'Could not register MCP server.');
    }
  };

  return (
    <div className="surface-scroll" data-testid="customizations-surface">
      <div className="surface-container" style={{ maxWidth: 960 }}>
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            gap: 12,
            flexWrap: 'wrap',
            marginBottom: 16,
          }}
        >
          <div>
            <h1 style={{ margin: 0, fontSize: 19, fontWeight: 600 }}>Customizations</h1>
            <p style={{ margin: '3px 0 0', fontSize: 12.5, color: 'var(--text-secondary)' }}>
              Capability Packs, Verified Python DAG workflows, and MCP server harvesting.
            </p>
          </div>

          <div className="segmented-pills">
            <button
              type="button"
              className={`segmented-pill ${tab === 'skills' ? 'active' : ''}`}
              onClick={() => setTab('skills')}
            >
              Skills ({skills.length})
            </button>
            <button
              type="button"
              className={`segmented-pill ${tab === 'dags' ? 'active' : ''}`}
              onClick={() => setTab('dags')}
            >
              Python DAGs ({dags.length})
            </button>
            <button
              type="button"
              className={`segmented-pill ${tab === 'mcp' ? 'active' : ''}`}
              onClick={() => setTab('mcp')}
            >
              MCP Servers ({mcpServers.length})
            </button>
          </div>
        </div>

        {/* Tab 1: Capability Packs */}
        {tab === 'skills' && (
          <div className="card-group">
            {skills.map((sk) => (
              <div
                key={sk.name}
                className="card-row"
                style={{ cursor: 'pointer' }}
                onClick={() => onInspect({ kind: 'skill', skillName: sk.name })}
              >
                <div className="card-row-info">
                  <div className="card-row-title" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span>{sk.name}</span>
                    <span className={`badge ${sk.scope === 'user' ? 'accent' : ''}`}>
                      {sk.scope === 'user' ? 'User Forged' : 'Core'}
                    </span>
                    {sk.dags && <span className="badge ok">DAG: {sk.dags}</span>}
                  </div>
                  <div className="card-row-desc">{sk.description}</div>
                </div>
                <button type="button" className="btn btn-sm">
                  <FileCode2 size={12} /> Inspect
                </button>
              </div>
            ))}
          </div>
        )}

        {/* Tab 2: Verified Python DAGs & Step-Checkpointed Runs */}
        {tab === 'dags' && (
          <>
            <div className="card-group">
              {dags.map((dag) => (
                <div key={dag.name} className="card-row" data-testid={`dag-row-${dag.name}`}>
                  <div className="card-row-info">
                    <div className="card-row-title" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <GitBranch size={14} style={{ color: 'var(--accent-primary)' }} />
                      <span>{dag.name}</span>
                      <span className="badge">{dag.skill}</span>
                    </div>
                    <div className="card-row-desc">
                      {dag.description} • Nodes: <span className="mono">{dag.nodes.join(' -> ')}</span>
                    </div>
                  </div>
                  <div style={{ display: 'inline-flex', gap: 6 }}>
                    <button
                      type="button"
                      className="btn btn-sm"
                      onClick={() => onInspect({ kind: 'dag', dagName: dag.name })}
                    >
                      Run w/ Inputs
                    </button>
                    <button
                      type="button"
                      className="btn btn-primary btn-sm"
                      onClick={() => void runDagQuick(dag)}
                    >
                      <Play size={12} /> Run
                    </button>
                  </div>
                </div>
              ))}
            </div>

            {dagRuns.length > 0 && (
              <>
                <div className="field-label" style={{ marginBottom: 8 }}>
                  Recent Step-Checkpointed DAG Runs
                </div>
                <div className="card-group">
                  {dagRuns.slice(0, 10).map((run) => (
                    <div
                      key={run.run_id}
                      className="card-row"
                      style={{ cursor: 'pointer' }}
                      onClick={() => onInspect({ kind: 'dag_run', runId: run.run_id })}
                    >
                      <div className="card-row-info">
                        <div className="card-row-title">
                          {run.dag_name}{' '}
                          <span
                            className={`badge ${
                              run.status === 'completed'
                                ? 'ok'
                                : run.status === 'waiting_approval'
                                  ? 'warn'
                                  : ''
                            }`}
                          >
                            {run.status}
                          </span>
                        </div>
                        <div className="card-row-desc mono">
                          {run.run_id}
                          {run.paused_node_id ? ` • Paused at node: ${run.paused_node_id}` : ''}
                        </div>
                      </div>
                      {run.status !== 'completed' && (
                        <button
                          type="button"
                          className="btn btn-sm"
                          onClick={(e) => {
                            e.stopPropagation();
                            void resumeRun(run.run_id);
                          }}
                        >
                          <RotateCcw size={12} /> Resume
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              </>
            )}
          </>
        )}

        {/* Tab 3: Outbound MCP Servers + 1-Click Harvester */}
        {tab === 'mcp' && (
          <div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 10 }}>
              <button
                type="button"
                className="btn btn-primary btn-sm"
                onClick={() => setShowAddMcp((v) => !v)}
              >
                <Plus size={12} /> Add MCP Server
              </button>
            </div>

            {showAddMcp && (
              <div className="card-group" style={{ padding: 14 }}>
                <div className="form-grid-mcp">
                  <input className="input mono" placeholder="server-id" value={mcpId} onChange={(e) => setMcpId(e.target.value)} />
                  <select className="select" value={mcpTransport} onChange={(e) => setMcpTransport(e.target.value as 'stdio' | 'http')}>
                    <option value="stdio">stdio</option>
                    <option value="http">http</option>
                  </select>
                  <input
                    className="input mono"
                    placeholder={mcpTransport === 'stdio' ? 'Command (e.g. npx -y @modelcontextprotocol/...)' : 'http://127.0.0.1:8900/mcp'}
                    value={mcpTarget}
                    onChange={(e) => setMcpTarget(e.target.value)}
                  />
                </div>
                <div style={{ marginTop: 10 }}>
                  <button type="button" className="btn btn-primary btn-sm" onClick={() => void addMcpServer()}>
                    Register MCP Server
                  </button>
                </div>
              </div>
            )}

            <div className="card-group">
              {mcpServers.length === 0 ? (
                <div className="card-row">
                  <span style={{ color: 'var(--text-muted)' }}>
                    No outbound MCP servers registered yet — add one or ask Companion to propose a server.
                  </span>
                </div>
              ) : (
                mcpServers.map((srv) => (
                  <div key={srv.server_id} className="card-row">
                    <div className="card-row-info">
                      <div className="card-row-title" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <Plug size={13} />
                        <span>{srv.label || srv.server_id}</span>
                        <span className="badge">{srv.transport}</span>
                        <span className={`badge ${srv.is_approved ? 'ok' : 'warn'}`}>
                          {srv.discovered_tools.length} tool(s)
                        </span>
                      </div>
                      <div className="card-row-desc mono">{srv.command || srv.url}</div>
                    </div>
                    <div style={{ display: 'inline-flex', gap: 6 }}>
                      <button
                        type="button"
                        className="btn btn-sm"
                        onClick={async () => {
                          const r = await callAction('mcp', { subaction: 'discover', server_id: srv.server_id });
                          if (!srv.is_approved) {
                            await callAction('mcp', { subaction: 'approve_server', server_id: srv.server_id });
                          }
                          await loadAll();
                          onToast(r.summary || 'Discovered MCP tools.');
                        }}
                      >
                        <RefreshCw size={12} /> Discover
                      </button>
                      <button
                        type="button"
                        className="btn btn-primary btn-sm"
                        disabled={!srv.is_approved || srv.discovered_tools.length === 0}
                        onClick={() => void harvestMcpPack(srv)}
                        title="Harvest MCP tools into data/skills/<pack>/ with Python DAG"
                      >
                        <PackagePlus size={12} /> Harvest Pack
                      </button>
                      <button
                        type="button"
                        className="btn btn-danger btn-sm"
                        onClick={async () => {
                          await callAction('mcp', { subaction: 'remove_server', server_id: srv.server_id });
                          await loadAll();
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
        )}
      </div>
    </div>
  );
};
