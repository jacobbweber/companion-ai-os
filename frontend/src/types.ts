export type ThemeId = 'storm' | 'loftra' | 'obsidian' | 'orbital' | 'graphite' | 'daylight';

export type StudioId =
  | 'chat'
  | 'boards'
  | 'calendar'
  | 'wiki'
  | 'jobs'
  | 'system';

export interface WorkspaceInfo {
  project_id: string;
  project_root: string;
  is_self_project: boolean;
}

export interface ConnectedPartnerSummary {
  partner_id: string;
  label: string;
  online: boolean;
  latency_ms: number;
  peer_profile: {
    companion_name?: string;
    version?: string;
    hostname?: string;
    os_platform?: string;
    cpu_count?: number;
    ram_gb?: number;
    gpu_summary?: string;
    fast_model?: string;
    reasoning_model?: string;
    trust_mode?: string;
    wiki_read_enabled?: boolean;
  };
}

export interface SessionNudge {
  id?: number;
  nudge_id?: number;
  session_id?: string;
  category: string;
  ref_id?: string;
  message: string;
  created_at?: string;
}

export interface ActiveWebJob {
  job_id: string;
  mode: string;
  message: string;
  status: string;
  elapsed_seconds: number;
  created_at: string;
  workflow_state: string;
  active_phase: string;
  active_tool: string;
  stream_text: string;
  stream_thinking: string;
  passed: boolean | null;
  awaiting_approval: boolean;
  response: string;
  error: string;
}

export interface WebStatus {
  ok: boolean;
  csrf_token: string;
  companion_name: string;
  user_name: string;
  tone: string;
  onboarded?: boolean;
  companion_tone?: string;
  companion_soul?: string;
  user_profile?: string;
  data_dir?: string;
  active_model_tier: string;
  fast_model: string;
  reasoning_model: string;
  hobby_mode: string;
  self_dev_mode?: string;
  self_dev_enabled?: boolean;
  self_dev_locked?: boolean;
  self_dev_lock_reason?: string;
  operating_quadrant?: OperatingQuadrant;
  unseen_hobbies_count?: number;
  workspace: WorkspaceInfo;
  busy: boolean;
  active_action: string;
  busy_seconds: number;
  model_swap: { active?: boolean; target_tier?: string; detail?: string } | null;
  active_job: ActiveWebJob | null;
  lan_url: string;
  workflow: { id?: number; goal?: string; current_state?: string; metadata?: Record<string, unknown> } | null;
  awaiting_approval: boolean;
  workflow_hud: string;
  pending_tasks: Array<{ id: number; description: string; status: string; priority: number }>;
  pending_backlog: Array<{ id: number; description: string; category: string; status: string }>;
  recent_savepoints: Array<{ hash?: string; subject?: string; time?: string } | string>;
  trust_mode: 'strict' | 'balanced' | 'autonomous' | string;
  privacy_mode?: 'hybrid' | 'local_only' | 'cloud_allowed' | string;
  provider_count?: number;
  pending_approvals_count: number;
  pending_approvals?: ApprovalRequest[];
  background_jobs_active: number;
  hosted_mcp_enabled: boolean;
  session_nudges?: SessionNudge[];
  agile_summary?: {
    active_sprint_title: string;
    open_cards: number;
    aging_count: number;
  };
  education_summary?: {
    active_course_id: string;
    active_course_title: string;
    mastery_pct: number;
    due_total: number;
    course_count: number;
  };
  connected_partners: ConnectedPartnerSummary[];
}

export interface ChatAttachment {
  id: string;
  filename: string;
  mime_type: string;
  kind: 'image' | 'pdf' | 'office' | 'data' | 'text' | 'code' | string;
  size_bytes: number;
  size_human: string;
  extracted_chars: number;
  meta_label: string;
  wiki_resource_path: string;
  width: number;
  height: number;
  raw_url: string;
  created_at?: string;
}

export interface ChatMessage {
  id: number | string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  thinking?: string;
  timestamp: string;
  session_tag?: string;
  activePhase?: string;
  activeTool?: string;
  streaming?: boolean;
}

export interface PromptTemplate {
  name: string;
  category: string;
  description: string;
  template: string;
  updated_at?: string;
}

export interface ArchiveSearchHit {
  id: number;
  tier: 'Hot' | 'Cold Archive' | string;
  role: string;
  content: string;
  timestamp: string;
  session_tag?: string;
}

