/**
 * Hand-written mirror of `packages/proto/jarvis_proto/*.py` (pydantic v2 models).
 *
 * Rules kept in sync by hand until `python -m jarvis_proto.schema` output is wired in:
 *  - field names are identical to the Python attribute names;
 *  - `X | None` becomes `X | null` (pydantic always emits the key, so it is not optional);
 *  - `datetime` is an ISO-8601 string; `dict[str, Any]` is `Record<string, unknown>`;
 *  - `StrEnum` becomes a string-literal union; discriminators are the `type` literal.
 */

// ---- messages.py ---------------------------------------------------------------------

export type Role = 'system' | 'user' | 'assistant' | 'tool';

export interface ToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export interface Message {
  id: string | null;
  conversation_id: string | null;
  run_id: string | null;
  role: Role;
  content: string;
  reasoning: string | null;
  tool_calls: ToolCall[];
  tool_call_id: string | null;
  name: string | null;
  partial: boolean;
  created_at: string;
}

export type ToolResultKind = 'data' | 'empty' | 'partial' | 'error';

export interface ToolResult {
  kind: ToolResultKind;
  text: string;
  count: number | null;
  total: number | null;
  cursor: string | null;
  error: string | null;
}

// ---- runs.py -------------------------------------------------------------------------

export type ThinkLevel = 'low' | 'medium' | 'high';
export const THINK_LEVELS: readonly ThinkLevel[] = ['low', 'medium', 'high'];

export type RunKind = 'chat' | 'scheduled' | 'collab' | 'triage' | 'meeting' | 'system';

export type RunStatus =
  | 'queued'
  | 'running'
  | 'waiting_user'
  | 'cancelling'
  | 'done'
  | 'failed'
  | 'cancelled'
  | 'interrupted';

export const TERMINAL_STATUSES: ReadonlySet<RunStatus> = new Set(['done', 'failed', 'cancelled']);

export function isTerminal(status: RunStatus): boolean {
  return TERMINAL_STATUSES.has(status);
}

export interface RunBudget {
  max_steps: number;
  max_tokens: number;
  max_seconds: number;
}

export interface ModelUsage {
  prompt_tokens: number;
  completion_tokens: number;
  calls: number;
  ttft_ms: number | null;
  duration_ms: number;
}

export type PlanStepStatus = 'pending' | 'in_progress' | 'done' | 'skipped';

export interface PlanStep {
  title: string;
  status: PlanStepStatus;
  note: string | null;
}

export interface Plan {
  goal: string;
  steps: PlanStep[];
}

