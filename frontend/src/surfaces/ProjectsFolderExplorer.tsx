import React, { useEffect, useState } from 'react';
import {
  ArrowUp,
  CheckCircle2,
  ChevronRight,
  Folder,
  FolderGit2,
  FolderPlus,
  Pencil,
  RefreshCw,
  RotateCcw,
} from 'lucide-react';
import type { ProjectsBrowseResult, ProjectsExplorerEntry, RepoProbeResult } from '../types';

export function slugifyRepoTitle(title: string): string {
  return title
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

export function sanitizeRelativeRepoSegment(raw: string): string {
  const noLeading = raw.replace(/^[\\/]+/, '').replace(/\\/g, '/').replace(/\/+/g, '/');
  return noLeading
    .split('/')
    .filter((seg, idx, arr) => {
      if (seg === '.' || seg === '..') return false;
      if (seg === '' && idx < arr.length - 1) return false;
      return true;
    })
    .join('/');
}

function joinHostPath(baseRoot: string, ...relParts: string[]): string {
  const base = baseRoot.trim().replace(/[\\/]+$/, '');
  const sep = base.includes('\\') && !base.includes('/') ? '\\' : '/';
  const segs: string[] = [];
  for (const part of relParts) {
    const clean = (part || '').trim().replace(/^[\\/]+|[\\/]+$/g, '');
    if (!clean) continue;
    for (const s of clean.split(/[\\/]+/)) {
      if (s && s !== '.' && s !== '..') segs.push(s);
    }
  }
  if (segs.length === 0) return base;
  return base ? `${base}${sep}${segs.join(sep)}` : segs.join(sep);
}

export function deriveTargetPath(projectsRoot: string, parentRel: string, repoFolder: string): string {
  const cleanLeaf = sanitizeRelativeRepoSegment(repoFolder).replace(/\/+$/, '').trim();
  return cleanLeaf ? joinHostPath(projectsRoot, parentRel, cleanLeaf) : '';
}

function normalizePathCompare(p: string): string {
  return (p || '').trim().replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
}

interface ProjectsFolderExplorerProps {
  mode: 'create' | 'attach';
  browseState: ProjectsBrowseResult | null;
  projectsRoot: string;
  loading: boolean;
  selectedAbsPath: string;
  onNavigateRel: (relPath: string) => Promise<void>;
  onSaveRoot: (newRoot: string, reset?: boolean) => Promise<boolean>;
  onCreateSubfolder: (folderName: string) => Promise<boolean>;
  onSelectEntry: (entry: ProjectsExplorerEntry) => void;
}

export const ProjectsFolderExplorer: React.FC<ProjectsFolderExplorerProps> = ({
  mode,
  browseState,
  projectsRoot,
  loading,
  selectedAbsPath,
  onNavigateRel,
  onSaveRoot,
  onCreateSubfolder,
  onSelectEntry,
}) => {
  const [editingRoot, setEditingRoot] = useState(false);
  const [rootDraft, setRootDraft] = useState(projectsRoot);
  const [savingRoot, setSavingRoot] = useState(false);
  const [creatingFolder, setCreatingFolder] = useState(false);
  const [newFolderDraft, setNewFolderDraft] = useState('');
  const [savingFolder, setSavingFolder] = useState(false);

  useEffect(() => {
    if (!editingRoot) setRootDraft(projectsRoot);
  }, [projectsRoot, editingRoot]);

  const submitRoot = async (reset = false) => {
    setSavingRoot(true);
    try {
      const ok = await onSaveRoot(rootDraft.trim(), reset);
      if (ok) setEditingRoot(false);
    } finally {
      setSavingRoot(false);
    }
  };

  const submitSubfolder = async () => {
    const clean = newFolderDraft.trim().replace(/^[\\/]+|[\\/]+$/g, '');
    if (!clean) return;
    setSavingFolder(true);
    try {
      const ok = await onCreateSubfolder(clean);
      if (ok) {
        setNewFolderDraft('');
        setCreatingFolder(false);
      }
    } finally {
      setSavingFolder(false);
    }
  };

  const crumbs = browseState?.breadcrumbs?.length ? browseState.breadcrumbs : [{ label: 'Root', rel_path: '' }];
  const entries = browseState?.entries || [];
  const normSelected = normalizePathCompare(selectedAbsPath);

  return (
    <div style={{ display: 'grid', gap: 8 }} data-testid="projects-folder-explorer">
      {/* Layer 1: Pinned Projects Root Header */}
      <div
        style={{
          display: 'grid',
          gap: 6,
          padding: '8px 10px',
          borderRadius: 'var(--radius-xs)',
          background: 'var(--bg-panel)',
          border: '1px solid var(--border-hairline)',
        }}
        data-testid="projects-root-header"
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', minWidth: 0 }}>
            <span style={{ fontSize: 11.5, color: 'var(--text-secondary)', fontWeight: 600 }}>Projects Root:</span>
            <code
              className="mono"
              data-testid="projects-root-display"
              style={{ fontSize: 11.5, color: 'var(--text-primary)', wordBreak: 'break-all' }}
            >
              {projectsRoot || '(loading...)'}
            </code>
            <span className={`badge ${browseState?.is_custom_root ? 'ok' : ''}`}>
              {browseState?.is_custom_root ? 'Pinned' : 'Default'}
            </span>
          </div>
          <button
            type="button"
            className="btn btn-sm"
            data-testid="change-projects-root-btn"
            onClick={() => {
              setRootDraft(projectsRoot);
              setEditingRoot((v) => !v);
            }}
          >
            <Pencil size={11} /> {editingRoot ? 'Close' : 'Change Root'}
          </button>
        </div>

        {editingRoot && (
          <div style={{ display: 'grid', gap: 6, marginTop: 2 }} data-testid="projects-root-editor">
            <input
              className="input mono"
              data-testid="projects-root-input"
              placeholder="e.g. /home/spark/projects or D:/Projects"
              value={rootDraft}
              onChange={(e) => setRootDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  void submitRoot(false);
                }
              }}
            />
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              <button
                type="button"
                className="btn btn-primary btn-sm"
                data-testid="save-projects-root-btn"
                disabled={savingRoot}
                onClick={() => void submitRoot(false)}
              >
                {savingRoot ? 'Saving...' : 'Save Root'}
              </button>
              {browseState?.is_custom_root && (
                <button
                  type="button"
                  className="btn btn-sm"
                  data-testid="reset-projects-root-btn"
                  disabled={savingRoot}
                  onClick={() => void submitRoot(true)}
                >
                  <RotateCcw size={11} /> Reset Default
                </button>
              )}
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => setEditingRoot(false)}>
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Layer 2: Confined Relative Directory Explorer */}
      <div style={{ border: '1px solid var(--border-hairline)', borderRadius: 'var(--radius-xs)', background: 'var(--bg-input)', overflow: 'hidden' }}>
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            gap: 6,
            padding: '6px 8px',
            borderBottom: '1px solid var(--border-hairline)',
            background: 'var(--bg-panel)',
            flexWrap: 'wrap',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 4, flexWrap: 'wrap', minWidth: 0 }}>
            <button
              type="button"
              className="btn btn-sm"
              data-testid="explorer-up-btn"
              disabled={!browseState?.can_go_up || loading}
              onClick={() => void onNavigateRel(browseState?.parent_rel_path || '')}
              title="Up to parent folder"
              style={{ padding: '2px 7px' }}
            >
              <ArrowUp size={11} /> Up
            </button>
            <div style={{ display: 'flex', alignItems: 'center', gap: 2, flexWrap: 'wrap', fontSize: 12 }}>
              {crumbs.map((c, idx) => (
                <React.Fragment key={c.rel_path || 'root'}>
                  {idx > 0 && <ChevronRight size={12} style={{ color: 'var(--text-muted)' }} />}
                  <button
                    type="button"
                    data-testid={idx === 0 ? 'explorer-crumb-root' : `explorer-crumb-${c.rel_path}`}
                    className="btn btn-ghost btn-sm"
                    style={{
                      padding: '2px 5px',
                      fontWeight: idx === crumbs.length - 1 ? 600 : 400,
                      color: idx === crumbs.length - 1 ? 'var(--text-primary)' : 'var(--text-secondary)',
                    }}
                    onClick={() => void onNavigateRel(c.rel_path)}
                  >
                    {c.label}
                  </button>
                </React.Fragment>
              ))}
            </div>
          </div>
          <button
            type="button"
            className="btn btn-sm"
            data-testid="explorer-new-folder-toggle"
            onClick={() => setCreatingFolder((v) => !v)}
            style={{ padding: '2px 8px' }}
          >
            <FolderPlus size={11} /> + New Folder
          </button>
        </div>

        {creatingFolder && (
          <div
            style={{
              display: 'flex',
              gap: 6,
              padding: '7px 8px',
              borderBottom: '1px solid var(--border-hairline)',
              background: 'var(--bg-elevated)',
              flexWrap: 'wrap',
            }}
            data-testid="explorer-new-folder-bar"
          >
            <input
              className="input mono"
              data-testid="explorer-new-folder-input"
              style={{ flex: '1 1 160px', minWidth: 140 }}
              placeholder="Subfolder name (e.g. experiments)"
              value={newFolderDraft}
              onChange={(e) => setNewFolderDraft(e.target.value.replace(/^[\\/]+/, ''))}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  void submitSubfolder();
                }
              }}
            />
            <button
              type="button"
              className="btn btn-primary btn-sm"
              data-testid="explorer-new-folder-submit"
              disabled={savingFolder || !newFolderDraft.trim()}
              onClick={() => void submitSubfolder()}
            >
              {savingFolder ? 'Creating...' : 'Create'}
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => {
                setCreatingFolder(false);
                setNewFolderDraft('');
              }}
            >
              Cancel
            </button>
          </div>
        )}

        <div style={{ maxHeight: 184, overflowY: 'auto' }} data-testid="explorer-entry-list">
          {loading && entries.length === 0 ? (
            <div style={{ padding: '10px 12px', fontSize: 12, color: 'var(--text-muted)' }}>Loading folders...</div>
          ) : entries.length === 0 ? (
            <div style={{ padding: '10px 12px', fontSize: 12, color: 'var(--text-muted)' }}>
              {mode === 'create'
                ? 'No subfolders here yet — new repo will be created in this folder (or click + New Folder to organize).'
                : 'No subfolders or Git repositories found in this directory.'}
            </div>
          ) : (
            entries.map((entry) => {
              const isSelected = Boolean(normSelected && normalizePathCompare(entry.abs_path) === normSelected);
              return (
                <div
                  key={entry.rel_path}
                  data-testid={`explorer-entry-${entry.name}`}
                  role="button"
                  tabIndex={0}
                  onClick={() => onSelectEntry(entry)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      onSelectEntry(entry);
                    }
                  }}
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    gap: 8,
                    padding: '7px 10px',
                    borderBottom: '1px solid var(--border-hairline)',
                    cursor: 'pointer',
                    background: isSelected ? 'var(--accent-soft)' : 'transparent',
                    flexWrap: 'wrap',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 7, minWidth: 0 }}>
                    {entry.is_git_repo ? (
                      <FolderGit2 size={14} style={{ color: 'var(--status-ok)', flexShrink: 0 }} />
                    ) : (
                      <Folder size={14} style={{ color: 'var(--accent-primary)', flexShrink: 0 }} />
                    )}
                    <span className="mono" style={{ fontSize: 12, fontWeight: isSelected ? 600 : 500 }}>{entry.name}</span>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 5, flexWrap: 'wrap' }}>
                    {entry.linked_project_id && (
                      <span className="badge" data-testid={`explorer-badge-linked-${entry.name}`}>
                        Linked Project · {entry.linked_project_title}
                      </span>
                    )}
                    {entry.is_git_repo ? (
                      <span className="badge ok" data-testid={`explorer-badge-git-${entry.name}`}>
                        Git Repo{entry.branch ? ` · ${entry.branch}` : ''}
                      </span>
                    ) : (
                      <span className="badge" data-testid={`explorer-badge-folder-${entry.name}`}>Folder</span>
                    )}
                    {mode === 'attach' && entry.is_git_repo && entry.has_children && (
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        data-testid={`explorer-drill-${entry.name}`}
                        style={{ padding: '1px 5px', fontSize: 11 }}
                        onClick={(e) => {
                          e.stopPropagation();
                          void onNavigateRel(entry.rel_path);
                        }}
                        title="Browse subfolders inside this repository"
                      >
                        Subfolders <ChevronRight size={11} />
                      </button>
                    )}
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
};

