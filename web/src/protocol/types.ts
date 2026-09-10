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
  /** Photos and files sent with this message (empty for everything else). */
  attachments: Attachment[];
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
  /** Prompt tokens the server answered from its prefix cache; the rest is what TTFT pays for. */
  cached_tokens: number;
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

/**
 * What the sidebar's activity dot says about a conversation. Derived by the server from the
 * runs table on every read, so it is right for every chat in the list — not just the open one,
 * whose runs are the only ones this client has actually loaded.
 */
export type ConversationActivity = 'idle' | 'running' | 'waiting';

/** A folder Arsen made himself, to file plain chats in (`Conversation.folder_id`). */
export interface ChatFolder {
  id: string;
  name: string;
  position: number;
  /** Unarchived chats filed here, and how many of those are unread. */
  conversation_count: number;
  unread_count: number;
  created_at: string;
  updated_at: string;
}

export type AttachmentKind = 'image' | 'document' | 'text' | 'email';

/** A photo, a file, pasted text or an email thread handed to Jarvis with a message. */
export interface Attachment {
  id: string;
  kind: AttachmentKind;
  name: string;
  mime: string;
  bytes: number;
  text: string | null;
  meta: Record<string, unknown>;
  created_at: string;
}

export interface Conversation {
  id: string;
  kind: ConversationKind;
  title: string;
  folder_key: string | null;
  folder_label: string | null;
  /** Arsen's own filing (a `ChatFolder` id); `folder_key`/`folder_label` above are the machine's. */
  folder_id: string | null;
  archived: boolean;
  unread: boolean;
  pinned: boolean;
  /** True while the title is machine-made; a rename by the user turns it off. */
  title_auto: boolean;
  /** A persona or standing rule for THIS chat only, added to its system message. */
  instructions: string;
  preview: string | null;
  message_count: number;
  activity: ConversationActivity;
  /**
   * A private chat: nothing from it is remembered anywhere else (no knowledge learned, no title
   * from its words, never a search hit, no preview, no notes/knowledge tools). Set when the chat
   * is opened, never later. Always has a `ttl_seconds`.
   */
  incognito: boolean;
  /** A disappearing chat: deleted by the core after this long idle. `null` = kept. */
  ttl_seconds: number | null;
  /** When the core will delete it (last message + ttl); `null` when kept. */
  expires_at: string | null;
  created_at: string;
  updated_at: string;
}

/** One `GET /api/search` result: a conversation, plus the matching message when the text matched. */
export interface SearchHit {
  conversation: Conversation;
  message_id: string | null;
  snippet: string | null;
  matched: 'title' | 'message';
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
  /** Free-text rules the classifier reads before the categories (owner, Cc-only, VIPs, exclusions). */
  instructions: string;
  /** Category used when the classifier answers "none"; empty = leave the mail in the inbox. */
  fallback_category: string;
  alerts: TriageAlert[];
  /** Per-account overrides, keyed by the account name (work mailbox vs personal Gmail). */
  account_rules: Record<string, TriageRules>;
}

/** How ONE mailbox is sorted; the defaults on TriageSettings have the same shape. */
export interface TriageRules {
  categories: Record<string, string>[];
  instructions: string;
  fallback_category: string;
  /** DM-1234 → Demands/DM-1234. Right for the work mailbox, wrong for a personal one. */
  demand_routing: boolean;
  alerts: TriageAlert[];
}

/** "Tell me the moment this person writes." Matched structurally, never by the classifier. */
export interface TriageAlert {
  name: string;
  enabled: boolean;
  /** Full addresses or whole domains; empty = any sender. */
  senders: string[];
  /** Case-insensitive substrings of the subject; empty = any subject. */
  keywords: string[];
  /** An out-of-office bounce from a VIP is not the VIP writing; on by default. */
  skip_auto_replies: boolean;
}