export interface Run {
  id: string;
  conversation_id: string;
  kind: RunKind;
  status: RunStatus;
  input_text: string;
  plan: Plan | null;
  budget: RunBudget;
  priority: number;
  steps_used: number;
  usage: ModelUsage;
  last_seq: number;
  error: string | null;
  waiting_reason: string | null;
  /** Per-run thinking override; null = the role's setting. */
  think: boolean | null;
  think_level: ThinkLevel | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export type ConversationKind = 'chat' | 'scheduled' | 'collab' | 'triage' | 'meeting' | 'archive';

export interface Conversation {
  id: string;
  kind: ConversationKind;
  title: string;
  folder_key: string | null;
  folder_label: string | null;
  archived: boolean;
  unread: boolean;
  preview: string | null;
  message_count: number;
  created_at: string;
  updated_at: string;
}

// ---- settings.py ---------------------------------------------------------------------

export type Provider = 'ollama' | 'vllm' | 'fake';

export type RoleName = 'chat' | 'planner' | 'classifier' | 'judge' | 'triage';

export const ROLE_NAMES: readonly RoleName[] = ['chat', 'planner', 'classifier', 'judge', 'triage'];
export const RUN_KINDS: readonly RunKind[] = ['chat', 'collab', 'scheduled', 'triage', 'meeting', 'system'];

export interface ModelSpec {
  provider: Provider;
  base_url: string;
  model: string;
  think: boolean;
  /** Only meaningful when `think` is on; the server drops it otherwise. null = model default. */
  think_level: ThinkLevel | null;
  num_ctx: number | null;
  temperature: number;
  max_tokens: number | null;
  timeout_s: number;
  keep_alive: string | null;
}

export type McpTransport = 'stdio' | 'streamable_http';

/** An external MCP server whose tools appear as `<name>.<tool>`. */
export interface McpServerSpec {
  /** ^[a-z][a-z0-9_-]{0,31}$, unique, never "jarvis". */
  name: string;
  transport: McpTransport;
  /** stdio only; the literal "{python}" means the core's own interpreter. */
  command: string | null;
  args: string[];
  env: Record<string, string>;
  /** streamable_http only. */
  url: string | null;
  headers: Record<string, string>;
  enabled: boolean;
  timeout_s: number;
}

export interface TriageSettings {
  enabled: boolean;
  interval_min: number;
  /** Name of the MCP server (jarvis-host) that owns Outlook. */
  host: string;
  accounts: string[];
  demand_root: string;
  demand_prefixes: string[];
  /** {name, folder, rule} */
  categories: Record<string, string>[];
}

export type ToolExposure = 'auto' | 'flat' | 'facade';

export interface Settings {
  assistant_name: string;
  user_name: string;
  timezone: string;
  language_hint: string;
  roles: Record<RoleName, ModelSpec>;
  budgets: Record<RunKind, RunBudget>;
  mcp_servers: McpServerSpec[];
  max_concurrent_runs_per_endpoint: number;
  repeated_call_threshold: number;
  tool_exposure: ToolExposure;
  facade_threshold: number;
  history_token_budget: number;
  boards_context_chars: number;
  skill_max_chars: number;
  planning_enabled: boolean;
  kg_learning: boolean;
  triage: TriageSettings;
  stt_url: string | null;
  stt_languages: string[];
}

// ---- features.py ---------------------------------------------------------------------

export type NoteColor = 'yellow' | 'blue' | 'green' | 'pink' | 'grey';
export const NOTE_COLORS: readonly NoteColor[] = ['yellow', 'blue', 'green', 'pink', 'grey'];

export interface Board {
  id: string;
  name: string;
  position: number;
  note_count: number;
  created_at: string;
  updated_at: string;
}

export interface Note {
  id: string;
  board_id: string;
  text: string;
  color: NoteColor;
  from_message_id: string | null;
  position: number;
  created_at: string;
  updated_at: string;
}

export type EntityType = 'person' | 'org' | 'project' | 'place' | 'thing' | 'topic';
export const ENTITY_TYPES: readonly EntityType[] = ['person', 'org', 'project', 'place', 'thing', 'topic'];

export interface Entity {
  id: string;
  name: string;
  type: EntityType;
  summary: string;
  aliases: string[];
  mention_count: number;
  updated_at: string;
}

export interface Edge {
  src: string;
  dst: string;
  relation: string;
  weight: number;
  evidence: string | null;
}

export interface Mention {
  conversation_id: string | null;
  message_id: string | null;
  snippet: string | null;
  at: string;
}

export interface EdgeWithOther extends Edge {
  other: Entity;
}

export interface EntityDetail extends Entity {
  edges: EdgeWithOther[];
  mentions: Mention[];
}

export interface Graph {
  nodes: Entity[];
  edges: Edge[];
}

export interface Skill {
  name: string;
  description: string;
  triggers: string[];
  enabled: boolean;
  size: number;
  updated_at: string;
}

export type CatchUp = 'skip' | 'run_once';

export interface Schedule {
  id: string;
  name: string;
  prompt: string;
  cron: string | null;
  at: string | null;
  tz: string;
  enabled: boolean;
  catch_up: CatchUp;
  think: boolean | null;
  think_level: ThinkLevel | null;
  next_fire: string | null;
  last_fired_for: string | null;
  last_run_id: string | null;
  last_status: string | null;
  created_at: string;
  updated_at: string;
}

export interface ScheduleFire {
  schedule_id: string;
  scheduled_for: string;
  run_id: string | null;
  conversation_id: string | null;
  status: string | null;
}

export interface CollabKey {
  id: string;
  name: string;
  created_at: string;
  last_used_at: string | null;
}

export type MeetingStatus = 'recording' | 'summarising' | 'done' | 'failed';

export interface Meeting {
  id: string;
  conversation_id: string;
  title: string;
  host: string;
  status: MeetingStatus;
  started_at: string;
  ended_at: string | null;
  summary_run_id: string | null;
}

export interface MeetingSegmentModel {
  seq: number;
  t0: number;
  t1: number;
  text: string;
}

export interface MeetingFrameModel {
  seq: number;
  at: number;
  url: string;
  ocr: string | null;
}

export interface MeetingDetail extends Meeting {
  segments: MeetingSegmentModel[];
  frames: MeetingFrameModel[];
}

export interface TriageState {
  account: string;
  cursor: string | null;
  day: string | null;
  processed_today: number;
  routed_today: number;
  last_run_at: string | null;
  last_error: string | null;
}

// ---- tools.py ------------------------------------------------------------------------

export interface ToolSpec {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  read_only: boolean;
  destructive: boolean;
  idempotent: boolean;
  provider: string;
}

// ---- events.py: server → client ------------------------------------------------------

interface Base {
  ts: string;
}

export interface RunEventBase extends Base {
  run_id: string;
  conversation_id: string;
  seq: number;
}

export interface RunQueued extends RunEventBase {
  type: 'run.queued';
  kind: RunKind;
  input_preview: string;
  user_message_id: string | null;
}
export interface RunStarted extends RunEventBase {
  type: 'run.started';
}
export interface RunResumed extends RunEventBase {
  type: 'run.resumed';
  from_seq: number;
}
export interface RunWaitingUser extends RunEventBase {
  type: 'run.waiting_user';
  reason: string;
  call_id: string | null;
}
export interface RunDone extends RunEventBase {
  type: 'run.done';
  message_id: string | null;
  usage: ModelUsage;
  steps_used: number;
  summary: string | null;
}
export interface RunFailed extends RunEventBase {
  type: 'run.failed';
  error: string;
}
export interface RunCancelled extends RunEventBase {
  type: 'run.cancelled';
  partial_message_id: string | null;
}
export interface RunInterrupted extends RunEventBase {
  type: 'run.interrupted';
}

export interface PlanCreated extends RunEventBase {
  type: 'plan.created';
  plan: Plan;
}
export interface PlanStepStarted extends RunEventBase {
  type: 'plan.step_started';
  index: number;
  title: string;
}
export interface PlanStepDone extends RunEventBase {
  type: 'plan.step_done';
  index: number;
}

export interface ModelCall extends RunEventBase {
  type: 'model.call';
  role: string;
  provider: string;
  model: string;
  message_count: number;
  tool_count: number;
  think: boolean;
  think_level: ThinkLevel | null;
}
export interface ModelDelta extends RunEventBase {
  type: 'model.delta';
  kind: 'text' | 'reasoning';
  text: string;
}
export interface ModelDone extends RunEventBase {
  type: 'model.done';
  usage: ModelUsage;
  finish_reason: string | null;
  tool_call_count: number;
}

export interface ToolCallEvent extends RunEventBase {
  type: 'tool.call';
  call_id: string;
  name: string;
  arguments: Record<string, unknown>;
  read_only: boolean;
  idempotency_key: string;
}
export interface ToolResultEvent extends RunEventBase {
  type: 'tool.result';
  call_id: string;
  name: string;
  result: ToolResult;
  duration_ms: number;
}
export interface ToolConfirmRequested extends RunEventBase {
  type: 'tool.confirm_requested';
  call_id: string;
  name: string;
  arguments: Record<string, unknown>;
  reason: string;
}
export interface ToolConfirmResolved extends RunEventBase {
  type: 'tool.confirm_resolved';
  call_id: string;
  approved: boolean;
  note: string | null;
}

export interface GuardArmed extends RunEventBase {
  type: 'guard.armed';
  guard: string;
  detail: string;
}
export interface GuardConsumed extends RunEventBase {
  type: 'guard.consumed';
  guard: string;
  detail: string;
}
export type Verdict = 'continue' | 'nudge' | 'stop';
export interface JudgeVerdict extends RunEventBase {
  type: 'judge.verdict';
  verdict: Verdict;
  reason: string;
}

export interface ConversationUpdated extends Base {
  type: 'conversation.updated';
  conversation: Conversation;
}
export interface ConversationDeleted extends Base {
  type: 'conversation.deleted';
  conversation_id: string;
}
export interface MessageCreated extends Base {
  type: 'message.created';
  message: Message;
}
export interface RunUpdated extends Base {
  type: 'run.updated';
  run: Run;
}
export interface Pong extends Base {
  type: 'pong';
}

// features (Phases 2–7)
export interface ContextSkills extends RunEventBase {
  type: 'context.skills';
  names: string[];
}
export interface BoardChanged extends Base {
  type: 'board.changed';
  /** null = the board list itself changed. */
  board_id: string | null;
}
export interface KgChanged extends Base {
  type: 'kg.changed';
  entity_ids: string[];
}
export interface SkillsChanged extends Base {
  type: 'skills.changed';
}
export interface ScheduleChanged extends Base {
  type: 'schedule.changed';
  schedule_id: string | null;
}
export interface ToolsChanged extends Base {
  type: 'tools.changed';
  provider: string | null;
}
export interface MeetingSegment extends Base {
  type: 'meeting.segment';
  meeting_id: string;
  conversation_id: string;
  seq: number;
  t0: number;
  t1: number;
  text: string;
}
export interface MeetingChanged extends Base {
  type: 'meeting.changed';
  meeting_id: string;
  conversation_id: string;
  status: string;
}

export type FeatureEvent = BoardChanged | KgChanged | SkillsChanged | ScheduleChanged | ToolsChanged | MeetingSegment | MeetingChanged;

export type RunScopedEvent =
  | RunQueued
  | RunStarted
  | RunResumed
  | RunWaitingUser
  | RunDone
  | RunFailed
  | RunCancelled
  | RunInterrupted
  | PlanCreated
  | PlanStepStarted
  | PlanStepDone
  | ModelCall
  | ModelDelta
  | ModelDone
  | ToolCallEvent
  | ToolResultEvent
  | ToolConfirmRequested
  | ToolConfirmResolved
  | GuardArmed
  | GuardConsumed
  | JudgeVerdict
  | ContextSkills;

export type ServerEvent =
  | RunScopedEvent
  | ConversationUpdated
  | ConversationDeleted
  | MessageCreated
  | RunUpdated
  | Pong
  | FeatureEvent;

export type ServerEventType = ServerEvent['type'];

export function isRunScoped(event: ServerEvent): event is RunScopedEvent {
  return 'run_id' in event;
}

// ---- events.py: client → server ------------------------------------------------------

export interface RunCreateRequest {
  type: 'run.create';
  conversation_id: string | null;
  text: string;
  kind: RunKind;
  client_ref: string | null;
  /** Per-message thinking override; null = the chat role's configured setting. */
  think: boolean | null;
  think_level: ThinkLevel | null;
}
export interface RunCancelRequest {
  type: 'run.cancel';
  run_id: string;
}
export interface ToolConfirmRequest {
  type: 'tool.confirm';
  run_id: string;
  call_id: string;
  approved: boolean;
  note: string | null;
}
export interface Subscribe {
  type: 'subscribe';
  conversation_id: string;
}
export interface Unsubscribe {
  type: 'unsubscribe';
  conversation_id: string;
}
export interface Ping {
  type: 'ping';
}

export type ClientMessage =
  | RunCreateRequest
  | RunCancelRequest
  | ToolConfirmRequest
  | Subscribe
  | Unsubscribe
  | Ping;

// ---- REST-only shapes (server.py contract, not in jarvis_proto) -----------------------

export interface HealthResponse {
  ok: boolean;
  version: string;
}

export interface EndpointStatus {
  /** A `RoleName` in practice; typed loosely so an extra probe row cannot break parsing. */
  role: string;
  provider: string;
  base_url: string;
  model: string;
  think: boolean;
  ok: boolean;
  latency_ms: number | null;
  detail: string | null;
  models: string[];
}

/** One row per tool provider: "builtin" plus each MCP server. */
export interface ToolProviderStatus {
  name: string;
  tools: number;
  ok: boolean;
  error: string | null;
}

export interface StatusResponse {
  version: string;
  endpoints: EndpointStatus[];
  runs: { running: number; queued: number };
  tools: ToolProviderStatus[];
}

/** `GET /api/skills/{name}` */
export interface SkillContent {
  name: string;
  content: string;
}

/** `GET /api/conversations/{id}/summary` (null when nothing was compacted). */
export interface ConversationSummary {
  up_to_message_id: string;
  text: string;
}

/** `POST /api/stt` */
export interface SttResponse {
  text: string;
  language: string;
  backend: string;
  duration_ms: number;
}

/** `POST /api/schedules/{id}/run` */
export interface ScheduleRunResponse {
  run_id: string;
  conversation_id: string;
}

/** `POST /api/collab/keys` — the key is shown once. */
export interface CollabKeyCreated {
  key: string;
  id: string;
  name: string;
}

/** `GET /api/pair` */
export interface PairResponse {
  url: string;
  qr_svg: string;
}

/** `GET /api/whoami` */
export interface Whoami {
  owner: boolean;
  key_name?: string;
}
