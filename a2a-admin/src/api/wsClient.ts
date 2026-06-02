/**
 * Admin WebSocket Client — real-time task updates via AdminWSHub.
 *
 * Connects to /v2/ws/admin, auto-reconnects on drop with exponential backoff,
 * and dispatches typed events to registered handlers.
 *
 * Protocol (server → client):
 *   - {"type": "task_update", "event": "created|updated|status_changed|deleted|comment_added", "task": {...}}
 *   - {"type": "task_list",    "tasks": [...]}
 *   - {"type": "task_progress","task_id": "...", "progress": 0.5, "message": "...", "status": "working"}
 *   - {"type": "pong"}
 *   - {"type": "error",        "detail": "..."}
 *
 * Protocol (client → server):
 *   - {"type": "subscribe_all"}
 *   - {"type": "subscribe", "task_ids": [...]}
 *   - {"type": "subscribe_progress", "task_ids": [...]}
 *   - {"type": "ping"}
 */

export type TaskEvent =
  | 'created'
  | 'updated'
  | 'status_changed'
  | 'deleted'
  | 'comment_added';

export interface TaskUpdateMessage {
  type: 'task_update';
  event: TaskEvent;
  task: Record<string, any>;
}

export interface TaskListMessage {
  type: 'task_list';
  tasks: Record<string, any>[];
}

export interface TaskProgressMessage {
  type: 'task_progress';
  task_id: string;
  progress: number;
  message?: string;
  status: string;
}

export type WsMessage = TaskUpdateMessage | TaskListMessage;

export type WsHandler = (msg: WsMessage) => void;

/** Handles non-entity meta-messages (pong with task_counts). */
export type WsMetaHandler = (msg: Record<string, unknown>) => void;

/** Handles real-time task progress updates. */
export type WsProgressHandler = (msg: TaskProgressMessage) => void;

export class AdminWsClient {
  private ws: WebSocket | null = null;
  private url: string;
  private handlers: WsHandler[] = [];
  private metaHandlers: WsMetaHandler[] = [];
  private progressHandlers: WsProgressHandler[] = [];
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 10;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private destroyed = false;
  private onStatusChange?: (connected: boolean) => void;

  constructor(
    token?: string,
    opts?: { onStatusChange?: (connected: boolean) => void },
  ) {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    this.url = `${proto}//${window.location.host}/v2/ws/admin`;
    if (token) {
      this.url += `?token=${encodeURIComponent(token)}`;
    }
    this.onStatusChange = opts?.onStatusChange;
  }

  /** Register a handler for incoming WS messages. */
  onMessage(handler: WsHandler): () => void {
    this.handlers.push(handler);
    return () => {
      this.handlers = this.handlers.filter((h) => h !== handler);
    };
  }

  /** Register a handler for meta messages (pong, error). */
  onMeta(handler: WsMetaHandler): () => void {
    this.metaHandlers.push(handler);
    return () => {
      this.metaHandlers = this.metaHandlers.filter((h) => h !== handler);
    };
  }

  /** Register a handler for real-time task progress updates. */
  onProgress(handler: WsProgressHandler): () => void {
    this.progressHandlers.push(handler);
    return () => {
      this.progressHandlers = this.progressHandlers.filter((h) => h !== handler);
    };
  }

  /** Open the WebSocket connection. Call once on mount. */
  connect(): void {
    if (this.destroyed) return;
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
      return; // already connected or connecting
    }

    try {
      this.ws = new WebSocket(this.url);
    } catch (e) {
      console.error('[AdminWS] Failed to create WebSocket:', e);
      this.scheduleReconnect();
      return;
    }

    this.ws.onopen = () => {
      console.log('[AdminWS] Connected');
      this.reconnectAttempts = 0;
      this.onStatusChange?.(true);
      // Subscribe to all tasks
      this.send({ type: 'subscribe_all' });
      // Start ping interval
      this.startPing();
    };

    this.ws.onmessage = (event: MessageEvent) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'task_update' || data.type === 'task_list') {
          this.dispatch(data as WsMessage);
        } else if (data.type === 'task_progress') {
          this.dispatchProgress(data as TaskProgressMessage);
        } else if (data.type === 'pong') {
          // Dispatch pong with task_counts to meta handlers
          this.dispatchMeta(data as Record<string, unknown>);
        }
        // Ignore connected/error meta messages (handled by protocol)
      } catch (e) {
        console.warn('[AdminWS] Failed to parse message:', e);
      }
    };

    this.ws.onclose = () => {
      console.log('[AdminWS] Disconnected');
      this.stopPing();
      this.onStatusChange?.(false);
      this.scheduleReconnect();
    };

    this.ws.onerror = (err) => {
      console.warn('[AdminWS] Error:', err);
    };
  }

  /** Close the WebSocket connection. Call on unmount. */
  disconnect(): void {
    this.destroyed = true;
    this.stopPing();
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws) {
      this.ws.onclose = null; // prevent reconnect loop
      if (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING) {
        this.ws.close(1000, 'Client disconnect');
      }
      this.ws = null;
    }
    this.onStatusChange?.(false);
  }

  /** Subscribe to progress updates for specific tasks. */
  subscribeProgress(taskIds: string[]): void {
    this.send({ type: 'subscribe_progress', task_ids: taskIds });
  }

  /** Send a JSON message to the server. */
  send(data: Record<string, unknown>): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(data));
    }
  }

  // ------------------------------------------------------------------
  // Private helpers
  // ------------------------------------------------------------------

  private dispatch(msg: WsMessage): void {
    for (const handler of this.handlers) {
      try {
        handler(msg);
      } catch (e) {
        console.error('[AdminWS] Handler error:', e);
      }
    }
  }

  private dispatchMeta(msg: Record<string, unknown>): void {
    for (const handler of this.metaHandlers) {
      try {
        handler(msg);
      } catch (e) {
        console.error('[AdminWS] Meta handler error:', e);
      }
    }
  }

  private dispatchProgress(msg: TaskProgressMessage): void {
    for (const handler of this.progressHandlers) {
      try {
        handler(msg);
      } catch (e) {
        console.error('[AdminWS] Progress handler error:', e);
      }
    }
  }

  private scheduleReconnect(): void {
    if (this.destroyed) return;
    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      console.warn('[AdminWS] Max reconnect attempts reached');
      return;
    }

    this.reconnectAttempts++;
    const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts - 1), 30000);
    const jitter = Math.random() * 1000;
    const totalDelay = delay + jitter;

    console.log(`[AdminWS] Reconnecting in ${Math.round(totalDelay)}ms (attempt ${this.reconnectAttempts})`);
    this.reconnectTimer = setTimeout(() => {
      if (!this.destroyed) {
        this.ws = null;
        this.connect();
      }
    }, totalDelay);
  }

  private startPing(): void {
    this.stopPing();
    // Send a ping every 25 s (server expects one every 30 s, with 10 s pong timeout)
    this.pingTimer = setInterval(() => {
      this.send({ type: 'ping' });
    }, 25000);
  }

  private stopPing(): void {
    if (this.pingTimer) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
  }
}