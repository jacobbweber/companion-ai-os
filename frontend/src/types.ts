export type ThemeId = 'obsidian' | 'daylight' | 'graphite' | 'storm' | 'loftra' | 'orbital';

export type SurfaceId =
  | 'chat'
  | 'boards'
  | 'calendar'
  | 'knowledge'
  | 'automations'
  | 'customizations';

export type SettingsSectionId =
  | 'general'
  | 'appearance'
  | 'identity'
  | 'models'
  | 'vault'
  | 'federation'
  | 'ops';

export interface WorkspaceInfo {
  project_id: string;
  project_root: string;
  is_self_project: boolean;
  projects_dir?: string;
}

export interface TriggerPolicySpec {
  mode: 'manual' | 'scheduled' | 'on_idle' | string;
  schedule_expr?: string;
  idle_min_seconds?: number;
  allowed_window?: string;
  cooldown_seconds?: number;
  precheck_name?: string;
}

export interface OperatingQuadrant {
  quadrant: 'interactive_standard' | 'autonomous_partner' | string;
  label: string;
  background_autonomy: boolean;
  immutable_app?: boolean;
  summary: string;
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

export interface ToolStepMeta {
  tool: string;
  summary: string;
  ok?: boolean;
  duration_ms?: number;
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
  tool_steps?: ToolStepMeta[];
  event_cursor?: number;
  passed: boolean | null;
  awaiting_approval: boolean;
  response: string;
  error: string;
}

export interface WebStatus {
  ok: boolean;
  csrf_token: string;
  app_version?: string;
  asset_hash?: string;
  companion_name: string;
  user_name: string;
  tone: string;
  onboarded?: boolean;
  companion_tone?: string;
  companion_soul?: string;
  user_profile?: string;
  data_dir?: string;
  projects_dir?: string;
  active_model_tier: string;
  fast_model: string;
  reasoning_model: string;
  background_autonomy?: 'on' | 'off' | string;
  immutable_app?: boolean;
  operating_quadrant?: OperatingQuadrant;
  unseen_autonomy_count?: number;
  focus_project_id?: string;
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
    blocked_count?: number;
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
  tool_steps?: ToolStepMeta[];
  duration_ms?: number;
  model?: string;
  timestamp: string;
  session_tag?: string;
  activePhase?: string;
  activeTool?: string;
  streaming?: boolean;
  is_error?: boolean;
  error_code?: string;
  retry_payload?: {
    text: string;
    attachmentIds: string[];
    execMode: 'chat' | 'plan' | 'goal' | 'job';
  };
}

export interface ChatDeliveryFailure {
  id: string;
  title: string;
  detail: string;
  statusCode?: number;
  errorCode?: string;
  authRequired?: boolean;
  text: string;
  attachmentIds: string[];
  execMode: 'chat' | 'plan' | 'goal' | 'job';
  timestamp: string;
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
  frontmatter?: Record<string, unknown>;
  content_hash: string;
  updated_at: string;
  last_verified_at?: string;
  review_after_days?: number;
  days_since_verified?: number;
  overdue_days?: number;
  stale?: boolean;
  excerpt: string;
  match_kind?: 'direct' | 'frontmatter' | 'ontology_bridge' | string;
  matched_fields?: string[];
  shared_ontology?: string[];
  via_note?: string;
}

export interface WikiTreeNode {
  kind: 'folder' | 'file';
  name: string;
  rel_path: string;
  folder: string;
  file_count?: number;
  children?: WikiTreeNode[];
  title?: string;
  domain?: string;
  topic?: string;
  author?: string;
  template?: string;
  document_type?: string;
  status?: string;
  tags?: string[];
  ontology?: string[];
  stale?: boolean;
  system_file?: boolean;
  is_asset?: boolean;
  asset_url?: string;
}

export interface WikiFacetItem {
  value: string;
  count: number;
}

export interface WikiFrontmatterFacets {
  domains: WikiFacetItem[];
  tags: WikiFacetItem[];
  ontology: WikiFacetItem[];
  templates: WikiFacetItem[];
  document_types: WikiFacetItem[];
  authors: WikiFacetItem[];
  statuses: WikiFacetItem[];
  keys: string[];
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
  mode?: string;
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
  project_id?: string;
  tier: string;
  status: 'queued' | 'running' | 'waiting_approval' | 'done' | 'failed' | 'cancelled' | 'interrupted' | string;
  envelope_summary: string;
  partial_output: string;
  full_output?: string;
  overflow_wiki_path: string;
  approval_id: string;
  error: string;
  created_at: string;
  updated_at?: string;
  started_at?: string;
  finished_at?: string;
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
  payload_json?: string;
  payload?: Record<string, unknown>;
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
  discovered_tools: Array<{ name: string; description?: string; inputSchema?: Record<string, unknown> }>;
  tools_fingerprint: string;
  approved_fingerprint: string;
  is_approved: boolean;
  enabled: boolean;
}

export interface RoutineItem {
  routine_id: string;
  title: string;
  schedule_expr: string;
  trigger_policy: TriggerPolicySpec;
  target_kind: string;
  target_name: string;
  inputs?: Record<string, unknown>;
  enabled: boolean;
  catch_up: boolean;
  next_run_at: string;
  last_run_at: string;
  last_job_id?: string;
  last_status: string;
  missed_count: number;
  updated_at?: string;
}

export interface AutonomyJournalEntry {
  id: number;
  entry_kind: 'routine' | 'goal_slice' | string;
  entry_name?: string;
  target_id?: string;
  status: string;
  summary: string;
  checkpoint_id?: string;
  doctor_passed?: boolean;
  seen_by_user?: boolean;
  created_at: string;
}

export interface AutonomySnapshot {
  ok: boolean;
  background_autonomy: boolean;
  immutable_app?: boolean;
  operating_quadrant: OperatingQuadrant;
  routines: RoutineItem[];
  journal: AutonomyJournalEntry[];
  summary?: string;
  error?: string;
}

export interface SnapshotBackup {
  snapshot_id: string;
  label: string;
  created_at: string;
  wiki_files: number;
  declared_secrets: string[];
  path: string;
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

export interface RepoProbeResult {
  ok: boolean;
  exists: boolean;
  is_git_repo: boolean;
  can_init?: boolean;
  projects_dir?: string;
  repo_root?: string;
  branch?: string;
  clean?: boolean;
  changed_count?: number;
  remote_url?: string;
  stack_summary?: string;
  rules_files?: string[];
  summary?: string;
  error?: string;
}

export interface WorkItemDeliverable {
  kind?: 'code_worktree' | 'personal_deliverable' | string;
  worker_status?: string;
  branch_name?: string;
  base_branch?: string;
  worktree_path?: string;
  commit_sha?: string;
  changed_files?: string[];
  diff_stat?: string;
  unified_diff?: string;
  diff_payload?: WikiDiffPayload;
  summary?: string;
  test_summary?: string;
  screenshot_ids?: string[];
  job_id?: string;
  has_remote?: boolean;
  remote_url?: string;
  pushed?: boolean;
  pr_url?: string;
  pr_number?: number;
  merged?: boolean;
  merged_commit?: string;
  merged_into?: string;
  merged_at?: string;
  revision_history?: Array<{ feedback: string; requested_at: string }>;
  error?: string;
  updated_at?: string;
}

export interface ProjectGraphEntry {
  project_id: string;
  slug: string;
  title: string;
  goal_summary: string;
  kind: 'goal' | 'project' | 'life' | 'work' | 'software' | string;
  status: string;
  workspace_path?: string;
  delivery_policy?: 'local_only' | 'remote_branch_pr' | 'ask_me' | string;
  git_credential_ref?: string;
  max_parallel_workers?: number;
  test_command?: string;
  preview_command?: string;
  trigger_policy: TriggerPolicySpec;
  wiki_hub_path?: string;
  metadata?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
}

export interface WorkItemVerificationSpec {
  kind?: 'assertion' | 'evidence_file' | 'check_command' | 'script_validator' | 'domain_validator' | 'human_gate' | string;
  target?: string;
  contains?: string;
  description?: string;
  params?: Record<string, unknown>;
}

export interface WorkItemChecklistEntry {
  id?: string;
  text: string;
  done?: boolean;
}

export interface WorkItem {
  id: number;
  item_id: string;
  card_id: string;
  project_id: string;
  milestone_id: string;
  epic_id: string;
  timebox_id: string;
  sprint_id: string;
  workflow_id?: number | null;
  item_kind: string;
  title: string;
  description: string;
  category?: string;
  status: 'backlog' | 'ready' | 'in_progress' | 'blocked' | 'verify' | 'done' | string;
  column_key: 'backlog' | 'ready' | 'in_progress' | 'blocked' | 'verify' | 'done' | string;
  owner: 'companion' | 'human' | 'collaborative' | string;
  stage_kind: 'OBSERVE_DATA' | 'CURATE_DIKW' | 'FORGE_CAPABILITY' | 'DELIVER_AND_VERIFY' | string;
  priority: number;
  depends_on: string[];
  verification: WorkItemVerificationSpec;
  dag_name: string;
  checkpoint_id: string;
  acceptance_criteria: string;
  verification_result: string;
  estimate_minutes: number;
  planned_date: string;
  due_date: string;
  checklist: Array<string | WorkItemChecklistEntry>;
  tags: string[];
  active_job_id?: string;
  worktree_path?: string;
  branch_name?: string;
  deliverable?: WorkItemDeliverable;
  created_at?: string;
  updated_at?: string;
}

export interface MilestoneEntry {
  milestone_id: string;
  project_id: string;
  title: string;
  description: string;
  stage_kind: string;
  status: string;
  target_date: string;
  sort_order?: number;
  item_count?: number;
  done_count?: number;
  progress_pct?: number;
  items?: WorkItem[];
}

export interface TimeboxEntry {
  timebox_id: string;
  sprint_id: string;
  project_id: string;
  title: string;
  goal: string;
  start_date: string;
  end_date: string;
  status: string;
  retro_notes: string;
}

export interface BoardColumnProjection {
  col_key: string;
  title: string;
  cards: WorkItem[];
}

export interface ProjectBoardProjection {
  ok: boolean;
  project: ProjectGraphEntry;
  board: ProjectGraphEntry;
  boards: ProjectGraphEntry[];
  projects_dir?: string;
  columns: BoardColumnProjection[];
  cards: WorkItem[];
  items?: WorkItem[];
  milestones: MilestoneEntry[];
  epics: MilestoneEntry[];
  active_sprint: TimeboxEntry | null;
  sprints: TimeboxEntry[];
  counts: {
    total: number;
    companion_ready: number;
    human_pending: number;
    blocked: number;
    done: number;
  };
  summary?: string;
}

export interface CalendarItem {
  id: string;
  date: string;
  time: string;
  end_time?: string;
  end_at?: string;
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
  sprints?: TimeboxEntry[];
  timeboxes?: TimeboxEntry[];
  active_sprint: TimeboxEntry | null;
  epics?: MilestoneEntry[];
  milestones?: MilestoneEntry[];
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
  max_concurrency?: number;
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
  max_concurrency?: number;
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

export interface TierCalibrationResult {
  ok: boolean;
  tier: string;
  model: string;
  base_url: string;
  streaming_ok: boolean;
  thinking_detected: boolean;
  tool_call_ok: boolean;
  latency_ms: number;
  ttft_ms: number;
  detail: string;
  summary?: string;
  error?: string;
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

export interface SkillPackSummary {
  name: string;
  description: string;
  required_tools: string;
  path: string;
  scope: 'core' | 'user' | string;
  references: string;
  dags: string;
  scripts: string;
}

export interface VerifiedDagSummary {
  name: string;
  skill: string;
  description: string;
  path: string;
  nodes: string[];
  input_schema: Record<string, string>;
}

export interface DagRunSummary {
  run_id: string;
  dag_name: string;
  status: 'completed' | 'waiting_approval' | 'failed' | 'running' | string;
  paused_node_id: string;
  approval_id: string;
  error: string;
  created_at: string;
  updated_at: string;
}

export interface DagNodeRunEntry {
  node_id: string;
  status: string;
  attempt?: number;
  attempts?: number;
  duration_ms?: number;
  output?: unknown;
  output_json?: string;
  error?: string;
}

export interface CustomizationsSnapshot {
  ok: boolean;
  skills: SkillPackSummary[];
  dags: VerifiedDagSummary[];
  dag_runs: DagRunSummary[];
  summary?: string;
}

export type AuxInspectorTarget =
  | { kind: 'work_item'; itemId: string; projectId?: string }
  | { kind: 'project'; projectId: string; createNew?: boolean }
  | { kind: 'routine'; routineId?: string; createNew?: boolean }
  | { kind: 'skill'; skillName: string; asset?: string }
  | { kind: 'dag'; dagName: string }
  | { kind: 'dag_run'; runId: string }
  | { kind: 'wiki_note'; notePath: string }
  | { kind: 'job'; jobId: string }
  | { kind: 'approvals' }
  | null;