export interface WikiNoteSummary {
  rel_path: string;
  stem: string;
  folder: string;
  domain: string;
  topic?: string;
  title: string;
  summary?: string;
  status?: string;
  template?: string;
  document_type?: string;
  author: 'user' | 'companion' | string;
  tags: string[];
  ontology?: string[];
  content_hash: string;
  updated_at: string;
  last_verified_at?: string;
  review_after_days?: number;
  days_since_verified?: number;
  overdue_days?: number;
  stale?: boolean;
  excerpt: string;
  match_kind?: 'direct' | 'ontology_bridge' | string;
  shared_ontology?: string[];
  via_note?: string;
}

export interface WikiNoteDetail {
  ok: boolean;
  path: string;
  title: string;
  summary?: string;
  note_summary?: string;
  status?: string;
  template?: string;
  author: string;
  domain: string;
  topic?: string;
  tags: string[];
  ontology?: string[];
  supersedes?: string[];
  superseded_by?: string;
  frontmatter?: Record<string, unknown>;
  content_hash: string;
  body: string;
  raw: string;
  wikilinks: string[];
  backlinks: string[];
  updated_at?: string;
  last_verified_at?: string;
  review_after_days?: number;
  days_since_verified?: number;
  stale?: boolean;
  error?: string;
}

export interface WikiDiffRow {
  kind: 'hunk' | 'context' | 'add' | 'del' | 'change';
  old_ln: number | null;
  new_ln: number | null;
  old_text: string;
  new_text: string;
}

export interface WikiDiffFile {
  title: string;
  old_label: string;
  new_label: string;
  additions: number;
  deletions: number;
  identical: boolean;
  unified_diff: string;
  rows: WikiDiffRow[];
}

export interface WikiDiffPayload {
  ok: boolean;
  mode?: 'lineage_diff' | 'approval_preview' | 'draft_preview' | string;
  path?: string;
  compare_to?: string;
  approval_id?: string;
  action_name?: string;
  files: WikiDiffFile[];
  total_additions: number;
  total_deletions: number;
  unified_diff: string;
  summary?: string;
  error?: string;
}

export interface WikiTemplateSpec {
  slug: string;
  title: string;
  document_type: string;
  when_to_use: string;
  required_sections: string[];
  min_words?: number;
  max_words?: number;
  skeleton: string;
  path?: string;
}

export interface CurationCandidate {
  path: string;
  title: string;
  domain: string;
  author: string;
  template: string;
  overlap_score: number;
  must_read: boolean;
  tags: string[];
  ontology: string[];
  summary: string;
}

export interface CurationInspection {
  ok: boolean;
  curation_id: string;
  staged_notes: Array<{
    path: string;
    title: string;
    template: string;
    tags: string[];
    ontology: string[];
    word_count: number;
  }>;
  overlap_candidates: CurationCandidate[];
  required_reads: string[];
  ontology_bridges: Array<{
    rel_path: string;
    title: string;
    domain: string;
    shared_ontology: string[];
  }>;
  recommended_templates: Array<{
    slug: string;
    title: string;
    document_type: string;
    required_sections: string[];
    when_to_use: string;
  }>;
  domains: Record<string, { note_count: number; has_hub: boolean; sample_notes: string[] }>;
  hub_recommendations: string[];
  approved_domains: string[];
  approved_ontology: string[];
  summary?: string;
  error?: string;
}

export interface CurationHistoryEntry {
  tx_id: string;
  curation_id: string;
  strategy: string;
  actor: string;
  summary: string;
  inbox_archives: Array<{ original_path: string; archive_path: string }>;
  knowledge_archives: Array<{ original_path: string; archive_path: string }>;
  output_paths: string[];
  repointed_links: string[];
  reverted: boolean;
  created_at: string;
}

export interface MindMapNode {
  id: string;
  label: string;
  folder: string;
  domain: string;
  author: string;
  template?: string;
  status?: string;
  tags: string[];
  ontology?: string[];
}

export interface MindMapEdge {
  source: string;
  target: string;
  ref: string;
}

export interface MindMapOntologyEdge {
  source: string;
  target: string;
  shared_ontology: string[];
}

