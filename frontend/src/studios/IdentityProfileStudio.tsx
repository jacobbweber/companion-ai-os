import React, { useEffect, useState } from 'react';
import { FolderGit2, HardDrive, RefreshCw, Save, Sparkles, UserCheck } from 'lucide-react';
import { callAction } from '../api';
import type { IdentitySnapshot } from '../types';

interface IdentityProfileStudioProps {
  onRefreshStatus: () => void;
  onToast: (msg: string) => void;
}

export const IdentityProfileStudio: React.FC<IdentityProfileStudioProps> = ({
  onRefreshStatus,
  onToast,
}) => {
  const [snap, setSnap] = useState<IdentitySnapshot | null>(null);
  const [companionName, setCompanionName] = useState('Companion');
  const [companionSoul, setCompanionSoul] = useState('');
  const [companionTone, setCompanionTone] = useState<'concise' | 'balanced' | 'elaborate' | string>('balanced');
  const [userName, setUserName] = useState('Partner');
  const [userRole, setUserRole] = useState('');
  const [userProfile, setUserProfile] = useState('');
  const [projectTarget, setProjectTarget] = useState('');
  const [saving, setSaving] = useState(false);

  const syncFromSnapshot = (data: IdentitySnapshot) => {
    setSnap(data);
    setCompanionName(data.companion_name || 'Companion');
    setCompanionSoul(data.companion_soul || '');
    setCompanionTone(data.companion_tone || 'balanced');
    setUserName(data.user_name || 'Partner');
    setUserRole(data.role || '');
    setUserProfile(data.user_profile || '');
  };

  const loadIdentity = async () => {
    const res = await callAction<IdentitySnapshot>('identity', { subaction: 'get' });
    if (res.ok) {
      syncFromSnapshot(res as unknown as IdentitySnapshot);
    }
  };

  useEffect(() => {
    void loadIdentity();
  }, []);

  const handleSaveIdentity = async () => {
    setSaving(true);
    const res = await callAction<IdentitySnapshot>('identity', {
      subaction: 'update',
      companion_name: companionName.trim(),
      companion_soul: companionSoul.trim(),
      companion_tone: companionTone,
      user_name: userName.trim(),
      role: userRole.trim(),
      user_profile: userProfile.trim(),
    });
    setSaving(false);
    if (res.ok) {
      syncFromSnapshot(res as unknown as IdentitySnapshot);
      onRefreshStatus();
      onToast(res.summary || 'Saved Identity & Profile.');
    } else {
      onToast(res.error || 'Could not save Identity & Profile.');
    }
  };

  const handleWorkspaceAction = async (
    subaction: 'project_self' | 'project_switch' | 'project_init',
    targetOverride?: string
  ) => {
    const target = (targetOverride ?? projectTarget).trim();
    const res = await callAction<IdentitySnapshot>('identity', {
      subaction,
      target,
    });
    if (res.ok) {
      syncFromSnapshot(res as unknown as IdentitySnapshot);
      setProjectTarget('');
      onRefreshStatus();
      onToast(res.summary || 'Updated active workspace.');
    } else {
      onToast(res.error || 'Workspace change failed.');
    }
  };

  const knownEntries = Object.entries(snap?.workspace?.known_projects || {});

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div className="studio-header">
        <div className="studio-title-group">
          <h1>Identity, Profile &amp; Workspace</h1>
          <p>
            Personalize your Companion&apos;s name, soul, and communication tone, update your own
            profile anytime, and manage isolated workspaces &amp; user data paths
          </p>
        </div>
        <div className="studio-actions">
          <span className={`badge ${snap?.onboarded ? 'ok' : 'warn'}`}>
            {snap?.onboarded ? 'PERSONALIZED' : 'DEFAULT PROFILE'}
          </span>
          <button type="button" className="btn" onClick={() => void loadIdentity()}>
            <RefreshCw size={13} /> Refresh
          </button>
        </div>
      </div>

      <div className="studio-scroll">
        <div className="grid-2">
          {/* Left: Companion Soul & Tone */}
          <div className="panel" data-testid="identity-companion-panel">
            <div className="panel-head">
              <span className="panel-title">
                <Sparkles size={13} /> Companion Identity &amp; Soul
              </span>
              <span className="badge accent">SYNCED TO IDENTITY.MD</span>
            </div>

            <div className="grid-2" style={{ marginBottom: 10 }}>
              <div>
                <label className="mono" style={{ fontSize: 11, display: 'block', marginBottom: 4 }}>
                  Companion Name
                </label>
                <input
                  aria-label="Companion Name"
                  className="input"
                  style={{ width: '100%' }}
                  placeholder="e.g. Companion, Atlas, Friday"
                  value={companionName}
                  onChange={(e) => setCompanionName(e.target.value)}
                />
              </div>

              <div>
                <label className="mono" style={{ fontSize: 11, display: 'block', marginBottom: 4 }}>
                  Communication Tone
                </label>
                <select
                  aria-label="Communication Tone"
                  className="select"
                  style={{ width: '100%' }}
                  value={companionTone}
                  onChange={(e) => setCompanionTone(e.target.value)}
                >
                  <option value="concise">Concise — Terse, bullet-first answers</option>
                  <option value="balanced">Balanced — Warm, plain-English summaries</option>
                  <option value="elaborate">Elaborate — Detailed walkthroughs &amp; tradeoffs</option>
                </select>
              </div>
            </div>

            <div>
              <label className="mono" style={{ fontSize: 11, display: 'block', marginBottom: 4 }}>
                Companion Soul &amp; Personality (1–2 sentences)
              </label>
              <textarea
                aria-label="Companion Soul"
                className="textarea"
                style={{ width: '100%', minHeight: 82 }}
                placeholder="Describe how your Companion should think, collaborate, and communicate..."
                value={companionSoul}
                onChange={(e) => setCompanionSoul(e.target.value)}
              />
            </div>
          </div>

          {/* Right: Human User Identity */}
          <div className="panel" data-testid="identity-user-panel">
            <div className="panel-head">
              <span className="panel-title">
                <UserCheck size={13} /> Your Profile &amp; Working Style
              </span>
              <span className="badge ok">LOCAL ONLY</span>
            </div>

            <div className="grid-2" style={{ marginBottom: 10 }}>
              <div>
                <label className="mono" style={{ fontSize: 11, display: 'block', marginBottom: 4 }}>
                  Your Name
                </label>
                <input
                  aria-label="Your Name"
                  className="input"
                  style={{ width: '100%' }}
                  placeholder="How your Companion should address you"
                  value={userName}
                  onChange={(e) => setUserName(e.target.value)}
                />
              </div>

              <div>
                <label className="mono" style={{ fontSize: 11, display: 'block', marginBottom: 4 }}>
                  Your Role / Focus (Optional)
                </label>
                <input
                  aria-label="Your Role"
                  className="input"
                  style={{ width: '100%' }}
                  placeholder="e.g. Creator, Researcher, Builder"
                  value={userRole}
                  onChange={(e) => setUserRole(e.target.value)}
                />
              </div>
            </div>

            <div>
              <label className="mono" style={{ fontSize: 11, display: 'block', marginBottom: 4 }}>
                About You &amp; Communication Preferences
              </label>
              <textarea
                aria-label="Your Profile"
                className="textarea"
                style={{ width: '100%', minHeight: 82 }}
                placeholder="What matters to you, how technical or high-level explanations should be, etc."
                value={userProfile}
                onChange={(e) => setUserProfile(e.target.value)}
              />
            </div>
          </div>
        </div>

        <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 14 }}>
          <button
            type="button"
            data-testid="save-identity-btn"
            className="btn btn-primary"
            disabled={saving}
            onClick={() => void handleSaveIdentity()}
          >
            <Save size={13} /> {saving ? 'Saving...' : 'Save Identity & Profile'}
          </button>
        </div>

        <div className="grid-2">
          {/* Active Workspace Switcher */}
          <div className="panel">
            <div className="panel-head">
              <span className="panel-title">
                <FolderGit2 size={13} /> Active Project Workspace (ADR-007)
              </span>
              <span className="badge">
                {snap?.workspace?.project_id || 'companion-self'}
              </span>
            </div>

            <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 10 }}>
              Active Root: <code className="mono">{snap?.workspace?.project_root || '.'}</code>
            </div>

            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 10 }}>
              <input
                className="input"
                style={{ flex: '1 1 200px' }}
                placeholder="External project folder name or absolute path"
                value={projectTarget}
                onChange={(e) => setProjectTarget(e.target.value)}
              />
              <button
                type="button"
                className="btn"
                disabled={!projectTarget.trim()}
                onClick={() => void handleWorkspaceAction('project_switch')}
              >
                Switch
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={!projectTarget.trim()}
                onClick={() => void handleWorkspaceAction('project_init')}
              >
                Init New Project
              </button>
              {!snap?.workspace?.is_self_project && (
                <button
                  type="button"
                  className="btn"
                  onClick={() => void handleWorkspaceAction('project_self')}
                >
                  Return to Companion Core
                </button>
              )}
            </div>

            {knownEntries.length > 0 && (
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                {knownEntries.map(([pid, ppath]) => (
                  <button
                    key={pid}
                    type="button"
                    className="btn"
                    style={{ fontSize: 11, padding: '3px 8px' }}
                    title={ppath}
                    onClick={() => void handleWorkspaceAction('project_switch', ppath)}
                  >
                    Switch: <code className="mono">{pid}</code>
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* Isolated User Data Paths */}
          <div className="panel" data-testid="identity-data-paths">
            <div className="panel-head">
              <span className="panel-title">
                <HardDrive size={13} /> Isolated User Data Directory
              </span>
              <span className="badge ok">CLEAN SEPARATION</span>
            </div>
            <p style={{ fontSize: 12, color: 'var(--text-secondary)', margin: '0 0 8px' }}>
              All conversations, Wiki notes, credentials, backups, and custom skills live inside
              your isolated data folder so app updates never overwrite personal state:
            </p>
            <div className="mono" style={{ fontSize: 11.5, lineHeight: 1.7 }}>
              <div>
                <strong>Data Root:</strong> {snap?.paths?.data_dir || 'data/'}
              </div>
              <div>
                <strong>SQLite DB:</strong> {snap?.paths?.db_path || 'data/conversations.db'}
              </div>
              <div>
                <strong>Identity Card:</strong> {snap?.paths?.identity_md_path || 'data/IDENTITY.md'}
              </div>
              <div>
                <strong>Custom Skills:</strong> {snap?.paths?.user_skills_dir || 'data/skills/'}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
