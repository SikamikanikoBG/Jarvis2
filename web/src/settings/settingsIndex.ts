export interface SettingsIndexEntry {
  /** DOM id of the section (or role card) to scroll to. */
  id: string;
  /** Shown on the chip. */
  label: string;
  /** Set on a sub-entry (a role card) so its chip can read "Model roles › chat" on hover. */
  group?: string;
  /** Extra words the search should match, drawn from that section's field labels and hints. */
  keywords: string[];
}

/**
 * One entry per anchor the settings screen exposes to the nav rail, in page order. Kept as data,
 * not read off the DOM, so it survives a section being collapsed or its fields changing shape.
 */
export const SETTINGS_INDEX: SettingsIndexEntry[] = [
  { id: 'settings-appearance', label: 'Appearance', keywords: ['theme', 'light', 'dark', 'system'] },
  {
    id: 'settings-general',
    label: 'General',
    keywords: [
      'assistant name',
      'user name',
      'timezone',
      'public url',
      'pairing',
      'qr',
      'stt',
      'whisper',
      'voice',
      'transcription',
      'asr',
      'languages',
      'concurrent runs',
      'endpoint',
      'repeated call',
      'threshold',
      'judge',
      'language hint',
    ],
  },
  {
    id: 'settings-personality',
    label: 'Personality',
    keywords: ['formality', 'humour', 'humor', 'verbosity', 'address', 'tone', 'speaks', 'sir', 'by name'],
  },
  {
    id: 'settings-voice',
    label: 'Voice',
    keywords: ['call', 'headset', 'speak', 'spoken', 'tools on a call', 'think', 'microphone', 'tts', 'style', 'namespaces', 'neural', 'borislav', 'kalina', 'rate', 'his voice', 'earpiece', 'speaker'],
  },
  {
    id: 'settings-confirmations',
    label: 'Confirmations',
    keywords: ['destructive', 'unattended', 'confirm before', 'ask before', 'scheduled', 'triage', 'meeting'],
  },
  {
    id: 'settings-email',
    label: 'Email',
    keywords: ['allow list', 'recipient', 'send', 'outlook', 'direct send', 'draft', 'gmail'],
  },
  {
    id: 'settings-behaviour',
    label: 'Context and behaviour',
    keywords: [
      'planning',
      'lane failover',
      'knowledge learning',
      'entities',
      'relations',
      'tool exposure',
      'facade',
      'flat',
      'auto',
      'history budget',
      'tool results budget',
      'admit',
      'truncated',
      'result_search',
      'context reserve',
      'tokens',
      'boards context',
      'skill size limit',
    ],
  },
  {
    id: 'settings-roles',
    label: 'Model roles',
    keywords: ['provider', 'ollama', 'vllm', 'base url', 'num_ctx', 'context', 'temperature', 'max tokens', 'timeout', 'keep alive', 'thinking', 'reasoning', 'lane'],
  },
  { id: 'role-chat', label: 'chat', group: 'Model roles', keywords: ['answers you', 'chat model'] },
  { id: 'role-background', label: 'background', group: 'Model roles', keywords: ['lane', 'scheduled', 'unattended', 'long context'] },
  { id: 'role-planner', label: 'planner', group: 'Model roles', keywords: ['writes the plan'] },
  { id: 'role-classifier', label: 'classifier', group: 'Model roles', keywords: ['tier', 'skills', 'routing', 'compaction'] },
  { id: 'role-judge', label: 'judge', group: 'Model roles', keywords: ['supervises a run'] },
  { id: 'role-triage', label: 'triage model', group: 'Model roles', keywords: ['inbound mail', 'background'] },
  { id: 'settings-mcp', label: 'MCP servers', keywords: ['tool', 'server', 'enabled', 'host', 'endpoint'] },
  {
    id: 'settings-triage',
    label: 'Triage',
    keywords: ['demand routing', 'dm-1234', 'alerts', 'categories', 'inbox', 'mail', 'folder'],
  },
  {
    id: 'settings-rsvp',
    label: 'Meeting auto-RSVP',
    keywords: ['calendar', 'accept', 'decline', 'cancelled meetings', 'invite'],
  },
  { id: 'settings-collab', label: 'Collaborators', keywords: ['key', 'invite', 'access', 'mcp'] },
  {
    id: 'settings-routing',
    label: 'Run routing',
    keywords: ['lane', 'chat', 'background', 'scheduled', 'triage', 'meeting', 'endpoint', 'gpu'],
  },
  {
    id: 'settings-budgets',
    label: 'Budgets per run kind',
    keywords: ['max steps', 'max tokens', 'max seconds', 'exhaustion', 'chat', 'planner', 'classifier', 'judge', 'triage'],
  },
];