export interface MindMapGraph {
  ok: boolean;
  nodes: MindMapNode[];
  edges: MindMapEdge[];
  ontology_edges?: MindMapOntologyEdge[];
  domains: string[];
  orphan_notes: string[];
  unresolved_links: Array<{ source: string; missing_ref: string }>;
  stats: {
    note_count: number;
    edge_count: number;
    ontology_edge_count?: number;
    domain_count: number;
  };
}

export interface BackgroundJob {
  job_id: string;
  title: string;
  goal: string;
  kind: string;
  starter: string;
  session_id: string;
  tier: string;
  status: 'queued' | 'running' | 'waiting_approval' | 'done' | 'failed' | 'cancelled' | 'interrupted' | string;
  envelope_summary: string;
  partial_output: string;
  overflow_wiki_path: string;
  approval_id: string;
  error: string;
  created_at: string;
  updated_at: string;
}

export interface ApprovalRequest {
  approval_id: string;
  category: string;
  action_name: string;
  target: string;
  summary: string;
  fingerprint: string;
  requester: string;
  status: 'waiting' | 'approved' | 'denied' | string;
  resolution_note?: string;
  created_at: string;
  resolved_at?: string;
}

export interface VaultSecretMeta {
  name: string;
  cred_type: string;
  description: string;
  allow_list: string[];
  is_set: boolean;
  expires_at: string;
  last_used_at: string;
  updated_at: string;
}

export interface MissingVaultSecret {
  name: string;
  cred_type: string;
  description?: string;
  needed_by: string | string[];
}

export interface VaultAuditEntry {
  id?: number;
  secret_name: string;
  caller_scope: string;
  outcome: string;
  detail: string;
  created_at: string;
}

export interface RemoteEndpoint {
  endpoint_id: string;
  label: string;
  protocol: 'ssh' | 'winrm' | 'http_api' | string;
  host: string;
  port: number;
  username: string;
  auth_mode: string;
  credential_ref: string;
  os_hint: string;
  tags: string[];
  last_probe_status: string;
  last_probe_ms: number;
  host_profile: {
    hostname?: string;
    os_kernel?: string;
    cpu_cores?: string | number;
    ram_total?: string;
    gpu_summary?: string;
  };
  updated_at: string;
}

export interface FederatedPartner {
  partner_id: string;
  label: string;
  url: string;
  credential_ref: string;
  enabled: boolean;
  online: boolean;
  latency_ms: number;
  last_seen_at: string;
  peer_profile: ConnectedPartnerSummary['peer_profile'];
  health: {
    trust_mode?: string;
    queued_or_running_jobs?: number;
    waiting_approvals?: number;
  };
}

export interface OutboundMcpServer {
  server_id: string;
  label: string;
  transport: 'stdio' | 'http' | string;
  command: string;
  args: string[];
  url: string;
  credential_ref: string;
  allowed_tools: string[];
  discovered_tools: Array<{ name: string; description?: string }>;
  tools_fingerprint: string;
  approved_fingerprint: string;
  is_approved: boolean;
  enabled: boolean;
}

export interface RoutineItem {
  routine_id: string;
  title: string;
  schedule_expr: string;
  target_kind: string;
  target_name: string;
  enabled: boolean;
  catch_up: boolean;
  next_run_at: string;
  last_run_at: string;
  last_status: string;
  missed_count: number;
}

export interface SnapshotBackup {
  snapshot_id: string;
  label: string;
  created_at: string;
  wiki_files: number;
  declared_secrets: string[];
  path: string;
}

export interface SandboxClone {
  sandbox_id: string;
  label: string;
  copied_files: number;
  path: string;
  source_root?: string;
}

export interface TelemetryPayload {
  ok: boolean;
  kpis: {
    total_traces?: number;
    error_rate_pct?: number;
    avg_duration_ms?: number;
    total_spans?: number;
    tool_calls?: number;
    llm_calls?: number;
  };
  selected_trace_id: string;
  traces: Array<{
    trace_id: string;
    trace_type: string;
    name: string;
    status: string;
    duration_ms: number;
    span_count: number;
    error_count: number;
    created_at: string;
  }>;
  spans: Array<{
    id: number;
    span_type: string;
    name: string;
    status: string;
    offset_ms: number;
    duration_ms: number;
    detail: string;
  }>;
  logs: Array<{
    id: number;
    level: string;
    component: string;
    trace_id: string;
    message: string;
    created_at: string;
  }>;
}