interface RepoTargetPreviewControlsProps {
  mode: 'create' | 'attach';
  projectsDir: string;
  currentRelPath: string;
  projTitle: string;
  repoFolderName: string;
  projWsPath: string;
  previewPath: string;
  probing: boolean;
  probeResult: RepoProbeResult | null;
  onFolderNameChange: (raw: string) => void;
  onCustomPathChange: (raw: string) => void;
  onCheckRepo: () => void;
}

export const RepoTargetPreviewControls: React.FC<RepoTargetPreviewControlsProps> = ({
  mode,
  projectsDir,
  currentRelPath,
  projTitle,
  repoFolderName,
  projWsPath,
  previewPath,
  probing,
  probeResult,
  onFolderNameChange,
  onCustomPathChange,
  onCheckRepo,
}) => {
  const probeValid =
    probeResult &&
    probeResult.ok !== false &&
    (mode === 'attach' ? probeResult.is_git_repo : Boolean(probeResult.is_git_repo || probeResult.can_init));
  const probeText = !probeResult
    ? ''
    : probeResult.ok === false
      ? probeResult.error || probeResult.summary || 'Invalid repository path'
      : mode === 'attach' && !probeResult.exists
        ? `Directory "${probeResult.repo_root || projWsPath.trim()}" does not exist — switch to "Create New Repo" to initialize it.`
        : mode === 'attach' && !probeResult.is_git_repo
          ? `${probeResult.summary || 'Folder is not a Git repository'} — switch to "Create New Repo" to initialize Git on main.`
          : probeResult.summary || probeResult.error || 'Inspected path';

  return (
    <>
      {mode === 'create' && (
        <div style={{ display: 'grid', gap: 6 }}>
          <label className="field-label">New Repo Folder Name (Relative to Selected Folder)</label>
          <input
            className="input mono"
            data-testid="repo-folder-name-input"
            placeholder={slugifyRepoTitle(projTitle) || 'my-service'}
            value={repoFolderName}
            onChange={(e) => onFolderNameChange(e.target.value)}
          />
        </div>
      )}

      <div
        data-testid={mode === 'create' ? 'create-repo-live-preview' : 'attach-selected-repo-banner'}
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          gap: 8,
          padding: '7px 10px',
          borderRadius: 'var(--radius-xs)',
          background: 'var(--bg-panel)',
          border: '1px solid var(--border-hairline)',
          fontSize: 11.5,
          flexWrap: 'wrap',
        }}
      >
        {mode === 'create' ? (
          <span style={{ color: 'var(--text-secondary)', wordBreak: 'break-all' }}>
            Will initialize Git repo on <strong>main</strong> at:{' '}
            <code className="mono" style={{ color: 'var(--text-primary)' }}>{previewPath}</code>
          </span>
        ) : (
          <span style={{ color: 'var(--text-secondary)', wordBreak: 'break-all' }}>
            Selected clone:{' '}
            <code className="mono" style={{ color: 'var(--text-primary)' }}>
              {projWsPath.trim() || '(click a [Git Repo] row above)'}
            </code>
          </span>
        )}
        <button
          type="button"
          className="btn btn-sm"
          data-testid="check-repo-btn"
          disabled={probing}
          onClick={onCheckRepo}
          style={{ flexShrink: 0 }}
        >
          <RefreshCw size={12} /> {probing ? 'Checking...' : 'Check Repo'}
        </button>
      </div>

      {probeResult && (
        <div data-testid="repo-probe-badge">
          <span className={`badge ${probeValid ? 'ok' : 'warn'}`}>
            {probeResult.is_git_repo && <CheckCircle2 size={11} />}
            {probeText}
          </span>
        </div>
      )}

      <details data-testid="custom-repo-path-details" style={{ fontSize: 12 }}>
        <summary style={{ cursor: 'pointer', color: 'var(--text-secondary)', userSelect: 'none' }}>
          Custom path outside Projects Root
        </summary>
        <div style={{ marginTop: 6 }}>
          <input
            className="input mono"
            data-testid="project-workspace-path-input"
            placeholder={
              mode === 'create' && projectsDir
                ? deriveTargetPath(projectsDir, currentRelPath, 'my-service')
                : 'e.g. /mnt/external/my-repo or D:/Work/MyRepo'
            }
            value={projWsPath}
            onChange={(e) => onCustomPathChange(e.target.value)}
          />
        </div>
      </details>
    </>
  );
};
