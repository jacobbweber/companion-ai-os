import React, { useEffect, useState } from 'react';
import { Cpu, Globe, KeyRound, Settings2, Sparkles, UserCheck } from 'lucide-react';
import type { WebStatus } from '../types';
import { AutonomyHobbiesStudio } from './AutonomyHobbiesStudio';
import { FederationMcpStudio } from './FederationMcpStudio';
import { IdentityProfileStudio } from './IdentityProfileStudio';
import { ModelsTiersStudio } from './ModelsTiersStudio';
import { OpsStudio } from './OpsStudio';
import { VaultEndpointsStudio } from './VaultEndpointsStudio';

export type SystemSubTab = 'identity' | 'models' | 'autonomy' | 'vault' | 'federation' | 'ops';

interface SystemStudioProps {
  status: WebStatus | null;
  initialTab?: SystemSubTab;
  tabNonce?: number;
  onRefreshStatus: () => void;
  onToast: (msg: string) => void;
}

export const SystemStudio: React.FC<SystemStudioProps> = ({
  status,
  initialTab = 'models',
  tabNonce = 0,
  onRefreshStatus,
  onToast,
}) => {
  const [subTab, setSubTab] = useState<SystemSubTab>(initialTab);

  useEffect(() => {
    if (tabNonce > 0) {
      setSubTab(initialTab);
    }
  }, [initialTab, tabNonce]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      <div className="system-subnav" role="tablist" aria-label="System Administration Sections">
        <button
          type="button"
          role="tab"
          aria-selected={subTab === 'identity'}
          className={`btn ${subTab === 'identity' ? 'btn-primary' : ''}`}
          onClick={() => setSubTab('identity')}
        >
          <UserCheck size={13} /> Identity &amp; Profile
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={subTab === 'models'}
          className={`btn ${subTab === 'models' ? 'btn-primary' : ''}`}
          onClick={() => setSubTab('models')}
        >
          <Cpu size={13} /> Models, Tiers &amp; Context
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={subTab === 'autonomy'}
          className={`btn ${subTab === 'autonomy' ? 'btn-primary' : ''}`}
          onClick={() => setSubTab('autonomy')}
        >
          <Sparkles size={13} /> Autonomy &amp; Hobbies
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={subTab === 'vault'}
          className={`btn ${subTab === 'vault' ? 'btn-primary' : ''}`}
          onClick={() => setSubTab('vault')}
        >
          <KeyRound size={13} /> Vault &amp; Endpoints
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={subTab === 'federation'}
          className={`btn ${subTab === 'federation' ? 'btn-primary' : ''}`}
          onClick={() => setSubTab('federation')}
        >
          <Globe size={13} /> Federation &amp; MCP
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={subTab === 'ops'}
          className={`btn ${subTab === 'ops' ? 'btn-primary' : ''}`}
          onClick={() => setSubTab('ops')}
        >
          <Settings2 size={13} /> Backups, Sandboxes &amp; Doctor
        </button>
      </div>

      <div style={{ flex: 1, minHeight: 0, overflow: 'hidden' }}>
        {subTab === 'identity' && (
          <IdentityProfileStudio onRefreshStatus={onRefreshStatus} onToast={onToast} />
        )}
        {subTab === 'models' && (
          <ModelsTiersStudio onRefreshStatus={onRefreshStatus} onToast={onToast} />
        )}
        {subTab === 'autonomy' && (
          <AutonomyHobbiesStudio onRefreshStatus={onRefreshStatus} onToast={onToast} />
        )}
        {subTab === 'vault' && <VaultEndpointsStudio onToast={onToast} />}
        {subTab === 'federation' && (
          <FederationMcpStudio
            status={status}
            onRefreshStatus={onRefreshStatus}
            onToast={onToast}
          />
        )}
        {subTab === 'ops' && (
          <OpsStudio
            status={status}
            onOpenAutonomy={() => setSubTab('autonomy')}
            onToast={onToast}
          />
        )}
      </div>
    </div>
  );
};