export interface AgileBoard {
  board_id: string;
  title: string;
  kind: 'life' | 'work' | 'project' | 'software' | string;
  project_id: string;
  description: string;
  archived: boolean;
  open_cards: number;
}

export interface AgileChecklistItem {
  id: string;
  text: string;
  done: boolean;
}

export interface AgileEpic {
  epic_id: string;
  board_id: string;
  board_title?: string;
  title: string;
  horizon: 'now' | 'next' | 'later' | 'someday' | string;
  status: string;
  start_date: string;
  target_date: string;
  wiki_path: string;
  summary: string;
  card_count?: number;
  done_card_count?: number;
  progress_pct?: number;
  effective_start_date?: string;
  roadmap_end_date?: string;
  created_at?: string;
  updated_at?: string;
}

export interface AgileSprint {
  sprint_id: string;
  title: string;
  goal_summary: string;
  start_date: string;
  end_date: string;
  status: string;
  weekday_capacity_slots: number;
  weekend_capacity_slots: number;
  retro_notes: string;
}

export interface AgileCard {
  card_id: string;
  board_id: string;
  board_title: string;
  epic_id: string;
  epic_title: string;
  sprint_id: string;
  title: string;
  description: string;
  status: 'backlog' | 'sprint_todo' | 'in_progress' | 'blocked' | 'verify' | 'done' | string;
  horizon: 'now' | 'next' | 'later' | 'someday' | string;
  priority: number;
  effort_size: 'quick' | 'half_day' | 'full_day' | 'multi_day' | string;
  day_slot: 'weekday' | 'weekend' | 'any' | string;
  slot_points: number;
  planned_date: string;
  due_date: string;
  blocked_reason: string;
  depends_on: string[];
  unresolved_deps: Array<{ card_id: string; title: string }>;
  tags: string[];
  checklist: AgileChecklistItem[];
  checklist_done: number;
  checklist_total: number;
  rollover_count: number;
  age_days: number;
  untouched_days: number;
  is_aging: boolean;
  workflow_id?: number | null;
}

export interface AgileCapacity {
  has_active_sprint: boolean;
  sprint?: AgileSprint;
  weekday_used: number;
  weekday_capacity: number;
  weekend_used: number;
  weekend_capacity: number;
  any_used: number;
  total_used?: number;
  total_capacity?: number;
  done_count: number;
  total_count: number;
  over_capacity: boolean;
  warnings: string[];
}

export interface AgileBoardView {
  ok: boolean;
  board_id: string;
  boards: AgileBoard[];
  epics: AgileEpic[];
  active_sprint: AgileSprint | null;
  capacity: AgileCapacity;
  cards: AgileCard[];
  all_cards: AgileCard[];
  backlog_cards: AgileCard[];
  columns: Record<string, AgileCard[]>;
  horizons: Record<string, AgileCard[]>;
  aging_cards: AgileCard[];
  summary?: string;
}

export interface CalendarItem {
  id: string;
  date: string;
  time: string;
  source_type: 'event' | 'sprint_task' | 'deadline' | 'routine' | 'study_review' | 'epic_milestone' | string;
  kind: string;
  title: string;
  all_day: boolean;
  board_id?: string;
  card_id?: string;
  epic_id?: string;
  status?: string;
  effort_size?: string;
  routine_id?: string;
  schedule_expr?: string;
  count?: number;
  notes?: string;
}

export interface CalendarWindow {
  ok: boolean;
  today: string;
  start_date: string;
  end_date: string;
  sprints: AgileSprint[];
  active_sprint: AgileSprint | null;
  epics?: AgileEpic[];
  items: CalendarItem[];
  by_date: Record<string, CalendarItem[]>;
}

export interface EducationModule {
  slug: string;
  title: string;
  objective: string;
  stage: 'priming' | 'dual_coding' | 'elaboration' | 'retrieval' | 'mastered' | string;
  wiki_path: string;
}

export interface EducationCourse {
  course_id: string;
  title: string;
  domain: string;
  goal_summary: string;
  current_module_idx: number;
  current_module: EducationModule | null;
  modules: EducationModule[];
  total_modules: number;
  mastered_modules: number;
  total_items: number;
  due_count: number;
  mastery_pct: number;
  active: boolean;
}

