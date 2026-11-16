import React, { useEffect, useState } from 'react';
import {
  BookOpen,
  Calendar,
  Clock,
  Folder,
  FolderKanban,
  MessageSquare,
  Search,
  Settings,
  Wrench,
  X,
} from 'lucide-react';
import { apiGet } from '../api';
import type { ProjectGraphEntry, SettingsSectionId, SurfaceId } from '../types';

interface CommandPaletteModalProps {
  projects: ProjectGraphEntry[];
  onClose: () => void;
  onSelectSurface: (s: SurfaceId) => void;
  onSelectProject: (projectId: string, openBoard?: boolean) => void;
  onOpenSettings: (section?: SettingsSectionId) => void;
}

interface SearchHitRow {
  id: number;
  role: string;
  content: string;
  timestamp: string;
}

export const CommandPaletteModal: React.FC<CommandPaletteModalProps> = ({
  projects,
  onClose,
  onSelectSurface,
  onSelectProject,
  onOpenSettings,
}) => {
  const [query, setQuery] = useState('');
  const [chatHits, setChatHits] = useState<SearchHitRow[]>([]);

  useEffect(() => {
    const q = query.trim();
    if (q.length < 2) {
      setChatHits([]);
      return;
    }
    const timer = window.setTimeout(() => {
      void apiGet<{ ok?: boolean; results?: SearchHitRow[] }>(
        `/api/search?q=${encodeURIComponent(q)}&limit=8`
      ).then((res) => {
        if (res.results) setChatHits(res.results);
      });
    }, 150);
    return () => window.clearTimeout(timer);
  }, [query]);

  const low = query.trim().toLowerCase();

  const quickActions: Array<{ label: string; sub: string; icon: React.ReactNode; run: () => void }> = [
    { label: 'Conversation', sub: 'Jump to main Chat canvas', icon: <MessageSquare size={14} />, run: () => onSelectSurface('chat') },
    { label: 'Board & Goals', sub: 'Universal Project & Goal Graph', icon: <FolderKanban size={14} />, run: () => onSelectSurface('boards') },
    { label: 'Calendar', sub: 'Today, Week & Month agenda', icon: <Calendar size={14} />, run: () => onSelectSurface('calendar') },
    { label: 'Knowledge Wiki', sub: 'Notes, Mind Map & Spaced Repetition', icon: <BookOpen size={14} />, run: () => onSelectSurface('knowledge') },
    { label: 'Automations & Morning Briefing', sub: 'Manual, Scheduled & On-Idle TriggerPolicy routines', icon: <Clock size={14} />, run: () => onSelectSurface('automations') },
    { label: 'Customizations', sub: 'Skills, Verified Python DAGs & MCP Harvester', icon: <Wrench size={14} />, run: () => onSelectSurface('customizations') },
    { label: 'Settings: Models & Tiers', sub: 'Providers, capability tiers & context window', icon: <Settings size={14} />, run: () => onOpenSettings('models') },
    { label: 'Settings: Blind Credential Vault & Endpoints', sub: 'Secrets, SSH, WinRM & HTTP endpoints', icon: <Settings size={14} />, run: () => onOpenSettings('vault') },
    { label: 'Settings: Backups, Sandboxes & System Doctor', sub: 'Snapshots, self-dev clones & 7-stage diagnostics', icon: <Settings size={14} />, run: () => onOpenSettings('ops') },
  ].filter((a) => !low || a.label.toLowerCase().includes(low) || a.sub.toLowerCase().includes(low));

  const matchedProjects = projects.filter(
    (p) => !low || p.title.toLowerCase().includes(low) || p.slug.toLowerCase().includes(low)
  );

  return (
    <div className="modal-backdrop" onClick={onClose} data-testid="command-palette-backdrop">
      <div className="cmd-palette-modal" onClick={(e) => e.stopPropagation()}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            padding: '12px 14px',
            borderBottom: '1px solid var(--border-hairline)',
          }}
        >
          <Search size={16} style={{ color: 'var(--text-muted)', flexShrink: 0 }} />
          <input
            type="text"
            className="input"
            style={{ border: 'none', background: 'transparent', padding: 0, fontSize: 14 }}
            placeholder="Search surfaces, projects, settings, or Forever Chat..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            autoFocus
          />
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close search">
            <X size={15} />
          </button>
        </div>

        <div style={{ flex: 1, overflowY: 'auto', padding: 8 }}>
          {quickActions.length > 0 && (
            <>
              <div className="popover-group-label">Navigation &amp; Settings</div>
              {quickActions.map((act) => (
                <button
                  key={act.label}
                  type="button"
                  className="popover-row"
                  onClick={() => {
                    act.run();
                    onClose();
                  }}
                >
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 9 }}>
                    {act.icon}
                    <span>{act.label}</span>
                  </span>
                  <span style={{ fontSize: 11.5, color: 'var(--text-muted)' }}>{act.sub}</span>
                </button>
              ))}
            </>
          )}

          {matchedProjects.length > 0 && (
            <>
              <div className="popover-group-label">Projects</div>
              {matchedProjects.map((p) => (
                <button
                  key={p.project_id}
                  type="button"
                  className="popover-row"
                  onClick={() => {
                    onSelectProject(p.project_id, true);
                    onClose();
                  }}
                >
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 9 }}>
                    <Folder size={14} />
                    <span>{p.title}</span>
                  </span>
                  <span className="badge">{p.kind}</span>
                </button>
              ))}
            </>
          )}

          {chatHits.length > 0 && (
            <>
              <div className="popover-group-label">Forever Chat History Matches</div>
              {chatHits.map((h) => (
                <button
                  key={h.id}
                  type="button"
                  className="popover-row"
                  onClick={() => {
                    onSelectSurface('chat');
                    onClose();
                  }}
                >
                  <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    <strong>[{h.role}]</strong> {h.content.slice(0, 90)}
                  </span>
                </button>
              ))}
            </>
          )}
        </div>
      </div>
    </div>
  );
};
