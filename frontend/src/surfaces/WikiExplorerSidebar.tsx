import React from 'react';
import {
  ChevronDown,
  ChevronRight,
  ChevronsDownUp,
  ChevronsUpDown,
  FileCode2,
  FileText,
  Folder,
  FolderOpen,
  Image as ImageIcon,
  PanelLeftClose,
  RefreshCw,
  X,
} from 'lucide-react';
import type { WikiNoteSummary, WikiTreeNode } from '../types';

interface WikiExplorerSidebarProps {
  tree: WikiTreeNode[];
  totalFiles: number;
  expandedPaths: Record<string, boolean>;
  selectedPath: string;
  query: string;
  searchHits: WikiNoteSummary[];
  onToggleFolder: (relPath: string) => void;
  onExpandAll: () => void;
  onCollapseAll: () => void;
  onSelectFile: (node: { rel_path: string; is_asset?: boolean; asset_url?: string; title?: string }) => void;
  onClearQuery: () => void;
  onApplyToken: (token: string) => void;
  onReindex: () => void;
  onCollapseSidebar: () => void;
}

const FOLDER_LABELS: Record<string, string> = {
  '01_inbox': '01_inbox',
  '02_knowledge': '02_knowledge',
  '03_resources': '03_resources',
  '04_archive': '04_archive',
};

export function collectDefaultExpanded(nodes: WikiTreeNode[]): Record<string, boolean> {
  const out: Record<string, boolean> = {};
  const walk = (list: WikiTreeNode[], openBranch: boolean) => {
    for (const n of list) {
      if (n.kind === 'folder') {
        if (openBranch || n.rel_path === '01_inbox' || n.rel_path === '02_knowledge') out[n.rel_path] = true;
        if (n.children) walk(n.children, n.rel_path.startsWith('02_knowledge'));
      }
    }
  };
  walk(nodes, false);
  return out;
}

export function collectAllFolderPaths(nodes: WikiTreeNode[]): Record<string, boolean> {
  const out: Record<string, boolean> = {};
  const walk = (list: WikiTreeNode[]) => {
    for (const n of list) {
      if (n.kind === 'folder') {
        out[n.rel_path] = true;
        if (n.children) walk(n.children);
      }
    }
  };
  walk(nodes);
  return out;
}

export function findFirstReadableFile(nodes: WikiTreeNode[]): string {
  for (const n of nodes) {
    if (n.kind === 'file' && !n.system_file && !n.is_asset && n.folder !== '04_archive') return n.rel_path;
    if (n.children) {
      const sub = findFirstReadableFile(n.children);
      if (sub) return sub;
    }
  }
  return '';
}

export function ancestorFoldersFor(relPath: string): string[] {
  const parts = relPath.split('/').filter(Boolean);
  return parts.slice(0, -1).map((_, idx) => parts.slice(0, idx + 1).join('/'));
}

