import React, { useEffect, useRef, useState } from 'react';
import { FolderGit2, GitBranchPlus, Link2, Sparkles } from 'lucide-react';
import { callAction } from '../api';
import { ProjectsFolderExplorer, RepoTargetPreviewControls, deriveTargetPath, parentRelUnderRoot, sanitizeRelativeRepoSegment, slugifyRepoTitle } from './ProjectsFolderExplorer';
import type { ProjectBoardProjection, ProjectGraphEntry, ProjectsBrowseResult, ProjectsExplorerEntry, RepoProbeResult, VaultSecretMeta } from '../types';

export { sanitizeRelativeRepoSegment };

interface ProjectConfigPanelProps {
  projectId?: string; createNew?: boolean; externalProjectsRoot?: string; onSaved: () => void;
  onCreatedProject?: (newProjectId: string) => void; onClose: () => void; onToast: (msg: string) => void;
}

export const ProjectConfigPanel: React.FC<ProjectConfigPanelProps> = ({
  projectId, createNew = false, externalProjectsRoot, onSaved, onCreatedProject, onClose, onToast,
}) => {
  const [loadedProj, setLoadedProj] = useState<ProjectGraphEntry | null>(null);
  const [projectMode, setProjectMode] = useState<'personal' | 'code'>('personal');
  const [repoSubMode, setRepoSubMode] = useState<'create' | 'attach'>(createNew ? 'create' : 'attach');
  const [projectsDir, setProjectsDir] = useState('');
  const [browseState, setBrowseState] = useState<ProjectsBrowseResult | null>(null);
  const [explorerLoading, setExplorerLoading] = useState(false);
  const [currentRelPath, setCurrentRelPath] = useState('');
  const [repoFolderName, setRepoFolderName] = useState('');
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

  const folderNameEditedRef = useRef(false);
  const customPathEditedRef = useRef(false);
  const titleRef = useRef('');
  const folderNameRef = useRef('');
  const relPathRef = useRef('');
  const wsPathRef = useRef('');
  const subModeRef = useRef<'create' | 'attach'>(createNew ? 'create' : 'attach');

  titleRef.current = projTitle;
  folderNameRef.current = repoFolderName;
  relPathRef.current = currentRelPath;
  wsPathRef.current = projWsPath;
  subModeRef.current = repoSubMode;

  const updateWsPath = (next: string) => {
    wsPathRef.current = next;
    setProjWsPath(next);
  };

  const applyBrowsePayload = (res: ProjectsBrowseResult) => {
    setBrowseState(res);
    const rootStr = String(res.projects_root || res.projects_dir || '');
    const relStr = String(res.current_rel_path || '');
    if (rootStr) setProjectsDir(rootStr);
    setCurrentRelPath(relStr);
    relPathRef.current = relStr;
    if (subModeRef.current === 'create' && !customPathEditedRef.current && rootStr) {
      const leaf = folderNameRef.current.trim() || slugifyRepoTitle(titleRef.current);
      updateWsPath(leaf ? deriveTargetPath(rootStr, relStr, leaf) : '');
    }
  };

  const navigateExplorer = async (relPath: string, resetCreateSelection = false) => {
    if (resetCreateSelection && subModeRef.current === 'create') {
      customPathEditedRef.current = false;
      setProbeResult(null);
    }
    setExplorerLoading(true);
    try {
      const res = await callAction<ProjectsBrowseResult>('agile', {
        subaction: 'browse_projects_dir',
        rel_path: relPath,
      });
      if (res.ok) {
        applyBrowsePayload(res as unknown as ProjectsBrowseResult);
        if (subModeRef.current === 'create' && !customPathEditedRef.current) setProbeResult(null);
      } else {
        onToast(res.error || 'Could not browse folder.');
      }
    } finally {
      setExplorerLoading(false);
    }
  };

  const handleSaveRoot = async (newRoot: string, reset = false): Promise<boolean> => {
    const res = await callAction<ProjectsBrowseResult>('agile', {
      subaction: reset ? 'reset_projects_root' : 'set_projects_root',
      projects_root: newRoot,
      reset,
    });
    if (!res.ok) {
      onToast(res.error || 'Could not update Projects Root.');
      return false;
    }
    const nextRoot = String(res.projects_root || res.projects_dir || '');
    if (nextRoot) setProjectsDir(nextRoot);
    onSaved();
    onToast(res.summary || `Updated Projects Root to ${nextRoot}`);
    await navigateExplorer('', true);
    return true;
  };

  const handleCreateSubfolder = async (folderName: string): Promise<boolean> => {
    const res = await callAction<ProjectsBrowseResult>('agile', {
      subaction: 'create_projects_subfolder',
      parent_rel_path: currentRelPath,
      name: folderName,
    });
    if (!res.ok) {
      onToast(res.error || 'Could not create subfolder.');
      return false;
    }
    if (subModeRef.current === 'create') {
      customPathEditedRef.current = false;
      setProbeResult(null);
    }
    applyBrowsePayload(res as unknown as ProjectsBrowseResult);
    onToast(res.summary || `Created subfolder "${folderName}".`);
    return true;
  };

  const runProbe = async (rawTargetPath?: string) => {
    const cleanPath = (rawTargetPath ?? wsPathRef.current).trim();
    if (!cleanPath) {
      onToast('Enter or select a repository folder path first.');
      return;
    }
    setProbing(true);
    try {
      const res = await callAction<RepoProbeResult>('agile', {
        subaction: 'probe_repo',
        workspace_path: cleanPath,
      });
      const probe = res as unknown as RepoProbeResult;
      const pDir = probe.projects_root || probe.projects_dir;
      if (pDir && !projectsDir) setProjectsDir(pDir);
      setProbeResult(probe);
    } finally {
      setProbing(false);
    }
  };

  const handleSelectEntry = (entry: ProjectsExplorerEntry) => {
    if (subModeRef.current === 'create') {
      if (!entry.is_git_repo) {
        void navigateExplorer(entry.rel_path, true);
      } else {
        customPathEditedRef.current = true;
        updateWsPath(entry.abs_path);
        void runProbe(entry.abs_path);
      }
      return;
    }
    if (entry.is_git_repo) {
      customPathEditedRef.current = true;
      updateWsPath(entry.abs_path);
      if (!titleRef.current.trim()) {
        const niceTitle =
          entry.linked_project_title ||
          entry.name.replace(/[-_]+/g, ' ').replace(/\b\w/g, (ch) => ch.toUpperCase());
        setProjTitle(niceTitle);
        titleRef.current = niceTitle;
      }
      void runProbe(entry.abs_path);
    } else {
      void navigateExplorer(entry.rel_path);
    }
  };

  useEffect(() => {
    const ext = (externalProjectsRoot || '').trim();
    if (!ext || !projectsDir || ext === projectsDir) return;
    setProjectsDir(ext);
    void navigateExplorer('', true);
  }, [externalProjectsRoot]);

  useEffect(() => {
    setProbeResult(null);
    void callAction<{ secrets?: VaultSecretMeta[]; entries?: VaultSecretMeta[] }>('vault', {
      subaction: 'list',
    }).then((vRes) => setVaultSecrets(vRes.secrets || vRes.entries || []));

    if (createNew) {
      folderNameEditedRef.current = false;
      customPathEditedRef.current = false;
      setLoadedProj(null);
      setProjectMode('personal');
      setRepoSubMode('create');
      subModeRef.current = 'create';
      setCurrentRelPath('');
      relPathRef.current = '';
      setRepoFolderName('');
      folderNameRef.current = '';
      setProjTitle('');
      titleRef.current = '';
      setProjGoal('');
      updateWsPath('');
      setProjDelivery('local_only');
      setProjGitCred('');
      setMaxWorkers(2);
      setTpMode('manual');
      void navigateExplorer('');
      return;
    }

    customPathEditedRef.current = true;
    folderNameEditedRef.current = false;
    setRepoSubMode('attach');
    subModeRef.current = 'attach';
    void callAction<ProjectBoardProjection>('agile', {
      subaction: 'board_view',
      project_id: projectId || '',
    }).then((bRes) => {
      if (!bRes.ok) {
        void navigateExplorer('');
        return;
      }
      const view = bRes as unknown as ProjectBoardProjection;
      const pRoot = String(view.projects_root || view.projects_dir || externalProjectsRoot || '');
      if (pRoot) setProjectsDir(pRoot);
      const p = view.project;
      if (!p) {
        void navigateExplorer('');
        return;
      }
      setLoadedProj(p);
      const hasWs = Boolean(p.workspace_path && p.workspace_path.trim());
      const isCode = Boolean(
        hasWs || p.kind === 'software' || (p.delivery_policy && p.delivery_policy !== 'local_only')
      );
      customPathEditedRef.current = hasWs || p.project_id === 'companion-self';
      setProjectMode(isCode ? 'code' : 'personal');
      setProjTitle(p.title);
      setProjGoal(p.goal_summary || '');
      updateWsPath(p.workspace_path || '');
      setProjDelivery(p.delivery_policy || 'local_only');
      setProjGitCred(p.git_credential_ref || '');
      setMaxWorkers(p.max_parallel_workers ?? 2);
      setTpMode(p.trigger_policy?.mode || 'manual');
      setTpWindow(p.trigger_policy?.allowed_window || '22:00-06:00');
      setTpIdleSec(p.trigger_policy?.idle_min_seconds ?? 900);
      setTpCooldownSec(p.trigger_policy?.cooldown_seconds ?? 3600);
      void navigateExplorer(hasWs && pRoot ? parentRelUnderRoot(p.workspace_path || '', pRoot) : '');
    });
  }, [projectId, createNew]);

  const saveProject = async () => {
    const cleanTitle = projTitle.trim();
    if (!cleanTitle) {
      onToast('Project title is required.');
      return;
    }
    const isCode = projectMode === 'code';
    const wantInitGit = isCode && repoSubMode === 'create';
    const fallbackLeaf = repoFolderName.trim() || slugifyRepoTitle(cleanTitle);
    const cleanWs = isCode
      ? projWsPath.trim() ||
        (wantInitGit && projectsDir ? deriveTargetPath(projectsDir, currentRelPath, fallbackLeaf) : '')
      : '';
    const wantAttachExisting = isCode && repoSubMode === 'attach' && (createNew || Boolean(cleanWs));
    if (createNew && repoSubMode === 'attach' && isCode && !cleanWs) {
      onToast('Select or enter an existing Git clone folder path to attach.');
      return;
    }
    const payload = {
      title: cleanTitle,
      goal_summary: projGoal.trim(),
      kind: isCode ? 'software' : loadedProj?.kind === 'software' ? 'project' : loadedProj?.kind || 'project',
      workspace_path: cleanWs,
      init_git: wantInitGit,
      require_existing: wantAttachExisting,
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
        focus: true,
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

  const credOptions = Array.from(new Set([...vaultSecrets.map((s) => s.name), ...(projGitCred ? [projGitCred] : [])]));
  const previewPath =
    projWsPath.trim() ||
    deriveTargetPath(projectsDir, currentRelPath, repoFolderName.trim() || slugifyRepoTitle(projTitle) || '<repo-slug>');

  return (
    <div style={{ display: 'grid', gap: 12 }} data-testid="project-inspector">
      <div>
        <label className="field-label">Project Mode</label>
        <div className="segmented-pills" style={{ width: '100%', display: 'grid', gridTemplateColumns: '1fr 1fr' }}>
          <button type="button" data-testid="project-mode-personal" className={`segmented-pill ${projectMode === 'personal' ? 'active' : ''}`} onClick={() => setProjectMode('personal')}>
            <Sparkles size={12} /> Personal / Goal Board
          </button>
          <button
            type="button"
            data-testid="project-mode-code"
            className={`segmented-pill ${projectMode === 'code' ? 'active' : ''}`}
            onClick={() => {
              setProjectMode('code');
              if (!browseState) void navigateExplorer('');
              if (repoSubMode === 'create' && !customPathEditedRef.current && projectsDir && projTitle.trim()) {
                const leaf = repoFolderName.trim() || slugifyRepoTitle(projTitle);
                if (!folderNameEditedRef.current) setRepoFolderName(leaf);
                updateWsPath(deriveTargetPath(projectsDir, currentRelPath, leaf));
              }
            }}
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
          onChange={(e) => {
            const val = e.target.value;
            setProjTitle(val);
            titleRef.current = val;
            if (subModeRef.current === 'create') {
              const nextLeaf = folderNameEditedRef.current ? folderNameRef.current : slugifyRepoTitle(val);
              if (!folderNameEditedRef.current) {
                setRepoFolderName(nextLeaf);
                folderNameRef.current = nextLeaf;
              }
              if (!customPathEditedRef.current && projectsDir) {
                updateWsPath(deriveTargetPath(projectsDir, relPathRef.current, nextLeaf));
                setProbeResult(null);
              }
            }
          }}
        />
      </div>

      <div>
        <label className="field-label">Goal Summary &amp; Outcome</label>
        <textarea className="textarea" rows={2} value={projGoal} onChange={(e) => setProjGoal(e.target.value)} />
      </div>

      {projectMode === 'code' && (
        <div
          style={{ display: 'grid', gap: 10, padding: 12, borderRadius: 'var(--radius-sm)', border: '1px solid var(--border-hairline)', background: 'var(--bg-elevated)' }}
          data-testid="code-repo-settings-box"
        >
          <div>
            <label className="field-label">Repository Setup</label>
            <div className="segmented-pills" style={{ width: '100%', display: 'grid', gridTemplateColumns: '1fr 1fr' }}>
              <button
                type="button"
                data-testid="repo-mode-create"
                className={`segmented-pill ${repoSubMode === 'create' ? 'active' : ''}`}
                onClick={() => {
                  setRepoSubMode('create');
                  subModeRef.current = 'create';
                  setProbeResult(null);
                  customPathEditedRef.current = false;
                  const leaf = folderNameRef.current.trim() || slugifyRepoTitle(titleRef.current);
                  if (!folderNameEditedRef.current && leaf) {
                    setRepoFolderName(leaf);
                    folderNameRef.current = leaf;
                  }
                  updateWsPath(projectsDir && leaf ? deriveTargetPath(projectsDir, relPathRef.current, leaf) : '');
                }}
              >
                <GitBranchPlus size={12} /> Create New Repo
              </button>
              <button
                type="button"
                data-testid="repo-mode-attach"
                className={`segmented-pill ${repoSubMode === 'attach' ? 'active' : ''}`}
                onClick={() => {
                  setRepoSubMode('attach');
                  subModeRef.current = 'attach';
                  setProbeResult(null);
                  if (!customPathEditedRef.current) updateWsPath('');
                }}
              >
                <Link2 size={12} /> Attach Existing Clone
              </button>
            </div>
          </div>

          <ProjectsFolderExplorer
            mode={repoSubMode}
            browseState={browseState}
            projectsRoot={projectsDir}
            loading={explorerLoading}
            selectedAbsPath={projWsPath}
            onNavigateRel={(rel) => navigateExplorer(rel, true)}
            onSaveRoot={handleSaveRoot}
            onCreateSubfolder={handleCreateSubfolder}
            onSelectEntry={handleSelectEntry}
          />

          <RepoTargetPreviewControls
            mode={repoSubMode}
            projectsDir={projectsDir}
            currentRelPath={currentRelPath}
            projTitle={projTitle}
            repoFolderName={repoFolderName}
            projWsPath={projWsPath}
            previewPath={previewPath}
            probing={probing}
            probeResult={probeResult}
            onFolderNameChange={(raw) => {
              const cleaned = sanitizeRelativeRepoSegment(raw);
              if (!cleaned.trim()) {
                folderNameEditedRef.current = false;
                customPathEditedRef.current = false;
                setRepoFolderName('');
                folderNameRef.current = '';
                updateWsPath('');
                setProbeResult(null);
                return;
              }
              folderNameEditedRef.current = true;
              customPathEditedRef.current = false;
              setRepoFolderName(cleaned);
              folderNameRef.current = cleaned;
              updateWsPath(projectsDir ? deriveTargetPath(projectsDir, currentRelPath, cleaned) : cleaned);
              setProbeResult(null);
            }}
            onCustomPathChange={(val) => {
              if (!val.trim()) {
                customPathEditedRef.current = false;
                const leaf = repoFolderName.trim() || slugifyRepoTitle(projTitle);
                updateWsPath(
                  repoSubMode === 'create' && projectsDir && leaf
                    ? deriveTargetPath(projectsDir, currentRelPath, leaf)
                    : ''
                );
              } else {
                customPathEditedRef.current = true;
                updateWsPath(val);
              }
              setProbeResult(null);
            }}
            onCheckRepo={() => void runProbe()}
          />

          <div className="form-grid-2">
            <div>
              <label className="field-label">Delivery Policy</label>
              <select className="select" data-testid="project-delivery-policy-select" value={projDelivery} onChange={(e) => setProjDelivery(e.target.value)}>
                <option value="local_only">Local Only (Default — No Push/PR)</option>
                <option value="ask_me">Ask Before Push / PR</option>
                <option value="remote_branch_pr">Remote Branch + PR</option>
              </select>
            </div>
            <div>
              <label className="field-label">Git Credential (Blind Vault)</label>
              <select className="select mono" data-testid="project-git-cred-input" value={projGitCred} onChange={(e) => setProjGitCred(e.target.value)}>
                <option value="">None / System Auth</option>
                {credOptions.map((name) => <option key={name} value={name}>{name}</option>)}
              </select>
            </div>
          </div>

          <div>
            <label className="field-label">Max Parallel Agents (Isolated Worktrees)</label>
            <select className="select" data-testid="project-max-workers-select" value={maxWorkers} onChange={(e) => setMaxWorkers(Number(e.target.value))}>
              {[1, 2, 3, 4].map((n) => <option key={n} value={n}>{n} Parallel {n === 1 ? 'Worker' : 'Workers'}</option>)}
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
            <input className="input mono" placeholder="22:00-06:00 or leave blank for anytime" value={tpWindow} onChange={(e) => setTpWindow(e.target.value)} />
          </div>
          <div className="form-grid-2">
            <div>
              <label className="field-label">Min Idle Seconds</label>
              <input type="number" className="input" value={tpIdleSec} onChange={(e) => setTpIdleSec(Number(e.target.value))} />
            </div>
            <div>
              <label className="field-label">Cooldown Seconds</label>
              <input type="number" className="input" value={tpCooldownSec} onChange={(e) => setTpCooldownSec(Number(e.target.value))} />
            </div>
          </div>
        </>
      )}

      <button type="button" className="btn btn-primary" data-testid="save-project-policy-btn" onClick={() => void saveProject()}>
        {createNew ? 'Create Project' : 'Save Project Settings'}
      </button>
    </div>
  );
};
