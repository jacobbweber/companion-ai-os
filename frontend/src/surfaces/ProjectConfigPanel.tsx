import React, { useEffect, useState } from 'react';
import { CheckCircle2, FolderGit2, RefreshCw, Sparkles } from 'lucide-react';
import { callAction } from '../api';
import type {
  ProjectBoardProjection,
  ProjectGraphEntry,
  RepoProbeResult,
  VaultSecretMeta,
} from '../types';

interface ProjectConfigPanelProps {
  projectId?: string;
  createNew?: boolean;
  onSaved: () => void;
  onCreatedProject?: (newProjectId: string) => void;
  onClose: () => void;
  onToast: (msg: string) => void;
}

export const ProjectConfigPanel: React.FC<ProjectConfigPanelProps> = ({
  projectId,
  createNew = false,
  onSaved,
  onCreatedProject,
  onClose,
  onToast,
}) => {
  const [loadedProj, setLoadedProj] = useState<ProjectGraphEntry | null>(null);
  const [projectMode, setProjectMode] = useState<'personal' | 'code'>('personal');
  const [projTitle, setProjTitle] = useState('');
  const [projGoal, setProjGoal] = useState('');
  const [projWsPath, setProjWsPath] = useState('');
  const [projDelivery, setProjDelivery] = useState('local_only');
  const [projGitCred, setProjGitCred] = useState('');
  const [maxWorkers, setMaxWorkers] = useState(2);
  const [tpMode, setTpMode] = useState('manual');
  const [tpWindow, setTpWindow] = useState('22:00-06:00');
  const [tpIdleSec, setTpIdleSec] = useState(900);
  const [tpCooldownSec, setTpCooldownSec] = useState(3600);
  const [vaultSecrets, setVaultSecrets] = useState<VaultSecretMeta[]>([]);
  const [probeResult, setProbeResult] = useState<RepoProbeResult | null>(null);
  const [probing, setProbing] = useState(false);

  useEffect(() => {
    setProbeResult(null);
    void callAction<{ secrets?: VaultSecretMeta[]; entries?: VaultSecretMeta[] }>('vault', {
      subaction: 'list',
    }).then((vRes) => {
      const list = vRes.secrets || vRes.entries || [];
      setVaultSecrets(list);
    });

    if (createNew) {
      setLoadedProj(null);
      setProjectMode('personal');
      setProjTitle('');
      setProjGoal('');
      setProjWsPath('');
      setProjDelivery('local_only');
      setProjGitCred('');
      setMaxWorkers(2);
      setTpMode('manual');
      return;
    }

    void callAction<ProjectBoardProjection>('agile', {
      subaction: 'board_view',
      project_id: projectId || '',
    }).then((bRes) => {
      if (bRes.ok) {
        const view = bRes as unknown as ProjectBoardProjection;
        const p = view.project;
        if (p) {
          setLoadedProj(p);
          const isCode = Boolean(
            (p.workspace_path && p.workspace_path.trim()) ||
              p.kind === 'software' ||
              (p.delivery_policy && p.delivery_policy !== 'local_only')
          );
          setProjectMode(isCode ? 'code' : 'personal');
          setProjTitle(p.title);
          setProjGoal(p.goal_summary || '');
          setProjWsPath(p.workspace_path || '');
          setProjDelivery(p.delivery_policy || 'local_only');
          setProjGitCred(p.git_credential_ref || '');
          setMaxWorkers(p.max_parallel_workers ?? 2);
          setTpMode(p.trigger_policy?.mode || 'manual');
          setTpWindow(p.trigger_policy?.allowed_window || '22:00-06:00');
          setTpIdleSec(p.trigger_policy?.idle_min_seconds ?? 900);
          setTpCooldownSec(p.trigger_policy?.cooldown_seconds ?? 3600);
        }
      }
    });
  }, [projectId, createNew]);

  const checkRepository = async () => {
    const cleanPath = projWsPath.trim();
    if (!cleanPath) {
      onToast('Enter a repository folder path first.');
      return;
    }
    setProbing(true);
    try {
      const res = await callAction<RepoProbeResult>('agile', {
        subaction: 'probe_repo',
        workspace_path: cleanPath,
      });
      setProbeResult(res as unknown as RepoProbeResult);
    } finally {
      setProbing(false);
    }
  };

  const saveProject = async () => {
    const cleanTitle = projTitle.trim();
    if (!cleanTitle) {
      onToast('Project title is required.');
      return;
    }
    const isCode = projectMode === 'code';
    const payload = {
      title: cleanTitle,
      goal_summary: projGoal.trim(),
      kind: isCode ? 'software' : loadedProj?.kind === 'software' ? 'project' : loadedProj?.kind || 'project',
      workspace_path: isCode ? projWsPath.trim() : '',
      delivery_policy: isCode ? projDelivery : 'local_only',
      git_credential_ref: isCode ? projGitCred.trim() : '',
      max_parallel_workers: isCode ? maxWorkers : 1,
      trigger_policy: {
        mode: tpMode,
        allowed_window: tpMode === 'on_idle' ? tpWindow.trim() : '',
        idle_min_seconds: tpIdleSec,
        cooldown_seconds: tpCooldownSec,
        precheck_name: tpMode === 'on_idle' ? 'active_goal_has_ready_items' : '',
      },
    };

    if (createNew) {
      const res = await callAction<{ project?: ProjectGraphEntry }>('agile', {
        subaction: 'create_project',
        ...payload,
      });
      if (res.ok && res.project) {
        onSaved();
        onCreatedProject?.(res.project.project_id);
        onClose();
        onToast(`Created ${isCode ? 'Code Repository' : 'Personal'} project "${res.project.title}".`);
      } else {
        onToast(res.error || 'Could not create project.');
      }
      return;
    }

    if (!loadedProj) return;
    const res = await callAction('agile', {
      subaction: 'update_project',
      project_id: loadedProj.project_id,
      ...payload,
    });
    if (res.ok) {
      onSaved();
      onToast('Updated Project settings.');
    } else {
      onToast(res.error || 'Could not update project.');
    }
  };

  const credOptions = Array.from(
    new Set([
      ...vaultSecrets.map((s) => s.name),
      ...(projGitCred ? [projGitCred] : []),
    ])
  );

  return (
    <div style={{ display: 'grid', gap: 12 }} data-testid="project-inspector">
      <div>
        <label className="field-label">Project Mode</label>
        <div className="segmented-pills" style={{ width: '100%', display: 'grid', gridTemplateColumns: '1fr 1fr' }}>
          <button
            type="button"
            data-testid="project-mode-personal"
            className={`segmented-pill ${projectMode === 'personal' ? 'active' : ''}`}
            onClick={() => setProjectMode('personal')}
          >
            <Sparkles size={12} /> Personal / Goal Board
          </button>
          <button
            type="button"
            data-testid="project-mode-code"
            className={`segmented-pill ${projectMode === 'code' ? 'active' : ''}`}
            onClick={() => setProjectMode('code')}
          >
            <FolderGit2 size={12} /> Code Repository
          </button>
        </div>
      </div>

      <div>
        <label className="field-label">Project / Goal Title</label>
        <input
          className="input"
          data-testid="project-title-input"
          placeholder={projectMode === 'code' ? 'e.g. Payments API' : 'e.g. Home Renovation'}
          value={projTitle}
          onChange={(e) => setProjTitle(e.target.value)}
        />
      </div>

      <div>
        <label className="field-label">Goal Summary &amp; Outcome</label>
        <textarea
          className="textarea"
          rows={2}
          value={projGoal}
          onChange={(e) => setProjGoal(e.target.value)}
        />
      </div>

      {projectMode === 'code' && (
        <div
          style={{
            display: 'grid',
            gap: 10,
            padding: 12,
            borderRadius: 'var(--radius-sm)',
            border: '1px solid var(--border-hairline)',
            background: 'var(--bg-elevated)',
          }}
          data-testid="code-repo-settings-box"
        >
          <div>
            <label className="field-label">Repository Path (Local Git Clone)</label>
            <div style={{ display: 'flex', gap: 6 }}>
              <input
                className="input mono"
                data-testid="project-workspace-path-input"
                placeholder="e.g. D:/Projects/MyService"
                value={projWsPath}
                onChange={(e) => {
                  setProjWsPath(e.target.value);
                  setProbeResult(null);
                }}
              />
              <button
                type="button"
                className="btn btn-sm"
                data-testid="check-repo-btn"
                disabled={probing}
                onClick={() => void checkRepository()}
                style={{ flexShrink: 0 }}
              >
                <RefreshCw size={12} /> {probing ? 'Checking...' : 'Check Repo'}
              </button>
            </div>
            {probeResult && (
              <div style={{ marginTop: 6 }} data-testid="repo-probe-badge">
                <span className={`badge ${probeResult.is_git_repo ? 'ok' : 'warn'}`}>
                  {probeResult.is_git_repo && <CheckCircle2 size={11} />}
                  {probeResult.summary || probeResult.error || 'Inspected path'}
                </span>
              </div>
            )}
          </div>

          <div className="form-grid-2">
            <div>
              <label className="field-label">Delivery Policy</label>
              <select
                className="select"
                data-testid="project-delivery-policy-select"
                value={projDelivery}
                onChange={(e) => setProjDelivery(e.target.value)}
              >
                <option value="local_only">Local Only (Default — No Push/PR)</option>
                <option value="ask_me">Ask Before Push / PR</option>
                <option value="remote_branch_pr">Remote Branch + PR</option>
              </select>
            </div>
            <div>
              <label className="field-label">Git Credential (Blind Vault)</label>
              <select
                className="select mono"
                data-testid="project-git-cred-input"
                value={projGitCred}
                onChange={(e) => setProjGitCred(e.target.value)}
              >
                <option value="">None / System Auth</option>
                {credOptions.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div>
            <label className="field-label">Max Parallel Agents (Isolated Worktrees)</label>
            <select
              className="select"
              data-testid="project-max-workers-select"
              value={maxWorkers}
              onChange={(e) => setMaxWorkers(Number(e.target.value))}
            >
              {[1, 2, 3, 4].map((n) => (
                <option key={n} value={n}>
                  {n} Parallel {n === 1 ? 'Worker' : 'Workers'}
                </option>
              ))}
            </select>
          </div>
        </div>
      )}

      <div>
        <label className="field-label">Autonomous Execution Policy</label>
        <select className="select" value={tpMode} onChange={(e) => setTpMode(e.target.value)}>
          <option value="manual">Manual (Only advance when asked)</option>
          <option value="on_idle">On Idle (Advance unblocked Companion slices automatically)</option>
        </select>
      </div>

      {tpMode === 'on_idle' && (
        <>
          <div>
            <label className="field-label">Allowed Time Window (e.g. 22:00-06:00)</label>
            <input
              className="input mono"
              placeholder="22:00-06:00 or leave blank for anytime"
              value={tpWindow}
              onChange={(e) => setTpWindow(e.target.value)}
            />
          </div>
          <div className="form-grid-2">
            <div>
              <label className="field-label">Min Idle Seconds</label>
              <input
                type="number"
                className="input"
                value={tpIdleSec}
                onChange={(e) => setTpIdleSec(Number(e.target.value))}
              />
            </div>
            <div>
              <label className="field-label">Cooldown Seconds</label>
              <input
                type="number"
                className="input"
                value={tpCooldownSec}
                onChange={(e) => setTpCooldownSec(Number(e.target.value))}
              />
            </div>
          </div>
        </>
      )}

      <button
        type="button"
        className="btn btn-primary"
        data-testid="save-project-policy-btn"
        onClick={() => void saveProject()}
      >
        {createNew ? 'Create Project' : 'Save Project Settings'}
      </button>
    </div>
  );
};
