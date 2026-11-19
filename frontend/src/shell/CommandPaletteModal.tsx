import React, { useEffect, useState } from 'react';
import {
  BookOpen,
  Calendar,
  Clock,
  FileText,
  Folder,
  FolderKanban,
  MessageSquare,
  Search,
  Settings,
  Wrench,
  X,
} from 'lucide-react';
import { apiGet, callAction } from '../api';
import type { ProjectGraphEntry, SettingsSectionId, SurfaceId, WikiNoteSummary } from '../types';

interface CommandPaletteModalProps {
  projects: ProjectGraphEntry[];
  onClose: () => void;
  onSelectSurface: (s: SurfaceId) => void;
  onSelectProject: (projectId: string, openBoard?: boolean) => void;
  onJumpToWiki?: (notePath: string) => void;
  onOpenSettings: (section?: SettingsSectionId) => void;
}

interface SearchHitRow {
  id: number;
  role: string;
  content: string;
  timestamp: string;
}

interface PaletteEntry {
  key: string;
  group: string;
  label: string;
  sub: string;
  icon: React.ReactNode;
  run: () => void;
}

export const CommandPaletteModal: React.FC<CommandPaletteModalProps> = ({
  projects,
  onClose,
  onSelectSurface,
  onSelectProject,
  onJumpToWiki,
  onOpenSettings,
}) => {
  const [query, setQuery] = useState('');
  const [chatHits, setChatHits] = useState<SearchHitRow[]>([]);
  const [wikiHits, setWikiHits] = useState<WikiNoteSummary[]>([]);
  const [activeIdx, setActiveIdx] = useState(0);

  useEffect(() => {
    setActiveIdx(0);
    const q = query.trim();
    if (q.length < 2) {
      setChatHits([]);
      setWikiHits([]);
      return;
    }
    const timer = window.setTimeout(() => {
      void Promise.all([
        apiGet<{ ok?: boolean; results?: SearchHitRow[] }>(
          `/api/search?q=${encodeURIComponent(q)}&limit=6`
        ),
        callAction<{ hits?: WikiNoteSummary[] }>('wiki', {
          subaction: 'search',
          query: q,
          limit: 6,
        }),
      ]).then(([cRes, wRes]) => {
        if (cRes.results) setChatHits(cRes.results);
        if (wRes.hits) setWikiHits(wRes.hits);
      });
    }, 150);
    return () => window.clearTimeout(timer);
  }, [query]);

  const low = query.trim().toLowerCase();

  const quickActions: PaletteEntry[] = [
    { key: 'nav_chat', group: 'Navigation & Settings', label: 'Conversation', sub: 'Jump to main Chat canvas', icon: <MessageSquare size={14} />, run: () => onSelectSurface('chat') },
    { key: 'nav_boards', group: 'Navigation & Settings', label: 'Board & Goals', sub: 'Universal Project & Goal Graph', icon: <FolderKanban size={14} />, run: () => onSelectSurface('boards') },
    { key: 'nav_cal', group: 'Navigation & Settings', label: 'Calendar', sub: 'Today, Week & Month agenda', icon: <Calendar size={14} />, run: () => onSelectSurface('calendar') },
    { key: 'nav_wiki', group: 'Navigation & Settings', label: 'Knowledge Wiki', sub: 'Notes, Mind Map & Spaced Repetition', icon: <BookOpen size={14} />, run: () => onSelectSurface('knowledge') },
    { key: 'nav_auto', group: 'Navigation & Settings', label: 'Automations & Morning Briefing', sub: 'Manual, Scheduled & On-Idle TriggerPolicy routines', icon: <Clock size={14} />, run: () => onSelectSurface('automations') },
    { key: 'nav_cust', group: 'Navigation & Settings', label: 'Customizations', sub: 'Skills, Verified Python DAGs & MCP Harvester', icon: <Wrench size={14} />, run: () => onSelectSurface('customizations') },
    { key: 'set_models', group: 'Navigation & Settings', label: 'Settings: Models & Tiers', sub: 'Providers, capability tiers & context window', icon: <Settings size={14} />, run: () => onOpenSettings('models') },
    { key: 'set_vault', group: 'Navigation & Settings', label: 'Settings: Blind Credential Vault & Endpoints', sub: 'Secrets, SSH, WinRM & HTTP endpoints', icon: <Settings size={14} />, run: () => onOpenSettings('vault') },
    { key: 'set_ops', group: 'Navigation & Settings', label: 'Settings: Backups & System Doctor', sub: 'Snapshots, service reload & 7-stage diagnostics', icon: <Settings size={14} />, run: () => onOpenSettings('ops') },
  ].filter((a) => !low || a.label.toLowerCase().includes(low) || a.sub.toLowerCase().includes(low));

  const projectEntries: PaletteEntry[] = projects
    .filter((p) => !low || p.title.toLowerCase().includes(low) || p.slug.toLowerCase().includes(low))
    .map((p) => ({
      key: `proj_${p.project_id}`,
      group: 'Projects',
      label: p.title,
      sub: p.kind,
      icon: <Folder size={14} />,
      run: () => onSelectProject(p.project_id, true),
    }));

  const wikiEntries: PaletteEntry[] = wikiHits.map((w) => ({
    key: `wiki_${w.rel_path}`,
    group: 'Knowledge Wiki Matches',
    label: w.title || w.rel_path,
    sub: w.rel_path,
    icon: <FileText size={14} />,
    run: () => {
      if (onJumpToWiki) onJumpToWiki(w.rel_path);
      else onSelectSurface('knowledge');
    },
  }));

  const chatEntries: PaletteEntry[] = chatHits.map((h) => ({
    key: `chat_${h.id}`,
    group: 'Forever Chat History Matches',
    label: `[${h.role}] ${h.content.slice(0, 84)}`,
    sub: h.timestamp?.slice(0, 10) || '',
    icon: <MessageSquare size={14} />,
    run: () => onSelectSurface('chat'),
  }));

  const flatEntries = [...quickActions, ...projectEntries, ...wikiEntries, ...chatEntries];
  const safeIdx = flatEntries.length > 0 ? Math.min(activeIdx, flatEntries.length - 1) : 0;

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
            data-testid="command-palette-input"
            style={{ border: 'none', background: 'transparent', padding: 0, fontSize: 14 }}
            placeholder="Search surfaces, projects, Wiki notes, or Forever Chat..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'ArrowDown' && flatEntries.length > 0) {
                e.preventDefault();
                setActiveIdx((cur) => (cur + 1) % flatEntries.length);
              } else if (e.key === 'ArrowUp' && flatEntries.length > 0) {
                e.preventDefault();
                setActiveIdx((cur) => (cur - 1 + flatEntries.length) % flatEntries.length);
              } else if (e.key === 'Enter' && flatEntries[safeIdx]) {
                e.preventDefault();
                flatEntries[safeIdx].run();
                onClose();
              }
            }}
            autoFocus
          />
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close search">
            <X size={15} />
          </button>
        </div>

        <div style={{ flex: 1, overflowY: 'auto', padding: 8 }}>
          {flatEntries.map((entry, idx) => {
            const showHeader = idx === 0 || flatEntries[idx - 1].group !== entry.group;
            const isSelected = idx === safeIdx;
            return (
              <React.Fragment key={entry.key}>
                {showHeader && <div className="popover-group-label">{entry.group}</div>}
                <button
                  type="button"
                  className={`popover-row ${isSelected ? 'active' : ''}`}
                  style={isSelected ? { background: 'var(--bg-hover)' } : undefined}
                  onMouseEnter={() => setActiveIdx(idx)}
                  onClick={() => {
                    entry.run();
                    onClose();
                  }}
                >
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 9, minWidth: 0 }}>
                    {entry.icon}
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {entry.label}
                    </span>
                  </span>
                  <span style={{ fontSize: 11.5, color: 'var(--text-muted)', flexShrink: 0 }}>{entry.sub}</span>
                </button>
              </React.Fragment>
            );
          })}
        </div>
      </div>
    </div>
  );
};
