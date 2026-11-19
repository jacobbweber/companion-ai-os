import React, { useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, Plus, RefreshCw, Trash2 } from 'lucide-react';
import { callAction } from '../api';
import type { LlmProviderEntry, ModelMatrixSnapshot, TierRouteEntry } from '../types';

interface SettingsModelsPanelProps {
  onRefreshStatus: () => Promise<void>;
  onToast: (msg: string) => void;
}

export const SettingsModelsPanel: React.FC<SettingsModelsPanelProps> = ({
  onRefreshStatus,
  onToast,
}) => {
  const [matrix, setMatrix] = useState<ModelMatrixSnapshot | null>(null);
  const [showAddProvider, setShowAddProvider] = useState(false);
  const [expandedTier, setExpandedTier] = useState<string>('');
  const [showCompactionAdv, setShowCompactionAdv] = useState(false);

  // Add Provider form (uncontrolled ref for API key — zero React state leakage)
  const [provId, setProvId] = useState('');
  const [provLabel, setProvLabel] = useState('');
  const [provDialect, setProvDialect] = useState('openai_compat');
  const [provUrl, setProvUrl] = useState('http://127.0.0.1:11434');
  const [provCredRef, setProvCredRef] = useState('');
  const apiKeyRef = useRef<HTMLInputElement | null>(null);

  // Compaction state
  const [trigPct, setTrigPct] = useState(75);
  const [targPct, setTargPct] = useState(25);
  const [keepRecent, setKeepRecent] = useState(16);

  const loadMatrix = async () => {
    const res = await callAction<ModelMatrixSnapshot>('models', { subaction: 'matrix' });
    if (res.ok) {
      const snap = res as unknown as ModelMatrixSnapshot;
      setMatrix(snap);
      if (snap.compaction) {
        setTrigPct(snap.compaction.trigger_pct);
        setTargPct(snap.compaction.target_pct);
        setKeepRecent(snap.compaction.keep_recent_messages);
      }
    }
  };

  useEffect(() => {
    void loadMatrix();
  }, []);

  const saveProvider = async () => {
    if (!provId.trim() || !provUrl.trim()) return;
    const rawKey = apiKeyRef.current?.value || '';
    if (apiKeyRef.current) apiKeyRef.current.value = '';
    const res = await callAction('models', {
      subaction: 'save_provider',
      provider_id: provId.trim(),
      label: provLabel.trim() || provId.trim(),
      dialect: provDialect,
      base_url: provUrl.trim(),
      credential_ref: provCredRef.trim(),
      api_key_value: rawKey,
    });
    if (res.ok) {
      setShowAddProvider(false);
      setProvId('');
      setProvLabel('');
      await loadMatrix();
      await onRefreshStatus();
      onToast(res.summary || 'Saved LLM provider.');
    } else {
      onToast(res.error || 'Could not save provider.');
    }
  };

  const probeProvider = async (p: LlmProviderEntry) => {
    const res = await callAction('models', {
      subaction: 'discover_models',
      provider_id: p.provider_id,
    });
    await loadMatrix();
    onToast(res.ok === false ? res.error || `Probe failed for ${p.label}.` : res.summary || `Probed ${p.label}.`);
  };

  const removeProvider = async (providerId: string) => {
    const res = await callAction('models', {
      subaction: 'delete_provider',
      provider_id: providerId,
    });
    await loadMatrix();
    onToast(res.ok === false ? res.error || 'Could not remove provider.' : res.summary || 'Removed provider.');
  };

  const saveTierUpdate = async (t: TierRouteEntry, patch: Partial<TierRouteEntry>) => {
    const res = await callAction('models', {
      subaction: 'save_tier',
      tier: t.tier,
      provider_id: patch.provider_id ?? t.provider_id,
      model: patch.model ?? t.model,
      context_window_tokens: patch.context_window_tokens ?? t.context_window_tokens,
      temperature: patch.temperature ?? t.temperature,
      max_tokens: patch.max_tokens ?? t.max_tokens,
      reasoning_effort: patch.reasoning_effort ?? t.reasoning_effort,
      fallback_provider_id: patch.fallback_provider_id ?? t.fallback_provider_id,
      fallback_model: patch.fallback_model ?? t.fallback_model,
    });
    await loadMatrix();
    await onRefreshStatus();
    onToast(res.ok === false ? res.error || `Could not save ${t.tier} tier.` : res.summary || `Updated ${t.tier} tier.`);
  };

  const saveCompaction = async () => {
    const res = await callAction('models', {
      subaction: 'set_compaction',
      trigger_pct: trigPct,
      target_pct: targPct,
      keep_recent_messages: keepRecent,
    });
    await loadMatrix();
    onToast(res.ok === false ? res.error || 'Could not save compaction policy.' : res.summary || 'Updated context compaction policy.');
  };

  const providers = matrix?.providers || [];
  const tiers = matrix?.tiers || [];

  return (
    <div data-testid="settings-models-panel">
      {/* Registered Providers */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <span className="field-label" style={{ margin: 0 }}>Registered Inference Providers</span>
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => setShowAddProvider((v) => !v)}
        >
          <Plus size={12} /> Add Provider
        </button>
      </div>

      {showAddProvider && (
        <div className="card-group" style={{ padding: 14 }}>
          <div className="form-grid-2">
            <div>
              <label className="field-label">Provider ID</label>
              <input className="input" placeholder="e.g. ollama-lan or gemini" value={provId} onChange={(e) => setProvId(e.target.value)} />
            </div>
            <div>
              <label className="field-label">Display Label</label>
              <input className="input" placeholder="LAN GPU Node" value={provLabel} onChange={(e) => setProvLabel(e.target.value)} />
            </div>
            <div>
              <label className="field-label">Wire Dialect</label>
              <select className="select" value={provDialect} onChange={(e) => setProvDialect(e.target.value)}>
                <option value="openai_compat">OpenAI / vLLM / Ollama Compatible</option>
                <option value="anthropic_messages">Anthropic Messages</option>
                <option value="gemini_native">Google Gemini Native</option>
              </select>
            </div>
            <div>
              <label className="field-label">Base URL</label>
              <input className="input" value={provUrl} onChange={(e) => setProvUrl(e.target.value)} />
            </div>
            <div>
              <label className="field-label">Vault Credential Ref (Optional)</label>
              <input className="input" placeholder="e.g. gemini-api-key" value={provCredRef} onChange={(e) => setProvCredRef(e.target.value)} />
            </div>
            <div>
              <label className="field-label">Write-Only API Key (Stored in Blind Vault)</label>
              <input ref={apiKeyRef} type="password" className="input" placeholder="Never echoed back" />
            </div>
          </div>
          <div style={{ marginTop: 10, display: 'flex', gap: 8 }}>
            <button type="button" className="btn btn-primary btn-sm" onClick={() => void saveProvider()}>
              Save Provider
            </button>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setShowAddProvider(false)}>
              Cancel
            </button>
          </div>
        </div>
      )}

      <div className="card-group">
        {providers.length === 0 ? (
          <div className="card-row">
            <span style={{ color: 'var(--text-muted)' }}>Using default .env inference endpoints.</span>
          </div>
        ) : (
          providers.map((p) => (
            <div key={p.provider_id} className="card-row">
              <div className="card-row-info">
                <div className="card-row-title">
                  {p.label}{' '}
                  <span className={`badge ${p.is_local ? 'ok' : 'accent'}`}>
                    {p.is_local ? 'LAN / Local' : 'Cloud'}
                  </span>
                </div>
                <div className="card-row-desc mono">
                  {p.base_url}
                  {p.last_health ? ` • ${p.last_health}` : ''}
                </div>
              </div>
              <div style={{ display: 'inline-flex', gap: 6 }}>
                <button
                  type="button"
                  className="btn btn-sm"
                  data-testid={`probe-provider-${p.provider_id}`}
                  onClick={() => void probeProvider(p)}
                >
                  <RefreshCw size={12} /> Probe
                </button>
                {p.provider_id !== 'local-default' && (
                  <button type="button" className="btn btn-danger btn-sm" onClick={() => void removeProvider(p.provider_id)}>
                    <Trash2 size={12} />
                  </button>
                )}
              </div>
            </div>
          ))
        )}
      </div>

      {/* Capability Tier Matrix with Progressive Disclosure */}
      <div className="field-label" style={{ marginBottom: 8 }}>Capability Tier Matrix</div>
      <div className="card-group">
        {tiers.map((t) => {
          const isOpen = expandedTier === t.tier;
          const activeProv = providers.find((p) => p.provider_id === t.provider_id);
          const modelSuggestions = Array.from(
            new Set([
              ...(activeProv?.discovered_models || []).map((m) => m.id),
              ...(activeProv?.suggested_models || []),
            ])
          );
          const listId = `models-datalist-${t.tier}`;
          return (
            <div key={t.tier} style={{ borderBottom: '1px solid var(--border-hairline)' }}>
              <div className="card-row" style={{ borderBottom: 'none' }}>
                <div className="card-row-info">
                  <div className="card-row-title" style={{ textTransform: 'capitalize' }}>
                    {t.tier} Tier
                    {t.privacy_blocked && <span className="badge warn" style={{ marginLeft: 6 }}>Privacy Blocked</span>}
                  </div>
                  <div className="card-row-desc mono">
                    {t.provider_id ? `${t.provider_id} / ` : ''}
                    {t.model} • {Math.round(t.context_window_tokens / 1024)}K ctx • temp {t.temperature}
                  </div>
                </div>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  data-testid={`configure-tier-${t.tier}`}
                  onClick={() => setExpandedTier(isOpen ? '' : t.tier)}
                >
                  {isOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />} Configure
                </button>
              </div>

              {isOpen && (
                <div className="form-grid-3" style={{ padding: '0 16px 14px' }}>
                  <div>
                    <label className="field-label">Provider</label>
                    <select
                      className="select"
                      data-testid={`tier-provider-${t.tier}`}
                      value={t.provider_id || 'local-default'}
                      onChange={(e) => void saveTierUpdate(t, { provider_id: e.target.value })}
                    >
                      {providers.map((p) => (
                        <option key={p.provider_id} value={p.provider_id}>
                          {p.label} ({p.provider_id})
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="field-label">Model Name</label>
                    <input
                      className="input mono"
                      data-testid={`tier-model-${t.tier}`}
                      list={listId}
                      key={`${t.tier}_${t.model}`}
                      defaultValue={t.model}
                      onBlur={(e) => {
                        if (e.target.value.trim() && e.target.value.trim() !== t.model) {
                          void saveTierUpdate(t, { model: e.target.value.trim() });
                        }
                      }}
                    />
                    <datalist id={listId}>
                      {modelSuggestions.map((mName) => (
                        <option key={mName} value={mName} />
                      ))}
                    </datalist>
                  </div>
                  <div>
                    <label className="field-label">Context Window</label>
                    <select
                      className="select"
                      value={t.context_window_tokens}
                      onChange={(e) => void saveTierUpdate(t, { context_window_tokens: Number(e.target.value) })}
                    >
                      {[32768, 65536, 131072, 200000, 262144, 1048576].map((cw) => (
                        <option key={cw} value={cw}>{Math.round(cw / 1024)}K tokens</option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="field-label">Temperature</label>
                    <input
                      type="number"
                      step="0.05"
                      min="0"
                      max="2"
                      className="input"
                      key={`${t.tier}_temp_${t.temperature}`}
                      defaultValue={t.temperature}
                      onBlur={(e) => void saveTierUpdate(t, { temperature: Number(e.target.value) })}
                    />
                  </div>
                  <div>
                    <label className="field-label">Reasoning Effort</label>
                    <select
                      className="select"
                      data-testid={`tier-effort-${t.tier}`}
                      value={t.reasoning_effort || ''}
                      onChange={(e) => void saveTierUpdate(t, { reasoning_effort: e.target.value })}
                    >
                      <option value="">Provider Default</option>
                      <option value="low">Low</option>
                      <option value="medium">Medium</option>
                      <option value="high">High</option>
                    </select>
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Advanced Compaction Controls */}
      <button
        type="button"
        className="btn btn-ghost btn-sm"
        data-testid="toggle-compaction-adv"
        onClick={() => setShowCompactionAdv((v) => !v)}
      >
        {showCompactionAdv ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        Advanced Context Compaction Thresholds
      </button>

      {showCompactionAdv && (
        <div className="card-group" style={{ marginTop: 8, padding: 14 }}>
          <div className="form-grid-3">
            <div>
              <label className="field-label">Trigger %</label>
              <input type="number" className="input" value={trigPct} onChange={(e) => setTrigPct(Number(e.target.value))} />
            </div>
            <div>
              <label className="field-label">Target %</label>
              <input type="number" className="input" value={targPct} onChange={(e) => setTargPct(Number(e.target.value))} />
            </div>
            <div>
              <label className="field-label">Keep Recent Turns</label>
              <input type="number" className="input" data-testid="compaction-keep-recent" value={keepRecent} onChange={(e) => setKeepRecent(Number(e.target.value))} />
            </div>
          </div>
          <div style={{ marginTop: 10 }}>
            <button type="button" className="btn btn-primary btn-sm" data-testid="save-compaction-btn" onClick={() => void saveCompaction()}>
              Save Compaction Rules
            </button>
          </div>
        </div>
      )}
    </div>
  );
};
