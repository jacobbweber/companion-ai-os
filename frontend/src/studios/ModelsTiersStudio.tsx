import React, { useEffect, useRef, useState } from 'react';
import {
  Check,
  Cloud,
  Cpu,
  KeyRound,
  Lock,
  Plus,
  RefreshCw,
  RotateCcw,
  Server,
  ShieldAlert,
  ShieldCheck,
  Sliders,
  Trash2,
  Zap,
} from 'lucide-react';
import { callAction } from '../api';
import type {
  LlmProviderEntry,
  ModelMatrixSnapshot,
  ProviderPreset,
  TierRouteEntry,
} from '../types';

interface ModelsTiersStudioProps {
  onRefreshStatus: () => void;
  onToast: (msg: string) => void;
}

interface TierDraft {
  provider_id: string;
  model: string;
  context_window_tokens: number;
  temperature: number;
  max_tokens: number;
  reasoning_effort: string;
  fallback_provider_id: string;
  fallback_model: string;
}

const TIER_META: Record<string, { title: string; hint: string }> = {
  default: {
    title: 'Default (Global Base)',
    hint: 'Primary baseline inherited by unconfigured slots & final failover',
  },
  fast: {
    title: 'Fast / Everyday Chat',
    hint: 'Low-latency conversation, quick tool turns & intake routing',
  },
  coding: {
    title: 'Coding & Engineering',
    hint: 'Code authoring, refactoring & automated verification steps',
  },
  reasoning: {
    title: 'Deep Reasoning & Strategy',
    hint: 'Architecture blueprints, complex tradeoffs & 2-error OODA escalation',
  },
  image: {
    title: 'Image / Vision',
    hint: 'Screenshots, UI inspection, diagrams & photo attachments',
  },
  audio: {
    title: 'Audio Multimodal',
    hint: 'Voice clips & .wav / .mp3 / .m4a / .ogg turn attachments',
  },
  video: {
    title: 'Video Multimodal',
    hint: 'Screen recordings & .mp4 / .webm / .mov turn attachments',
  },
  embedding: {
    title: 'Semantic Embeddings',
    hint: 'Optional dense vector recall (/v1/embeddings or :embedContent)',
  },
};

const QUICK_PRESET_IDS = [
  'ollama',
  'vllm',
  'gemini',
  'anthropic',
  'xai',
  'openai',
  'openrouter',
  'deepseek',
  'groq',
  'custom',
];

const CONTEXT_PRESETS = [
  { label: '32K', value: 32768 },
  { label: '64K', value: 65536 },
  { label: '128K', value: 131072 },
  { label: '200K', value: 200000 },
  { label: '256K', value: 262144 },
  { label: '1M', value: 1048576 },
  { label: '2M', value: 2097152 },
];

