import React, { useState } from 'react';
import {
  BookOpen,
  Calendar,
  Clock,
  Folder,
  FolderKanban,
  FolderPlus,
  MessageSquarePlus,
  PanelLeftClose,
  RefreshCw,
  Search,
  Settings,
  Wrench,
} from 'lucide-react';
import type { ProjectGraphEntry, SurfaceId, WebStatus } from '../types';

interface LeftSidebarProps {
  collapsed: boolean;
  activeSurface: SurfaceId;
  status: WebStatus | null;
  projects: ProjectGraphEntry[];
  selectedProjectId: string;
  onToggleCollapse: () => void;
  onNewConversation: () => void;
  onSelectSurface: (surface: SurfaceId) => void;
  onSelectProject: (projectId: string, openBoard?: boolean) => void;
  onCreateProject: (title: string) => Promise<void>;
  onOpenSearch: () => void;
  onOpenSettings: () => void;
  onRefresh: () => void;
}

const SURFACE_NAV: Array<{ id: SurfaceId; label: string; icon: React.ReactNode }> = [
  { id: 'boards', label: 'Board & Goals', icon: <FolderKanban size={15} /> },
  { id: 'calendar', label: 'Calendar', icon: <Calendar size={15} /> },
  { id: 'knowledge', label: 'Knowledge', icon: <BookOpen size={15} /> },
  { id: 'automations', label: 'Automations', icon: <Clock size={15} /> },
  { id: 'customizations', label: 'Customizations', icon: <Wrench size={15} /> },
];

export const LeftSidebar: React.FC<LeftSidebarProps> = ({
  collapsed,
  activeSurface,
  status,
  projects,
  selectedProjectId,
  onToggleCollapse,
  onNewConversation,
  onSelectSurface,
  onSelectProject,
  onCreateProject,
  onOpenSearch,
  onOpenSettings,
  onRefresh,
}) => {
  const [creatingProj, setCreatingProj] = useState(false);
  const [projDraft, setProjDraft] = useState('');

  const submitNewProject = async (ev: React.FormEvent) => {
    ev.preventDefault();
    const clean = projDraft.trim();
    if (!clean) return;
    setProjDraft('');
    setCreatingProj(false);
    await onCreateProject(clean);
  };

  const openCards = status?.agile_summary?.open_cards || 0;
  const unseenAutonomy = status?.unseen_autonomy_count || 0;
  const dueStudy = status?.education_summary?.due_total || 0;

  return (
    <aside
      className={`agy-rail ${collapsed ? 'collapsed' : ''}`}
      data-testid="left-sidebar"
      aria-label="Primary Navigation"
    >
      <div className="rail-top-row">
        <button
          type="button"
          className="rail-brand"
          onClick={() => onSelectSurface('chat')}
          title="Companion Home"
        >
          <span className="rail-brand-glyph">C</span>
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {status?.companion_name || 'Companion'}
          </span>
        </button>

        <div className="rail-icon-cluster">
          <button
            type="button"
            className="icon-btn"
            onClick={onOpenSearch}
            title="Search & Quick Jump (Ctrl+K)"
            aria-label="Search"
          >
            <Search size={14} />
          </button>
          <button
            type="button"
            className="icon-btn"
            onClick={onRefresh}
            title="Refresh status"
            aria-label="Refresh"
          >
            <RefreshCw size={14} />
          </button>
          <button
            type="button"
            className="icon-btn"
            onClick={onToggleCollapse}
            title="Collapse sidebar"
            aria-label="Collapse sidebar"
            data-testid="collapse-sidebar-btn"
          >
            <PanelLeftClose size={15} />
          </button>
        </div>
      </div>

      <button
        type="button"
        className="rail-new-chat-btn"
        data-testid="new-conversation-btn"
        onClick={onNewConversation}
      >
        <MessageSquarePlus size={15} />
        <span>New Conversation</span>
      </button>

      <nav className="nav-items" aria-label="Surfaces">
        {SURFACE_NAV.map((item) => {
          const badge =
            item.id === 'boards' && openCards > 0
              ? openCards
              : item.id === 'automations' && unseenAutonomy > 0
                ? unseenAutonomy
                : item.id === 'knowledge' && dueStudy > 0
                  ? dueStudy
                  : 0;
          return (
            <button
              key={item.id}
              type="button"
              data-testid={`nav-${item.id}`}
              className={`rail-nav-item ${activeSurface === item.id ? 'active' : ''}`}
              onClick={() => onSelectSurface(item.id)}
            >
              <span className="rail-nav-left">
                {item.icon}
                <span>{item.label}</span>
              </span>
              {badge > 0 && <span className="count-pill">{badge}</span>}
            </button>
          );
        })}
      </nav>

      <div className="rail-divider" />

      <div className="rail-section-head">
        <span>Projects</span>
        <button
          type="button"
          className="icon-btn"
          style={{ width: 22, height: 22 }}
          title="New Goal / Project"
          onClick={() => setCreatingProj((v) => !v)}
        >
          <FolderPlus size={13} />
        </button>
      </div>

      {creatingProj && (
        <form onSubmit={(e) => void submitNewProject(e)} style={{ marginBottom: 8 }}>
          <input
            type="text"
            className="input"
            placeholder="Project or goal title..."
            value={projDraft}
            onChange={(e) => setProjDraft(e.target.value)}
            autoFocus
          />
        </form>
      )}

      <div className="rail-projects-scroll">
        {projects.map((proj) => {
          const isActive =
            selectedProjectId === proj.project_id || selectedProjectId === proj.slug;
          return (
            <div
              key={proj.project_id}
              className={`rail-project-row ${isActive ? 'active' : ''}`}
              onClick={() => onSelectProject(proj.project_id, false)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === 'Enter') onSelectProject(proj.project_id, false);
              }}
            >
              <span className="rail-nav-left">
                <Folder size={14} style={{ flexShrink: 0, opacity: 0.78 }} />
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{proj.title}</span>
              </span>
              <button
                type="button"
                className="icon-btn"
                style={{ width: 22, height: 22 }}
                title={`Open ${proj.title} Board`}
                onClick={(e) => {
                  e.stopPropagation();
                  onSelectProject(proj.project_id, true);
                }}
              >
                <FolderKanban size={12} />
              </button>
            </div>
          );
        })}
      </div>

      <div className="rail-footer">
        <button
          type="button"
          className="rail-nav-item"
          data-testid="open-settings-btn"
          onClick={onOpenSettings}
          style={{ flex: 1 }}
        >
          <span className="rail-nav-left">
            <Settings size={15} />
            <span>Settings</span>
          </span>
          <span
            className="mono"
            style={{ fontSize: 10.5, color: 'var(--text-muted)' }}
          >
            v3.5
          </span>
        </button>
      </div>
    </aside>
  );
};
