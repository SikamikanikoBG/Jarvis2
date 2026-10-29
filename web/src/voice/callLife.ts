/**
 * The call as the core sees it (docs/stories/13_delegated_tasks.md): an id for this call, hang-up
 * to hang-up, sent with every turn; a heartbeat while the page is there; and the hang-up itself.
 *
 * The core starts the tasks noted on a call when the call ends. Hang-up says so at once. A phone
 * that dies, a page Android throws away, a tunnel - those just stop the heartbeat, and the core
 * takes the call for dropped after `tasks.call_drop_after_s` (240 s: Chrome wakes a locked
 * phone's timers about once a minute). Silence on the line is not a drop: the heartbeat goes on.
 */

/** How often the page says it is still there. Well inside the core's drop window even when a
 *  locked phone stretches it to a minute. */
export const CALL_ALIVE_MS = 20_000;

export interface CallLifeDeps {
  /** Over the WebSocket; false when it is not open (a reconnect re-sends `alive`). */
  send: (msg: { type: 'call.alive'; call_id: string; conversation_id: string | null } | { type: 'call.ended'; call_id: string }) => boolean;
  /** The keepalive request for a page that is going away, when the socket may already be gone. */
  beacon: (callId: string) => void;
  newId?: () => string;
  setInterval?: (fn: () => void, ms: number) => unknown;
  clearInterval?: (handle: unknown) => void;
}

export class CallLife {
  readonly id: string;
  private conversationId: string | null;
  private timer: unknown = null;
  private ended = false;
  private readonly deps: CallLifeDeps;

  constructor(deps: CallLifeDeps, conversationId: string | null) {
    this.deps = deps;
    this.id = (deps.newId ?? defaultId)();
    this.conversationId = conversationId;
  }

  /** The call began: say so now and every CALL_ALIVE_MS until it ends. */
  start(): void {
    this.alive();
    const every = this.deps.setInterval ?? ((fn: () => void, ms: number) => window.setInterval(fn, ms));
    this.timer = every(() => this.alive(), CALL_ALIVE_MS);
  }

  /** The chat the call is in, once the core has made it (a call that opens a new chat). */
  setConversation(id: string | null): void {
    this.conversationId = id;
  }

  alive(): void {
    if (this.ended) return;
    this.deps.send({ type: 'call.alive', call_id: this.id, conversation_id: this.conversationId });
  }

  /** Hang-up: once, whatever calls it (the red button, the page going away). */
  end(viaBeacon = false): void {
    if (this.ended) return;
    this.ended = true;
    const stop = this.deps.clearInterval ?? ((h: unknown) => window.clearInterval(h as number));
    if (this.timer !== null) stop(this.timer);
    this.timer = null;
    if (viaBeacon || !this.deps.send({ type: 'call.ended', call_id: this.id })) this.deps.beacon(this.id);
  }

  get isEnded(): boolean {
    return this.ended;
  }
}

function defaultId(): string {
  const c = globalThis.crypto as Crypto | undefined;
  return c?.randomUUID ? c.randomUUID() : `call_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
}