export const ModelsTiersStudio: React.FC<ModelsTiersStudioProps> = ({
  onRefreshStatus,
  onToast,
}) => {
  const [matrix, setMatrix] = useState<ModelMatrixSnapshot | null>(null);
  const [drafts, setDrafts] = useState<Record<string, TierDraft>>({});
  const [busyProvider, setBusyProvider] = useState<string>('');
  const [savingTier, setSavingTier] = useState<string>('');
  const [pingingAll, setPingingAll] = useState<boolean>(false);

  // Provider Editor State (raw API key uses uncontrolled ref for strict zero-DOM/state leakage)
  const [provPreset, setProvPreset] = useState<string>('gemini');
  const [provId, setProvId] = useState<string>('gemini');
  const [provLabel, setProvLabel] = useState<string>('Google Gemini (Native v1beta)');
  const [provDialect, setProvDialect] = useState<string>('gemini_native');
  const [provUrl, setProvUrl] = useState<string>(
    'https://generativelanguage.googleapis.com/v1beta'
  );
  const [provCredRef, setProvCredRef] = useState<string>('gemini-api-key');
  const [hasRawKey, setHasRawKey] = useState<boolean>(false);
  const rawKeyRef = useRef<HTMLInputElement | null>(null);

  // Compaction watermarks
  const [trigPct, setTrigPct] = useState<string>('25');
  const [targPct, setTargPct] = useState<string>('10');

  const syncFromSnapshot = (snap: ModelMatrixSnapshot) => {
    setMatrix(snap);
    setTrigPct(String(snap.compaction?.trigger_pct ?? 25));
    setTargPct(String(snap.compaction?.target_pct ?? 10));
    const nextDrafts: Record<string, TierDraft> = {};
    for (const t of snap.tiers || []) {
      nextDrafts[t.tier] = {
        provider_id: t.provider_id,
        model: t.model,
        context_window_tokens: t.context_window_tokens,
        temperature: t.temperature,
        max_tokens: t.max_tokens,
        reasoning_effort: t.reasoning_effort || '',
        fallback_provider_id: t.fallback_provider_id || '',
        fallback_model: t.fallback_model || '',
      };
    }
    setDrafts(nextDrafts);
  };

  const loadMatrix = async () => {
    const res = await callAction<ModelMatrixSnapshot>('models', { subaction: 'status' });
    if (res.ok) {
      syncFromSnapshot(res as unknown as ModelMatrixSnapshot);
    }
  };

  useEffect(() => {
    void loadMatrix();
  }, []);

  const applyPreset = (preset: ProviderPreset) => {
    setProvPreset(preset.id);
    setProvId(preset.id === 'custom' ? 'custom-llm' : preset.id);
    setProvLabel(preset.label);
    setProvDialect(preset.dialect);
    setProvUrl(preset.base_url || 'http://127.0.0.1:8080/v1');
    setProvCredRef(preset.credential_ref || '');
    if (rawKeyRef.current) rawKeyRef.current.value = '';
    setHasRawKey(false);
  };

  const loadProviderIntoForm = (p: LlmProviderEntry) => {
    setProvPreset(p.preset || 'custom');
    setProvId(p.provider_id);
    setProvLabel(p.label);
    setProvDialect(p.dialect);
    setProvUrl(p.base_url);
    setProvCredRef(p.credential_ref || '');
    if (rawKeyRef.current) rawKeyRef.current.value = '';
    setHasRawKey(false);
  };

  const handleSaveProvider = async () => {
    const rawKey = rawKeyRef.current?.value || '';
    if (rawKeyRef.current) rawKeyRef.current.value = '';
    setHasRawKey(false);
    if (!provId.trim() || !provUrl.trim()) return;
    const res = await callAction<ModelMatrixSnapshot>('models', {
      subaction: 'save_provider',
      preset: provPreset,
      provider_id: provId.trim(),
      label: provLabel.trim() || provId.trim(),
      dialect: provDialect,
      base_url: provUrl.trim(),
      credential_ref: provCredRef.trim(),
      ...(rawKey.trim() ? { api_key_value: rawKey.trim() } : {}),
    });
    if (res.ok) {
      syncFromSnapshot(res as unknown as ModelMatrixSnapshot);
      onRefreshStatus();
      onToast(res.summary || `Saved provider ${provId}.`);
    } else {
      onToast(res.error || 'Failed to save provider.');
    }
  };

  const handlePingProvider = async (providerId: string) => {
    setBusyProvider(providerId);
    const res = await callAction<ModelMatrixSnapshot>('models', {
      subaction: 'discover_models',
      provider_id: providerId,
    });
    setBusyProvider('');
    if (res.ok) {
      syncFromSnapshot(res as unknown as ModelMatrixSnapshot);
      onToast(res.summary || `Probed ${providerId}.`);
    } else {
      onToast(res.error || `Probe failed for ${providerId}.`);
    }
  };

  const handlePingAll = async () => {
    setPingingAll(true);
    const res = await callAction<ModelMatrixSnapshot>('models', { subaction: 'check_all' });
    setPingingAll(false);
    if (res.ok) {
      syncFromSnapshot(res as unknown as ModelMatrixSnapshot);
      onToast(res.summary || 'Probed all providers.');
    }
  };

  const handleDeleteProvider = async (providerId: string) => {
    const res = await callAction<ModelMatrixSnapshot>('models', {
      subaction: 'delete_provider',
      provider_id: providerId,
    });
    if (res.ok) {
      syncFromSnapshot(res as unknown as ModelMatrixSnapshot);
      onRefreshStatus();
      onToast(res.summary || `Removed ${providerId}.`);
    } else {
      onToast(res.error || 'Could not remove provider.');
    }
  };

  const handleSetMode = async (mode: string) => {
    const res = await callAction<ModelMatrixSnapshot>('models', {
      subaction: 'set_mode',
      mode,
    });
    if (res.ok) {
      syncFromSnapshot(res as unknown as ModelMatrixSnapshot);
      onRefreshStatus();
      onToast(res.summary || `Routing mode: ${mode}`);
    }
  };

  const handleSetPrivacy = async (privacyMode: string) => {
    const res = await callAction<ModelMatrixSnapshot>('models', {
      subaction: 'set_privacy',
      privacy_mode: privacyMode,
    });
    if (res.ok) {
      syncFromSnapshot(res as unknown as ModelMatrixSnapshot);
      onRefreshStatus();
      onToast(res.summary || `Privacy mode: ${privacyMode}`);
    } else {
      onToast(res.error || 'Failed to update privacy mode.');
    }
  };

  const handleSaveCompaction = async () => {
    const res = await callAction<ModelMatrixSnapshot>('models', {
      subaction: 'set_compaction',
      trigger_pct: Number(trigPct) || 25,
      target_pct: Number(targPct) || 10,
    });
    if (res.ok) {
      syncFromSnapshot(res as unknown as ModelMatrixSnapshot);
      onToast(res.summary || 'Updated context compaction watermarks.');
    }
  };

  const updateDraft = (tier: string, patch: Partial<TierDraft>) => {
    setDrafts((prev) => {
      const cur = prev[tier];
      if (!cur) return prev;
      const next = { ...cur, ...patch };
      if (patch.provider_id && patch.provider_id !== cur.provider_id && matrix) {
        const prov = matrix.providers.find((p) => p.provider_id === patch.provider_id);
        if (prov) {
          const firstModel =
            prov.discovered_models[0]?.id || prov.suggested_models[0] || next.model;
          const firstCtx =
            prov.discovered_models[0]?.context_window ||
            matrix.presets.find((pr) => pr.id === prov.preset)?.default_context ||
            next.context_window_tokens;
          next.model = firstModel;
          if (firstCtx >= 4096) next.context_window_tokens = firstCtx;
        }
      }
      return { ...prev, [tier]: next };
    });
  };

  const isTierDirty = (row: TierRouteEntry): boolean => {
    const d = drafts[row.tier];
    if (!d) return false;
    return (
      d.provider_id !== row.provider_id ||
      d.model !== row.model ||
      Number(d.context_window_tokens) !== Number(row.context_window_tokens) ||
      Math.abs(Number(d.temperature) - Number(row.temperature)) > 0.001 ||
      Number(d.max_tokens) !== Number(row.max_tokens) ||
      (d.reasoning_effort || '') !== (row.reasoning_effort || '') ||
      (d.fallback_provider_id || '') !== (row.fallback_provider_id || '') ||
      (d.fallback_model || '') !== (row.fallback_model || '')
    );
  };

  const handleSaveTier = async (tier: string) => {
    const d = drafts[tier];
    if (!d || !d.model.trim()) return;
    setSavingTier(tier);
    const res = await callAction<ModelMatrixSnapshot>('models', {
      subaction: 'save_tier',
      tier,
      provider_id: d.provider_id,
      model: d.model.trim(),
      context_window_tokens: Number(d.context_window_tokens) || 131072,
      temperature: Number(d.temperature),
      max_tokens: Number(d.max_tokens) || 4096,
      reasoning_effort: d.reasoning_effort,
      fallback_provider_id: d.fallback_provider_id,
      fallback_model: d.fallback_model.trim(),
    });
    setSavingTier('');
    if (res.ok) {
      syncFromSnapshot(res as unknown as ModelMatrixSnapshot);
      onRefreshStatus();
      onToast(res.summary || `Saved ${tier} tier.`);
    } else {
      onToast(res.error || `Failed to save ${tier} tier.`);
    }
  };

  const handleResetTier = async (tier: string) => {
    setSavingTier(tier);
    const res = await callAction<ModelMatrixSnapshot>('models', {
      subaction: 'reset_tier',
      tier,
    });
    setSavingTier('');
    if (res.ok) {
      syncFromSnapshot(res as unknown as ModelMatrixSnapshot);
      onRefreshStatus();
      onToast(res.summary || `Reset ${tier} tier.`);
    }
  };

  const presetsById: Record<string, ProviderPreset> = {};
  for (const pr of matrix?.presets || []) presetsById[pr.id] = pr;
  const providers = matrix?.providers || [];
  const providersById: Record<string, LlmProviderEntry> = {};
  for (const p of providers) providersById[p.provider_id] = p;

  return (
    <div
      style={{
        height: '100%',
        overflowY: 'auto',
        padding: 16,
        display: 'flex',
        flexDirection: 'column',
        gap: 16,
      }}
    >
      {/* 1. Top Control Strip: Routing Mode, Mechanical Privacy Lock & Context Compaction */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))',
          gap: 12,
        }}
      >
        <section className="panel" style={{ padding: 12 }}>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              marginBottom: 8,
            }}
          >
            <span className="panel-title">
              <Zap size={13} /> Active Routing Mode
            </span>
            <span className="hud-chip accent mono">{(matrix?.active_mode || 'auto').toUpperCase()}</span>
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {(['auto', 'fast', 'coding', 'reasoning', 'image', 'audio', 'video'] as const).map(
              (m) => (
                <button
                  key={m}
                  type="button"
                  className={`btn ${matrix?.active_mode === m ? 'btn-primary' : ''}`}
                  style={{ padding: '4px 9px', fontSize: 11 }}
                  onClick={() => void handleSetMode(m)}
                >
                  {m === 'auto' ? 'Auto (OODA)' : m}
                </button>
              )
            )}
          </div>
        </section>

        <section className="panel" style={{ padding: 12 }}>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              marginBottom: 8,
            }}
          >
            <span className="panel-title">
              <Lock size={13} /> Outbound LLM Privacy Lock
            </span>
            <span
              className={`hud-chip mono ${matrix?.privacy_mode === 'local_only' ? 'ok' : 'accent'}`}
            >
              {(matrix?.privacy_mode || 'hybrid').toUpperCase()}
            </span>
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            <button
              type="button"
              className={`btn ${matrix?.privacy_mode === 'local_only' ? 'btn-primary' : ''}`}
              style={{ padding: '4px 9px', fontSize: 11 }}
              onClick={() => void handleSetPrivacy('local_only')}
              title="Air-gap prompts to LAN/loopback GPUs; mechanically blocks all cloud endpoints"
            >
              <ShieldCheck size={12} /> Local LAN Only
            </button>
            <button
              type="button"
              className={`btn ${matrix?.privacy_mode === 'hybrid' ? 'btn-primary' : ''}`}
              style={{ padding: '4px 9px', fontSize: 11 }}
              onClick={() => void handleSetPrivacy('hybrid')}
              title="Local LAN primary with optional cloud burst on configured tiers"
            >
              Hybrid (Local + Cloud)
            </button>
            <button
              type="button"
              className={`btn ${matrix?.privacy_mode === 'cloud_allowed' ? 'btn-primary' : ''}`}
              style={{ padding: '4px 9px', fontSize: 11 }}
              onClick={() => void handleSetPrivacy('cloud_allowed')}
            >
              <Cloud size={12} /> All Providers
            </button>
          </div>
        </section>

        <section className="panel" style={{ padding: 12 }}>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              marginBottom: 8,
            }}
          >
            <span className="panel-title">
              <Sliders size={13} /> Context Compaction Policy
            </span>
            <span className="mono" style={{ fontSize: 11, color: 'var(--text-muted)' }}>
              {trigPct}% &rarr; {targPct}%
            </span>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <label style={{ fontSize: 11, color: 'var(--text-secondary)', flex: 1 }}>
              Trigger %
              <input
                aria-label="Compaction Trigger Percent"
                type="number"
                min={5}
                max={90}
                className="input mono"
                style={{ padding: '4px 7px', marginTop: 2 }}
                value={trigPct}
                onChange={(e) => setTrigPct(e.target.value)}
              />
            </label>
            <label style={{ fontSize: 11, color: 'var(--text-secondary)', flex: 1 }}>
              Target %
              <input
                aria-label="Compaction Target Percent"
                type="number"
                min={3}
                max={75}
                className="input mono"
                style={{ padding: '4px 7px', marginTop: 2 }}
                value={targPct}
                onChange={(e) => setTargPct(e.target.value)}
              />
            </label>
            <button
              type="button"
              className="btn"
              style={{ alignSelf: 'flex-end', padding: '5px 10px' }}
              onClick={() => void handleSaveCompaction()}
            >
              Apply
            </button>
          </div>
        </section>
      </div>

      {/* 2. Provider Registry & 1-Click Local / Frontier Presets */}
      <div className="studio-split" style={{ height: 'auto', padding: 0 }}>
        <section className="panel">
          <div className="panel-header">
            <span className="panel-title">
              <Plus size={14} /> Add or Configure LLM Provider
            </span>
            <span className="hud-chip ok mono">ZERO-REVEAL VAULT</span>
          </div>
          <div className="panel-body" style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            <div>
              <div
                style={{
                  fontSize: 11,
                  color: 'var(--text-muted)',
                  marginBottom: 5,
                  textTransform: 'uppercase',
                  letterSpacing: '0.04em',
                }}
              >
                1-Click Provider Presets
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5 }}>
                {QUICK_PRESET_IDS.map((pid) => {
                  const pr = presetsById[pid];
                  if (!pr) return null;
                  return (
                    <button
                      key={pid}
                      type="button"
                      className={`btn ${provPreset === pid ? 'btn-primary' : ''}`}
                      style={{ padding: '3px 8px', fontSize: 11 }}
                      onClick={() => applyPreset(pr)}
                    >
                      {pid === 'vllm'
                        ? 'vLLM / SGLang'
                        : pid === 'gemini'
                          ? 'Google Gemini'
                          : pid === 'anthropic'
                            ? 'Anthropic Claude'
                            : pid === 'xai'
                              ? 'xAI Grok'
                              : pid === 'openai'
                                ? 'OpenAI'
                                : pid === 'openrouter'
                                  ? 'OpenRouter'
                                  : pid.charAt(0).toUpperCase() + pid.slice(1)}
                    </button>
                  );
                })}
              </div>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1.3fr', gap: 8 }}>
              <input
                className="input mono"
                placeholder="Provider ID (e.g. gemini, ollama)"
                value={provId}
                onChange={(e) => setProvId(e.target.value)}
              />
              <input
                className="input"
                placeholder="Display Label"
                value={provLabel}
                onChange={(e) => setProvLabel(e.target.value)}
              />
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1.1fr 1.9fr', gap: 8 }}>
              <select
                aria-label="Provider Wire Dialect"
                className="select mono"
                value={provDialect}
                onChange={(e) => setProvDialect(e.target.value)}
              >
                <option value="openai_compat">openai_compat (/v1/chat/completions)</option>
                <option value="gemini_native">gemini_native (Google v1beta)</option>
                <option value="anthropic_messages">anthropic_messages (/v1/messages)</option>
              </select>
              <input
                className="input mono"
                placeholder="Base URL (https://... or http://192.168...)"
                value={provUrl}
                onChange={(e) => setProvUrl(e.target.value)}
              />
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1.3fr', gap: 8 }}>
              <div>
                <input
                  list="vault-secret-refs"
                  className="input mono"
                  placeholder="Vault Secret Ref (optional for LAN)"
                  value={provCredRef}
                  onChange={(e) => setProvCredRef(e.target.value)}
                />
                <datalist id="vault-secret-refs">
                  {(matrix?.vault_secrets || []).map((vs) => (
                    <option key={vs.name} value={vs.name}>
                      {vs.is_set ? 'SET' : 'MISSING'}
                    </option>
                  ))}
                </datalist>
              </div>
              <input
                ref={rawKeyRef}
                type="password"
                autoComplete="new-password"
                className="input mono"
                placeholder="Paste API Key -> seals to Blind Vault (optional)"
                onInput={(e) => setHasRawKey(Boolean((e.target as HTMLInputElement).value))}
              />
            </div>

            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
              <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                {hasRawKey
                  ? 'API key will be sealed write-only into secrets.env and cleared from browser memory.'
                  : 'Raw API keys never touch SQLite, snapshots, or UI responses.'}
              </span>
              <button
                type="button"
                className="btn btn-primary"
                disabled={!provId.trim() || !provUrl.trim()}
                onClick={() => void handleSaveProvider()}
              >
                <KeyRound size={13} /> Save Provider
              </button>
            </div>
          </div>
        </section>

        <section className="panel">
          <div className="panel-header">
            <span className="panel-title">
              <Server size={14} /> Registered Providers ({providers.length})
            </span>
            <button
              type="button"
              className="btn"
              style={{ padding: '4px 9px', fontSize: 11 }}
              disabled={pingingAll}
              onClick={() => void handlePingAll()}
            >
              <RefreshCw size={12} /> {pingingAll ? 'Pinging...' : 'Ping & Fetch All'}
            </button>
          </div>
          <div
            className="panel-body"
            style={{ display: 'flex', flexDirection: 'column', gap: 8, maxHeight: 310, overflowY: 'auto' }}
          >
            {providers.map((p) => (
              <div
                key={p.provider_id}
                style={{
                  padding: '9px 11px',
                  background: 'var(--bg-elevated)',
                  border: '1px solid var(--border-subtle)',
                  borderRadius: 'var(--radius-sm)',
                  display: 'flex',
                  flexDirection: 'column',
                  gap: 5,
                }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                    <strong className="mono" style={{ fontSize: 12.5 }}>
                      {p.provider_id}
                    </strong>
                    <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{p.label}</span>
                    <span className={`hud-chip mono ${p.is_local ? 'ok' : 'accent'}`} style={{ fontSize: 10 }}>
                      {p.is_local ? 'LAN / LOCAL' : 'CLOUD'}
                    </span>
                    <span className="hud-chip mono" style={{ fontSize: 10 }}>
                      {p.dialect}
                    </span>
                    {p.credential_ref && (
                      <span
                        className={`hud-chip mono ${p.credential_set ? 'ok' : 'warn'}`}
                        style={{ fontSize: 10 }}
                      >
                        VAULT:{p.credential_ref} ({p.credential_set ? 'SET' : 'MISSING'})
                      </span>
                    )}
                  </div>
                  <div style={{ display: 'flex', gap: 5 }}>
                    <button
                      type="button"
                      className="btn"
                      style={{ padding: '3px 8px', fontSize: 11 }}
                      disabled={busyProvider === p.provider_id}
                      onClick={() => void handlePingProvider(p.provider_id)}
                    >
                      <RefreshCw size={11} />{' '}
                      {busyProvider === p.provider_id ? 'Probing...' : 'Ping & Models'}
                    </button>
                    <button
                      type="button"
                      className="btn"
                      style={{ padding: '3px 8px', fontSize: 11 }}
                      onClick={() => loadProviderIntoForm(p)}
                    >
                      Edit
                    </button>
                    {p.provider_id !== 'local-default' && (
                      <button
                        type="button"
                        className="btn btn-danger"
                        style={{ padding: '3px 7px' }}
                        aria-label={`Remove provider ${p.provider_id}`}
                        onClick={() => void handleDeleteProvider(p.provider_id)}
                      >
                        <Trash2 size={11} />
                      </button>
                    )}
                  </div>
                </div>
                <div className="mono" style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                  {p.base_url}
                  {p.last_health ? ` • ${p.last_health}` : ''}
                  {p.discovered_models.length > 0
                    ? ` • ${p.discovered_models.length} model(s)`
                    : ''}
                </div>
              </div>
            ))}
          </div>
        </section>
      </div>

      {/* 3. Capability Tier Routing Matrix */}
      <section className="panel">
        <div className="panel-header">
          <span className="panel-title">
            <Cpu size={14} /> Capability Tier Routing, Context Window &amp; Fallback Matrix
          </span>
          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
            Hot-reloads immediately without restarting Companion
          </span>
        </div>
        <div className="panel-body" style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {(matrix?.tiers || []).map((row) => {
            const d = drafts[row.tier];
            if (!d) return null;
            const meta = TIER_META[row.tier] || { title: row.tier, hint: '' };
            const selProv = providersById[d.provider_id];
            const modelOptions = selProv?.suggested_models || [];
            const dirty = isTierDirty(row);
            const datalistId = `tier-models-${row.tier}`;

            return (
              <div
                key={row.tier}
                data-testid={`tier-row-${row.tier}`}
                style={{
                  padding: '10px 12px',
                  background: 'var(--bg-elevated)',
                  border: dirty
                    ? '1px solid var(--accent-primary)'
                    : '1px solid var(--border-subtle)',
                  borderRadius: 'var(--radius-sm)',
                  display: 'flex',
                  flexDirection: 'column',
                  gap: 8,
                }}
              >
                <div
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    flexWrap: 'wrap',
                    gap: 8,
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                    <span className="hud-chip accent mono" style={{ fontSize: 11 }}>
                      {row.tier.toUpperCase()}
                    </span>
                    <strong style={{ fontSize: 13 }}>{meta.title}</strong>
                    <span style={{ fontSize: 11.5, color: 'var(--text-muted)' }}>{meta.hint}</span>
                    {row.is_overridden ? (
                      <span className="hud-chip ok mono" style={{ fontSize: 10 }}>
                        CUSTOMIZED
                      </span>
                    ) : (
                      <span className="hud-chip mono" style={{ fontSize: 10 }}>
                        {row.is_dedicated ? '.ENV DEDICATED' : 'INHERITS DEFAULT'}
                      </span>
                    )}
                    {row.privacy_blocked && (
                      <span className="hud-chip warn mono" style={{ fontSize: 10 }}>
                        <ShieldAlert size={11} /> BLOCKED BY LOCAL_ONLY LOCK
                      </span>
                    )}
                  </div>

                  <div style={{ display: 'flex', gap: 6 }}>
                    {row.is_overridden && (
                      <button
                        type="button"
                        className="btn"
                        style={{ padding: '4px 8px', fontSize: 11 }}
                        disabled={savingTier === row.tier}
                        onClick={() => void handleResetTier(row.tier)}
                        title="Reset tier to default inheritance"
                      >
                        <RotateCcw size={11} /> Reset
                      </button>
                    )}
                    <button
                      type="button"
                      className={`btn ${dirty ? 'btn-primary' : ''}`}
                      style={{ padding: '4px 10px', fontSize: 11 }}
                      disabled={savingTier === row.tier || !d.model.trim()}
                      onClick={() => void handleSaveTier(row.tier)}
                    >
                      <Check size={11} /> {savingTier === row.tier ? 'Saving...' : 'Save Tier'}
                    </button>
                  </div>
                </div>

                <div
                  style={{
                    display: 'grid',
                    gridTemplateColumns: 'repeat(auto-fit, minmax(145px, 1fr))',
                    gap: 8,
                    alignItems: 'end',
                  }}
                >
                  <label style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
                    Provider
                    <select
                      aria-label={`${row.tier} provider`}
                      className="select mono"
                      style={{ marginTop: 3, padding: '5px 7px' }}
                      value={d.provider_id}
                      onChange={(e) => updateDraft(row.tier, { provider_id: e.target.value })}
                    >
                      {providers.map((p) => (
                        <option key={p.provider_id} value={p.provider_id}>
                          {p.provider_id} ({p.is_local ? 'LAN' : 'Cloud'})
                        </option>
                      ))}
                    </select>
                  </label>

                  <label style={{ fontSize: 11, color: 'var(--text-secondary)', gridColumn: 'span 2' }}>
                    Model ID {selProv?.discovered_models?.length ? `(${selProv.discovered_models.length} live)` : ''}
                    <input
                      aria-label={`${row.tier} model`}
                      list={datalistId}
                      className="input mono"
                      style={{ marginTop: 3, padding: '5px 8px' }}
                      placeholder="Model ID (pick or type)"
                      value={d.model}
                      onChange={(e) => updateDraft(row.tier, { model: e.target.value })}
                    />
                    <datalist id={datalistId}>
                      {modelOptions.map((m) => {
                        const live = selProv?.discovered_models?.find((dm) => dm.id === m);
                        return (
                          <option key={m} value={m}>
                            {live?.loaded ? 'LOADED IN VRAM' : live ? 'LIVE' : 'PRESET'}
                          </option>
                        );
                      })}
                    </datalist>
                  </label>

                  <label style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
                    Context Window
                    <div style={{ display: 'flex', gap: 4, marginTop: 3 }}>
                      <select
                        aria-label={`${row.tier} context preset`}
                        className="select mono"
                        style={{ padding: '5px 5px', width: 74 }}
                        value={
                          CONTEXT_PRESETS.some((cp) => cp.value === Number(d.context_window_tokens))
                            ? String(d.context_window_tokens)
                            : ''
                        }
                        onChange={(e) => {
                          if (e.target.value) {
                            updateDraft(row.tier, { context_window_tokens: Number(e.target.value) });
                          }
                        }}
                      >
                        <option value="">Custom</option>
                        {CONTEXT_PRESETS.map((cp) => (
                          <option key={cp.value} value={cp.value}>
                            {cp.label}
                          </option>
                        ))}
                      </select>
                      <input
                        aria-label={`${row.tier} context tokens`}
                        type="number"
                        min={4096}
                        step={4096}
                        className="input mono"
                        style={{ padding: '5px 6px' }}
                        value={d.context_window_tokens}
                        onChange={(e) =>
                          updateDraft(row.tier, {
                            context_window_tokens: Number(e.target.value) || 32768,
                          })
                        }
                      />
                    </div>
                  </label>

                  {row.tier !== 'embedding' && (
                    <>
                      <label style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
                        Temp / Max Out
                        <div style={{ display: 'flex', gap: 4, marginTop: 3 }}>
                          <input
                            aria-label={`${row.tier} temperature`}
                            type="number"
                            min={0}
                            max={2}
                            step={0.05}
                            className="input mono"
                            style={{ padding: '5px 6px', width: 64 }}
                            value={d.temperature}
                            onChange={(e) =>
                              updateDraft(row.tier, { temperature: Number(e.target.value) })
                            }
                          />
                          <input
                            aria-label={`${row.tier} max tokens`}
                            type="number"
                            min={256}
                            step={512}
                            className="input mono"
                            style={{ padding: '5px 6px' }}
                            value={d.max_tokens}
                            onChange={(e) =>
                              updateDraft(row.tier, { max_tokens: Number(e.target.value) || 4096 })
                            }
                          />
                        </div>
                      </label>

                      <label style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
                        Reasoning Effort
                        <select
                          aria-label={`${row.tier} reasoning effort`}
                          className="select mono"
                          style={{ marginTop: 3, padding: '5px 7px' }}
                          value={d.reasoning_effort}
                          onChange={(e) =>
                            updateDraft(row.tier, { reasoning_effort: e.target.value })
                          }
                        >
                          <option value="">Provider Default</option>
                          <option value="low">low (1k think)</option>
                          <option value="medium">medium (4k think)</option>
                          <option value="high">high (8k think)</option>
                        </select>
                      </label>

                      <label style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
                        Fallback Route (Optional)
                        <div style={{ display: 'flex', gap: 4, marginTop: 3 }}>
                          <select
                            aria-label={`${row.tier} fallback provider`}
                            className="select mono"
                            style={{ padding: '5px 6px', width: 118, flexShrink: 0 }}
                            value={d.fallback_provider_id}
                            onChange={(e) =>
                              updateDraft(row.tier, { fallback_provider_id: e.target.value })
                            }
                          >
                            <option value="">No Fallback</option>
                            {providers.map((p) => (
                              <option key={p.provider_id} value={p.provider_id}>
                                {p.provider_id}
                              </option>
                            ))}
                          </select>
                          <input
                            aria-label={`${row.tier} fallback model`}
                            className="input mono"
                            style={{ padding: '5px 6px' }}
                            placeholder="Fallback model"
                            disabled={!d.fallback_provider_id}
                            value={d.fallback_model}
                            onChange={(e) =>
                              updateDraft(row.tier, { fallback_model: e.target.value })
                            }
                          />
                        </div>
                      </label>
                    </>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </section>
    </div>
  );
};
