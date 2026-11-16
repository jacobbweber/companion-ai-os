import React, { useEffect, useState } from 'react';
import {
  Cpu,
  Folder,
  Globe,
  KeyRound,
  Palette,
  Plus,
  Settings2,
  Sliders,
  Trash2,
  UserCheck,
  X,
} from 'lucide-react';
import { apiPost, callAction } from '../api';
import type {
  ProjectGraphEntry,
  PromptTemplate,
  SettingsSectionId,
  ThemeId,
  WebStatus,
} from '../types';
import { SettingsInfraPanel } from './SettingsInfraPanel';
import { SettingsModelsPanel } from './SettingsModelsPanel';

interface SettingsModalProps {
  initialSection?: SettingsSectionId;
  status: WebStatus | null;
  theme: ThemeId;
  projects: ProjectGraphEntry[];
  onApplyTheme: (t: ThemeId) => void;
  onSelectProject?: (projectId: string) => void;
  onClose: () => void;
  onRefreshStatus: () => Promise<void>;
  onToast: (msg: string) => void;
}

export const THEME_OPTIONS: Array<{ id: ThemeId; label: string; desc: string }> = [
  { id: 'obsidian', label: 'Obsidian Matte', desc: 'Deep OLED near-black canvas (Default)' },
  { id: 'daylight', label: 'Daylight Paper', desc: 'Crisp high-contrast light workspace' },
  { id: 'graphite', label: 'Graphite Minimal', desc: 'Soft neutral slate dark mode' },
  { id: 'storm', label: 'Storm & Safety Orange', desc: 'Charcoal slate with #ff5f00 accents' },
  { id: 'loftra', label: 'Loftra Industrial', desc: 'Mid-tone architectural grey' },
  { id: 'orbital', label: 'Orbital Brutalist', desc: 'Deep space navy & cyan accents' },
];

const NAV_ITEMS: Array<{ id: SettingsSectionId; label: string; icon: React.ReactNode }> = [
  { id: 'general', label: 'General & Autonomy', icon: <Sliders size={14} /> },
  { id: 'appearance', label: 'Appearance', icon: <Palette size={14} /> },
  { id: 'identity', label: 'Identity & Soul', icon: <UserCheck size={14} /> },
  { id: 'models', label: 'Models & Tiers', icon: <Cpu size={14} /> },
  { id: 'vault', label: 'Vault & Endpoints', icon: <KeyRound size={14} /> },
  { id: 'federation', label: 'Federation & MCP', icon: <Globe size={14} /> },
  { id: 'ops', label: 'Backups & Doctor', icon: <Settings2 size={14} /> },
];