export interface FlashcardItem {
  item_id: string;
  topic: string;
  prompt: string;
  answer: string;
  explanation?: string;
  srs_box: number;
}

export interface FlashcardDeckPayload {
  course_id: string;
  deck_title: string;
  cards: FlashcardItem[];
}

export interface ProviderPreset {
  id: string;
  label: string;
  dialect: 'openai_compat' | 'anthropic_messages' | 'gemini_native' | string;
  base_url: string;
  is_local: boolean;
  credential_ref: string;
  default_context: number;
  models: string[];
}

export interface DiscoveredModelEntry {
  id: string;
  label: string;
  loaded: boolean;
  context_window: number;
}

export interface LlmProviderEntry {
  provider_id: string;
  label: string;
  preset: string;
  dialect: 'openai_compat' | 'anthropic_messages' | 'gemini_native' | string;
  base_url: string;
  credential_ref: string;
  credential_set: boolean;
  is_local: boolean;
  enabled: boolean;
  discovered_models: DiscoveredModelEntry[];
  suggested_models: string[];
  last_health: string;
  updated_at?: string;
}

export interface TierRouteEntry {
  tier: string;
  provider_id: string;
  provider_label: string;
  dialect: string;
  base_url: string;
  model: string;
  context_window_tokens: number;
  temperature: number;
  max_tokens: number;
  reasoning_effort: '' | 'low' | 'medium' | 'high' | string;
  fallback_provider_id: string;
  fallback_model: string;
  is_overridden: boolean;
  is_dedicated: boolean;
  privacy_blocked: boolean;
}

export interface ModelMatrixSnapshot {
  ok: boolean;
  active_mode: string;
  privacy_mode: 'hybrid' | 'local_only' | 'cloud_allowed' | string;
  compaction: {
    trigger_pct: number;
    target_pct: number;
    keep_recent_messages: number;
  };
  presets: ProviderPreset[];
  providers: LlmProviderEntry[];
  tiers: TierRouteEntry[];
  vault_secrets: Array<{ name: string; cred_type: string; is_set: boolean }>;
  summary?: string;
  error?: string;
}

export interface OperatingQuadrant {
  id: 'quiet_daily_driver' | 'personal_daily_driver' | 'deliberate_self_dev' | 'autonomous_lab' | string;
  code: 'Q1' | 'Q2' | 'Q3' | 'Q4' | string;
  title: string;
  summary: string;
  hobby_enabled: boolean;
  self_dev_enabled: boolean;
  core_read_only: boolean;
  eligible_tracks: string[];
}

export interface HobbyCatalogItem {
  name: string;
  title: string;
  track: 'personal' | 'self_dev' | string;
  discipline: string;
  description: string;
  preferred_tier: string;
  eligible: boolean;
  locked_reason: string;
  is_next: boolean;
}

export interface HobbyJournalEntry {
  id: number;
  hobby_name: string;
  status: string;
  summary: string;
  checkpoint_id: string;
  doctor_passed: boolean;
  seen_by_user?: boolean;
  created_at: string;
}

export interface OperatingModesSnapshot {
  ok: boolean;
  hobby_enabled: boolean;
  hobby_mode: 'on' | 'off' | string;
  self_dev_enabled: boolean;
  self_dev_mode: 'on' | 'off' | string;
  self_dev_locked: boolean;
  self_dev_lock_reason: string;
  immutable_core: boolean;
  quadrant: OperatingQuadrant;
  idle_minutes: number;
  cooldown_minutes: number;
  max_iters: number;
  next_hobby: string;
  catalog: HobbyCatalogItem[];
  journal: HobbyJournalEntry[];
  unseen_count: number;
  morning_briefing: string;
  summary?: string;
  error?: string;
}

export interface IdentitySnapshot {
  ok: boolean;
  onboarded: boolean;
  companion_name: string;
  companion_soul: string;
  companion_tone: string;
  valid_tones: Record<string, string>;
  user_name: string;
  user_profile: string;
  role: string;
  communication_style: string;
  workspace: {
    project_id: string;
    project_root: string;
    is_self_project: boolean;
    self_root: string;
    known_projects: Record<string, string>;
  };
  paths: {
    data_dir: string;
    db_path: string;
    identity_md_path: string;
    memory_md_path: string;
    user_skills_dir: string;
  };
  summary?: string;
  error?: string;
}