/** Meeting auto-RSVP through the host's calendar (docs/stories/08). */
export interface MeetingRsvpSettings {
  enabled: boolean;
  interval_min: number;
  /** Name of the MCP server (jarvis-host) that owns the calendar. */
  host: string;
  /** Outlook account (store) whose invites are answered; "" = default. */
  account: string;
  lookahead_days: number;
  /** Organizer domains that get an automatic answer; empty = nobody (fail closed). */
  allowed_domains: string[];
  /** Addresses or domains that are never declined. */
  vip: string[];
  remove_canceled: boolean;
  propose_slots: number;
  work_start_hour: number;
  work_end_hour: number;
}

export type ToolExposure = 'auto' | 'flat' | 'facade';

export type Formality = 'formal' | 'balanced' | 'casual';
export type Humor = 'none' | 'light' | 'witty';
export type Verbosity = 'terse' | 'concise' | 'detailed';
export type AddressStyle = 'name' | 'sir' | 'neutral';

export const FORMALITY: readonly Formality[] = ['formal', 'balanced', 'casual'];
export const HUMOR: readonly Humor[] = ['none', 'light', 'witty'];
export const VERBOSITY: readonly Verbosity[] = ['terse', 'concise', 'detailed'];
export const ADDRESS_STYLE: readonly AddressStyle[] = ['name', 'sir', 'neutral'];

/** How Jarvis speaks. Tone only — never what he is willing to say. */
export interface Personality {
  enabled: boolean;
  formality: Formality;
  humor: Humor;
  verbosity: Verbosity;
  address_style: AddressStyle;
  persona: string;
}

export type ConfirmationMode = 'destructive' | 'off';

/** When Jarvis stops to ask before running a tool. Unattended runs never ask. */
export interface Confirmations {
  mode: ConfirmationMode;
  /** Tool names or `namespace.*` / `*.tool` patterns that never ask. */
  always_allow: string[];
  /** Names that always ask, even with mode "off". */
  always_ask: string[];
}

/** Who Jarvis may write to directly; everyone else gets a draft in Outlook. */
export interface EmailPolicy {
  /** Full addresses (`rumen@bank.bg`) or whole domains (`@bank.bg`). */
  approved_direct_send: string[];
  allow_any_recipient: boolean;
}

export type SttKind = 'openai' | 'asr';

export interface Settings {
  assistant_name: string;
  user_name: string;
  timezone: string;
  language_hint: string;
  personality: Personality;
  confirmations: Confirmations;
  email: EmailPolicy;
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
  rsvp: MeetingRsvpSettings;
  /** The address people reach this core on; used for pairing/QR. Normally https. */
  public_url: string | null;
  stt_url: string | null;
  stt_kind: SttKind;
  stt_model: string;
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
/** The run picked up a message Arsen sent while it was working. */
export interface RunSteered extends RunEventBase {
  type: 'run.steered';
  text: string;
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
/** Arsen's chat folders changed (added, renamed, reordered, removed) — refetch the list. */
export interface FoldersChanged extends Base {
  type: 'folders.changed';
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
  | RunSteered
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
  | FoldersChanged
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
  /** Ids of attachments uploaded before sending, tied to this message. */
  attachment_ids?: string[];
  /** Read only when `conversation_id` is null: how the chat this message opens should behave. */
  incognito?: boolean;
  ttl_seconds?: number | null;
}
export interface RunCancelRequest {
  type: 'run.cancel';
  run_id: string;
}
/** Something said to a run that is ALREADY working; it reads it at its next step. */
export interface RunSteerRequest {
  type: 'run.steer';
  run_id: string;
  text: string;
  client_ref?: string | null;
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
  | RunSteerRequest
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

/** `POST /api/conversations/bulk` — one action applied to a multi-selection in the sidebar. */
export type BulkConversationAction = 'delete' | 'archive' | 'unarchive' | 'move' | 'read' | 'pin' | 'unpin';

export interface BulkResult {
  /** Ids that are gone (action = "delete"). */
  deleted: string[];
  /** The conversations as they are now (every other action). */
  updated: Conversation[];
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
