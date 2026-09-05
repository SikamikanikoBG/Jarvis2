import type { ClientMessage, ServerEvent } from '../protocol/types';
import { getToken } from '../lib/token';

export type ConnectionState = 'connecting' | 'open' | 'reconnecting' | 'closed';

export interface WsHandlers {
  onEvents: (events: ServerEvent[]) => void;
  onState: (state: ConnectionState, attempt: number) => void;
  /** Fired after every successful (re)connect; the store re-subscribes and heals here. */
  onOpen: (isReconnect: boolean) => void;
}

const MIN_BACKOFF = 1_000;
const MAX_BACKOFF = 30_000;
const PING_INTERVAL = 25_000;

export function wsUrl(): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const token = getToken();
  return `${proto}//${window.location.host}/ws${token ? `?token=${encodeURIComponent(token)}` : ''}`;
}

/**
 * One WebSocket with exponential backoff (1s → 30s) and per-frame batching: events that
 * arrive in the same tick are applied in one store update, so a burst of deltas costs one
 * render, not fifty.
 */
export class WsClient {
  private socket: WebSocket | null = null;
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private queue: ServerEvent[] = [];
  private flushScheduled = false;
  private everOpened = false;
  private stopped = false;

  constructor(private readonly handlers: WsHandlers) {}

  connect(): void {
    this.stopped = false;
    this.open();
  }

  close(): void {
    this.stopped = true;
    this.clearTimers();
    this.socket?.close();
    this.socket = null;
    this.handlers.onState('closed', this.attempt);
  }

  get isOpen(): boolean {
    return this.socket?.readyState === WebSocket.OPEN;
  }

  send(msg: ClientMessage): boolean {
    if (!this.isOpen || !this.socket) return false;
    this.socket.send(JSON.stringify(msg));
    return true;
  }

  private open(): void {
    this.clearTimers();
    this.handlers.onState(this.everOpened ? 'reconnecting' : 'connecting', this.attempt);
    let ws: WebSocket;
    try {
      ws = new WebSocket(wsUrl());
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = ws;
    ws.onopen = () => {
      if (this.socket !== ws) return;
      const isReconnect = this.everOpened;
      this.everOpened = true;
      this.attempt = 0;
      this.handlers.onState('open', 0);
      this.pingTimer = setInterval(() => this.send({ type: 'ping' }), PING_INTERVAL);
      this.handlers.onOpen(isReconnect);
    };
    ws.onmessage = (e: MessageEvent<string>) => {
      let data: unknown;
      try {
        data = JSON.parse(e.data);
      } catch {
        return;
      }
      if (data && typeof data === 'object' && typeof (data as { type?: unknown }).type === 'string') {
        this.queue.push(data as ServerEvent);
        this.scheduleFlush();
      }
    };
    ws.onclose = () => {
      if (this.socket !== ws) return;
      this.socket = null;
      if (this.pingTimer) clearInterval(this.pingTimer);
      this.pingTimer = null;
      if (!this.stopped) this.scheduleReconnect();
    };
    ws.onerror = () => {
      /* onclose follows */
    };
  }

  private scheduleReconnect(): void {
    if (this.stopped) return;
    const delay = Math.min(MAX_BACKOFF, MIN_BACKOFF * 2 ** this.attempt) * (0.85 + Math.random() * 0.3);
    this.attempt += 1;
    this.handlers.onState('reconnecting', this.attempt);
    this.timer = setTimeout(() => this.open(), delay);
  }

  private scheduleFlush(): void {
    if (this.flushScheduled) return;
    this.flushScheduled = true;
    const flush = () => {
      this.flushScheduled = false;
      if (this.queue.length === 0) return;
      const batch = this.queue;
      this.queue = [];
      this.handlers.onEvents(batch);
    };
    if (typeof requestAnimationFrame === 'function' && document.visibilityState === 'visible') requestAnimationFrame(flush);
    else setTimeout(flush, 0);
  }

  private clearTimers(): void {
    if (this.timer) clearTimeout(this.timer);
    if (this.pingTimer) clearInterval(this.pingTimer);
    this.timer = null;
    this.pingTimer = null;
  }
}