export const SettingsModal: React.FC<SettingsModalProps> = ({
  initialSection = 'general',
  status,
  theme,
  projects,
  onApplyTheme,
  onSelectProject,
  onClose,
  onRefreshStatus,
  onToast,
}) => {
  const [section, setSection] = useState<SettingsSectionId>(initialSection);
  const [compName, setCompName] = useState(status?.companion_name || 'Companion');
  const [compSoul, setCompSoul] = useState(status?.companion_soul || '');
  const [compTone, setCompTone] = useState(status?.companion_tone || status?.tone || 'balanced');
  const [uName, setUName] = useState(status?.user_name || 'Partner');
  const [uProf, setUProf] = useState(status?.user_profile || '');

  // Saved Prompts Library state
  const [prompts, setPrompts] = useState<PromptTemplate[]>([]);
  const [showAddPrompt, setShowAddPrompt] = useState(false);
  const [ptName, setPtName] = useState('');
  const [ptBody, setPtBody] = useState('');

  const loadPrompts = async () => {
    const res = await callAction<{ prompts?: PromptTemplate[] }>('prompts', { subaction: 'list' });
    if (res.prompts) setPrompts(res.prompts);
  };

  useEffect(() => {
    if (section === 'identity') void loadPrompts();
  }, [section]);

  const savePromptTpl = async () => {
    if (!ptName.trim() || !ptBody.trim()) return;
    const res = await callAction('prompts', {
      subaction: 'save',
      name: ptName.trim(),
      template: ptBody.trim(),
    });
    if (res.ok) {
      setPtName('');
      setPtBody('');
      setShowAddPrompt(false);
      await loadPrompts();
      onToast(res.summary || 'Saved prompt template.');
    } else {
      onToast(res.error || 'Could not save prompt template.');
    }
  };

  const bgAutonomy = status?.background_autonomy === 'on';
  const selfDev = Boolean(status?.self_dev_enabled);
  const trustMode = status?.trust_mode || 'balanced';
  const privacyMode = status?.privacy_mode || 'local_only';

  const setOperatingLevers = async (nextBg: boolean, nextSelfDev: boolean) => {
    if (nextBg !== bgAutonomy) {
      await callAction('autonomy', { subaction: 'set_autonomy', enabled: nextBg });
    }
    if (nextSelfDev !== selfDev) {
      const r = await callAction('autonomy', { subaction: 'set_self_dev', enabled: nextSelfDev });
      if (!r.ok && r.error) onToast(r.error);
    }
    await onRefreshStatus();
    onToast('Updated operating posture.');
  };

  const saveIdentity = async () => {
    const res = await callAction('identity', {
      subaction: 'save',
      companion_name: compName,
      companion_soul: compSoul,
      companion_tone: compTone,
      user_name: uName,
      user_profile: uProf,
    });
    await apiPost('/api/settings', { tone: compTone });
    await onRefreshStatus();
    onToast(res.summary || 'Saved Companion identity & profile.');
  };

  const sectionMeta: Record<SettingsSectionId, { title: string; sub: string }> = {
    general: {
      title: 'General & Autonomy',
      sub: 'Configure operating levers, execution permissions, and LAN privacy boundaries.',
    },
    appearance: {
      title: 'Appearance',
      sub: 'Select color theme and visual surface contrast.',
    },
    identity: {
      title: 'Identity & Soul',
      sub: 'Customize Companion name, communication ethos, and your operator profile.',
    },
    models: {
      title: 'Models & Tiers',
      sub: 'Manage LLM providers, Blind Vault API bindings, capability tiers, and context compaction.',
    },
    vault: {
      title: 'Blind Credential Vault & Endpoints',
      sub: 'Zero-reveal secret references and remote SSH, WinRM, and HTTP endpoints.',
    },
    federation: {
      title: 'Partner Federation & Hosted MCP',
      sub: 'Configure inbound MCP order counter and paired remote Companion instances.',
    },
    ops: {
      title: 'Backups, Sandboxes & System Doctor',
      sub: 'Secret-free snapshots, isolated Self-Dev clones, 7-stage System Doctor, and service reload.',
    },
  };

  return (
    <div className="modal-backdrop" onClick={onClose} data-testid="settings-modal-backdrop">
      <div className="settings-modal" onClick={(e) => e.stopPropagation()} data-testid="settings-modal">
        {/* Left Navigation Column (Compact Horizontal Pill Strip on Mobile) */}
        <div className="settings-nav-col">
          <div>
            <div className="rail-section-head settings-desktop-only" style={{ paddingTop: 0 }}>
              <span>Settings</span>
            </div>
            <div className="settings-nav-items">
              {NAV_ITEMS.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  data-testid={`settings-tab-${item.id}`}
                  className={`rail-nav-item ${section === item.id ? 'active' : ''}`}
                  onClick={() => setSection(item.id)}
                >
                  <span className="rail-nav-left">
                    {item.icon}
                    <span>{item.label}</span>
                  </span>
                </button>
              ))}
            </div>

            {projects.length > 0 && (
              <div className="settings-desktop-only">
                <div className="rail-divider" />
                <div className="rail-section-head">
                  <span>Projects</span>
                </div>
                {projects.slice(0, 5).map((p) => (
                  <button
                    key={p.project_id}
                    type="button"
                    className="rail-nav-item"
                    style={{ fontSize: 12 }}
                    onClick={() => onSelectProject?.(p.project_id)}
                  >
                    <span className="rail-nav-left">
                      <Folder size={13} />
                      <span>{p.title}</span>
                    </span>
                    <span className="badge">{p.trigger_policy?.mode || 'manual'}</span>
                  </button>
                ))}
              </div>
            )}
          </div>

          <div
            className="settings-desktop-only"
            style={{
              padding: '10px 8px 2px',
              borderTop: '1px solid var(--border-hairline)',
              fontSize: 12,
            }}
          >
            <div style={{ fontWeight: 600 }}>{status?.user_name || 'Partner'}</div>
            <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
              {status?.workspace?.project_id || 'companion-self'}
            </div>
          </div>
        </div>

        {/* Right Content Column */}
        <div className="settings-content-col">
          <div className="settings-content-head">
            <div>
              <h2 style={{ margin: 0, fontSize: 17, fontWeight: 600 }}>
                {sectionMeta[section].title}
              </h2>
              <p style={{ margin: '3px 0 0', fontSize: 12.5, color: 'var(--text-secondary)' }}>
                {sectionMeta[section].sub}
              </p>
            </div>
            <button type="button" className="icon-btn" onClick={onClose} aria-label="Close settings">
              <X size={16} />
            </button>
          </div>

          <div className="settings-content-scroll">
            {section === 'general' && (
              <>
                <div className="field-label" style={{ marginBottom: 8 }}>
                  Operating Posture
                </div>
                <div className="posture-grid-2">
                  <button
                    type="button"
                    className={`posture-choice-card ${!bgAutonomy ? 'selected' : ''}`}
                    onClick={() => void setOperatingLevers(false, selfDev)}
                  >
                    <div style={{ fontWeight: 600, marginBottom: 4 }}>Interactive Partner</div>
                    <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
                      Foreground assistance only. Companion waits quietly until you ask or manually trigger a routine.
                    </div>
                  </button>
                  <button
                    type="button"
                    className={`posture-choice-card ${bgAutonomy ? 'selected' : ''}`}
                    onClick={() => void setOperatingLevers(true, selfDev)}
                  >
                    <div style={{ fontWeight: 600, marginBottom: 4 }}>Autonomous Partner</div>
                    <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
                      Scheduled and windowed On-Idle routines &amp; unblocked goal slices execute automatically when idle.
                    </div>
                  </button>
                </div>

                <div className="card-group">
                  <div className="card-row">
                    <div className="card-row-info">
                      <div className="card-row-title">Background Autonomy</div>
                      <div className="card-row-desc">
                        Allow eligible On-Idle routines and ready Companion goal slices to run during allowed windows.
                      </div>
                    </div>
                    <button
                      type="button"
                      data-testid="toggle-background-autonomy"
                      className={`switch-toggle ${bgAutonomy ? 'on' : ''}`}
                      onClick={() => void setOperatingLevers(!bgAutonomy, selfDev)}
                      aria-label="Toggle Background Autonomy"
                    />
                  </div>

                  <div className="card-row">
                    <div className="card-row-info">
                      <div className="card-row-title">Self-Development Core Unlock</div>
                      <div className="card-row-desc">
                        {status?.self_dev_locked
                          ? `Locked by environment: ${status.self_dev_lock_reason}`
                          : 'When OFF (default), Companion core source is mechanically read-only while data/ and user skills stay writable.'}
                      </div>
                    </div>
                    <button
                      type="button"
                      data-testid="toggle-self-dev"
                      disabled={Boolean(status?.self_dev_locked)}
                      className={`switch-toggle ${selfDev ? 'on' : ''}`}
                      onClick={() => void setOperatingLevers(bgAutonomy, !selfDev)}
                      aria-label="Toggle Self-Dev Mode"
                    />
                  </div>

                  <div className="card-row">
                    <div className="card-row-info">
                      <div className="card-row-title">Trust Policy</div>
                      <div className="card-row-desc">
                        Controls when high-impact tools, endpoints, and routine enables require approval.
                      </div>
                    </div>
                    <select
                      className="select"
                      style={{ width: 150 }}
                      value={trustMode}
                      onChange={async (e) => {
                        await callAction('trust', { subaction: 'set', mode: e.target.value });
                        await onRefreshStatus();
                        onToast(`Trust policy set to ${e.target.value}.`);
                      }}
                    >
                      <option value="strict">Strict</option>
                      <option value="balanced">Balanced</option>
                      <option value="autonomous">Autonomous</option>
                    </select>
                  </div>

                  <div className="card-row">
                    <div className="card-row-info">
                      <div className="card-row-title">Outbound Privacy Lock</div>
                      <div className="card-row-desc">
                        Enforce LAN-only inference or allow configured hybrid and cloud providers.
                      </div>
                    </div>
                    <select
                      className="select"
                      style={{ width: 150 }}
                      value={privacyMode}
                      onChange={async (e) => {
                        await callAction('models', {
                          subaction: 'set_privacy',
                          privacy_mode: e.target.value,
                        });
                        await onRefreshStatus();
                        onToast(`Privacy mode set to ${e.target.value}.`);
                      }}
                    >
                      <option value="local_only">Local Only</option>
                      <option value="hybrid">Hybrid</option>
                      <option value="cloud_allowed">Cloud Allowed</option>
                    </select>
                  </div>
                </div>
              </>
            )}

            {section === 'appearance' && (
              <div className="posture-grid-2">
                {THEME_OPTIONS.map((opt) => (
                  <button
                    key={opt.id}
                    type="button"
                    data-testid={`theme-card-${opt.id}`}
                    className={`posture-choice-card ${theme === opt.id ? 'selected' : ''}`}
                    onClick={() => onApplyTheme(opt.id)}
                  >
                    <div style={{ fontWeight: 600, marginBottom: 3 }}>{opt.label}</div>
                    <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{opt.desc}</div>
                  </button>
                ))}
              </div>
            )}

            {section === 'identity' && (
              <div>
                <div className="card-group" style={{ padding: 16 }}>
                  <div style={{ display: 'grid', gap: 12 }}>
                    <div className="form-grid-2">
                      <div>
                        <label className="field-label">Companion Name</label>
                        <input className="input" value={compName} onChange={(e) => setCompName(e.target.value)} />
                      </div>
                      <div>
                        <label className="field-label">Communication Tone</label>
                        <select className="select" value={compTone} onChange={(e) => setCompTone(e.target.value)}>
                          <option value="concise">Concise</option>
                          <option value="balanced">Balanced</option>
                          <option value="elaborate">Elaborate</option>
                        </select>
                      </div>
                    </div>
                    <div>
                      <label className="field-label">Companion Soul &amp; Ethos</label>
                      <textarea className="textarea" rows={2} value={compSoul} onChange={(e) => setCompSoul(e.target.value)} />
                    </div>
                    <div>
                      <label className="field-label">Your Name</label>
                      <input className="input" value={uName} onChange={(e) => setUName(e.target.value)} />
                    </div>
                    <div>
                      <label className="field-label">Your Profile &amp; Working Style</label>
                      <textarea className="textarea" rows={2} value={uProf} onChange={(e) => setUProf(e.target.value)} />
                    </div>
                    <div>
                      <button type="button" className="btn btn-primary" onClick={() => void saveIdentity()}>
                        Save Identity
                      </button>
                    </div>
                  </div>
                </div>

                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                  <span className="field-label" style={{ margin: 0 }}>Saved Prompts Library (Composer + Menu)</span>
                  <button type="button" className="btn btn-sm" onClick={() => setShowAddPrompt((v) => !v)}>
                    <Plus size={12} /> New Prompt
                  </button>
                </div>

                {showAddPrompt && (
                  <div className="card-group" style={{ padding: 14, display: 'grid', gap: 8 }}>
                    <input className="input mono" placeholder="Prompt slug (e.g. weekly-review)" value={ptName} onChange={(e) => setPtName(e.target.value)} />
                    <textarea className="textarea" rows={3} placeholder="Reusable prompt text..." value={ptBody} onChange={(e) => setPtBody(e.target.value)} />
                    <div>
                      <button type="button" className="btn btn-primary btn-sm" onClick={() => void savePromptTpl()}>
                        Save Prompt Template
                      </button>
                    </div>
                  </div>
                )}

                <div className="card-group">
                  {prompts.length === 0 ? (
                    <div className="card-row">
                      <span style={{ color: 'var(--text-muted)' }}>No saved prompt templates yet.</span>
                    </div>
                  ) : (
                    prompts.map((pt) => (
                      <div key={pt.name} className="card-row">
                        <div className="card-row-info">
                          <div className="card-row-title mono">{pt.name}</div>
                          <div className="card-row-desc">{pt.template.slice(0, 96)}</div>
                        </div>
                        <button
                          type="button"
                          className="btn btn-danger btn-sm"
                          onClick={async () => {
                            await callAction('prompts', { subaction: 'delete', name: pt.name });
                            await loadPrompts();
                          }}
                        >
                          <Trash2 size={12} />
                        </button>
                      </div>
                    ))
                  )}
                </div>
              </div>
            )}

            {section === 'models' && (
              <SettingsModelsPanel onRefreshStatus={onRefreshStatus} onToast={onToast} />
            )}

            {(section === 'vault' || section === 'federation' || section === 'ops') && (
              <SettingsInfraPanel
                section={section}
                onRefreshStatus={onRefreshStatus}
                onToast={onToast}
              />
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