export const WikiExplorerSidebar: React.FC<WikiExplorerSidebarProps> = ({
  tree,
  totalFiles,
  expandedPaths,
  selectedPath,
  query,
  searchHits,
  onToggleFolder,
  onExpandAll,
  onCollapseAll,
  onSelectFile,
  onClearQuery,
  onApplyToken,
  onReindex,
  onCollapseSidebar,
}) => {
  const hasSearch = query.trim().length > 0;
  const anyExpanded = Object.values(expandedPaths).some(Boolean);

  const renderTreeNode = (node: WikiTreeNode, depth: number): React.ReactNode => {
    const indentPx = 8 + depth * 14;
    if (node.kind === 'folder') {
      const isOpen = Boolean(expandedPaths[node.rel_path]);
      const children = node.children || [];
      return (
        <div key={node.rel_path}>
          <button
            type="button"
            className="wiki-tree-row wiki-tree-folder"
            style={{ paddingLeft: indentPx }}
            onClick={() => onToggleFolder(node.rel_path)}
            data-testid={`wiki-folder-${node.rel_path}`}
          >
            <span className="wiki-tree-caret">
              {isOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
            </span>
            <span className="wiki-tree-icon folder">
              {isOpen ? <FolderOpen size={14} /> : <Folder size={14} />}
            </span>
            <span className="wiki-tree-label">{FOLDER_LABELS[node.name] || node.name}</span>
            <span className="wiki-tree-count">{node.file_count ?? 0}</span>
          </button>

          {isOpen && (
            <div>
              {children.length === 0 ? (
                <div
                  className="wiki-tree-empty"
                  style={{ paddingLeft: indentPx + 28 }}
                >
                  Empty folder
                </div>
              ) : (
                children.map((child) => renderTreeNode(child, depth + 1))
              )}
            </div>
          )}
        </div>
      );
    }

    const isActive = selectedPath === node.rel_path;
    const isTemplateOrSys = Boolean(node.system_file && !node.is_asset);
    return (
      <button
        key={node.rel_path}
        type="button"
        className={`wiki-tree-row wiki-tree-file ${isActive ? 'active' : ''}`}
        style={{ paddingLeft: indentPx + 14 }}
        title={`${node.title || node.name} (${node.rel_path})`}
        onClick={() =>
          onSelectFile({
            rel_path: node.rel_path,
            is_asset: node.is_asset,
            asset_url: node.asset_url,
            title: node.title || node.name,
          })
        }
        data-testid={`wiki-file-${node.rel_path}`}
      >
        <span className="wiki-tree-icon file">
          {node.is_asset ? (
            <ImageIcon size={13} />
          ) : isTemplateOrSys ? (
            <FileCode2 size={13} />
          ) : (
            <FileText size={13} />
          )}
        </span>
        <span className="wiki-tree-file-body">
          <span className="wiki-tree-label">{node.title || node.name}</span>
          {!node.system_file && node.name !== node.title && (
            <span className="wiki-tree-subpath mono">{node.name}</span>
          )}
        </span>
        {node.stale && <span className="wiki-stale-dot" title="Verification due" />}
      </button>
    );
  };

  return (
    <aside className="wiki-sidebar" data-testid="wiki-explorer-sidebar">
      <div className="wiki-explorer-header">
        <div className="wiki-explorer-title">
          <span>Explorer</span>
          <span className="wiki-tree-count">{hasSearch ? `${searchHits.length} hits` : `${totalFiles}`}</span>
        </div>
        <div style={{ display: 'inline-flex', alignItems: 'center', gap: 2 }}>
          {!hasSearch && (
            <button
              type="button"
              className="icon-btn"
              title={anyExpanded ? 'Collapse all folders' : 'Expand all folders'}
              onClick={anyExpanded ? onCollapseAll : onExpandAll}
            >
              {anyExpanded ? <ChevronsDownUp size={13} /> : <ChevronsUpDown size={13} />}
            </button>
          )}
          <button
            type="button"
            className="icon-btn"
            title="Refresh & reindex directory tree"
            onClick={onReindex}
          >
            <RefreshCw size={13} />
          </button>
          <button
            type="button"
            className="icon-btn desktop-only-inline"
            title="Collapse folder & file explorer"
            onClick={onCollapseSidebar}
            data-testid="wiki-collapse-sidebar-btn"
          >
            <PanelLeftClose size={14} />
          </button>
        </div>
      </div>

      {hasSearch ? (
        <div className="wiki-note-list">
          <div className="wiki-search-banner">
            <span>
              {searchHits.length} matching note{searchHits.length === 1 ? '' : 's'}
            </span>
            <button type="button" className="wiki-clear-link" onClick={onClearQuery}>
              <X size={11} /> Clear
            </button>
          </div>
          {searchHits.length === 0 ? (
            <div style={{ padding: 14, fontSize: 12.5, color: 'var(--text-muted)' }}>
              No notes matched <span className="mono">{query}</span>. Try <span className="mono">tag:</span>,{' '}
              <span className="mono">domain:</span>, <span className="mono">template:</span>, or{' '}
              <span className="mono">ontology:</span>.
            </div>
          ) : (
            searchHits.map((n) => (
              <div
                key={n.rel_path}
                className={`wiki-note-item ${selectedPath === n.rel_path ? 'active' : ''}`}
                role="button"
                tabIndex={0}
                onClick={() => onSelectFile({ rel_path: n.rel_path, title: n.title })}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    onSelectFile({ rel_path: n.rel_path, title: n.title });
                  }
                }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: 6, alignItems: 'baseline' }}>
                  <div style={{ fontWeight: 500, fontSize: 13 }}>{n.title}</div>
                  {n.match_kind === 'ontology_bridge' && (
                    <span className="badge accent" style={{ fontSize: 10 }}>
                      2-hop
                    </span>
                  )}
                </div>
                <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-muted)', marginTop: 2 }}>
                  {n.rel_path}
                </div>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 5 }}>
                  {n.domain && (
                    <span
                      className="wiki-mini-pill"
                      onClick={(e) => {
                        e.stopPropagation();
                        onApplyToken(`domain:${n.domain}`);
                      }}
                    >
                      {n.domain}
                    </span>
                  )}
                  {n.template && (
                    <span
                      className="wiki-mini-pill"
                      onClick={(e) => {
                        e.stopPropagation();
                        onApplyToken(`template:${n.template}`);
                      }}
                    >
                      {n.template}
                    </span>
                  )}
                  {(n.tags || []).slice(0, 3).map((tg) => (
                    <span
                      key={tg}
                      className="wiki-mini-pill tag"
                      onClick={(e) => {
                        e.stopPropagation();
                        onApplyToken(`tag:${tg}`);
                      }}
                    >
                      #{tg}
                    </span>
                  ))}
                </div>
              </div>
            ))
          )}
        </div>
      ) : (
        <div className="wiki-tree-scroll" data-testid="wiki-directory-tree">
          {tree.map((rootNode) => renderTreeNode(rootNode, 0))}
        </div>
      )}
    </aside>
  );
};
