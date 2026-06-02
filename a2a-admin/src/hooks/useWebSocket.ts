/**
 * useWebSocket — React hook wrapping AdminWsClient for real-time task sync.
 *
 * Manages the full lifecycle: connect → subscribe_all → receive task_list →
 * incremental task_update → auto-reconnect with exponential backoff.
 *
 * Usage:
 *   const { connected, tasks, lastUpdate, taskCounts, taskProgress, connect, disconnect } = useWebSocket(token);
 *
 * The hook auto-connects when a token is provided, and disconnects on unmount.
 * Call connect()/disconnect() manually to re-establish or tear down on demand.
 */

import { useEffect, useState, useRef, useCallback } from 'react';
import { AdminWsClient } from '../api/wsClient';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface Task {
  id: string;
  title: string;
  status: string;
  priority?: string;
  assignee?: string;
  body?: string;
  description?: string;
  /** ISO-8601 or unix-seconds timestamp. Server shape varies; normalise at display. */
  created_at?: string | number;
  started_at?: string | number;
  completed_at?: string | number;
  updated_at?: string | number;
  /** Parent task ids (dependency edges). */
  parents?: string[];
  /** Child task ids. */
  children?: string[];
  /** Result from completed tasks */
  result?: string | Record<string, unknown> | null;
  /** Current run id */
  current_run_id?: number;
  /** Progress percentage (0-100) from agent updates */
  progress?: number;
  /** Arbitrary extra fields forwarded from the server. */
  [key: string]: unknown;
}

export interface TaskProgressInfo {
  /** Progress value 0.0–1.0 */
  progress: number;
  /** Optional human-readable message */
  message?: string;
  /** Current task status */
  status: string;
  /** Timestamp of last progress update */
  updatedAt: number;
}

export interface UseWebSocketResult {
  /** Whether the WebSocket is currently open. */
  connected: boolean;
  /** The current task list — maintained from task_list (full sync) and
   *  task_update (incremental). */
  tasks: Task[];
  /** Unix timestamp (ms) of the last message received. */
  lastUpdate: number;
  /** Task counts from the most recent pong (includes pending/running/completed/failed). */
  taskCounts: Record<string, number>;
  /** Per-task real-time progress info (keyed by task id). */
  taskProgress: Record<string, TaskProgressInfo>;
  /** Manually establish (or re-establish) the connection. */
  connect: () => void;
  /** Manually tear down the connection. */
  disconnect: () => void;
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useWebSocket(token: string | null): UseWebSocketResult {
  const [connected, setConnected] = useState(false);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [lastUpdate, setLastUpdate] = useState(0);
  const [taskCounts, setTaskCounts] = useState<Record<string, number>>({});
  const [taskProgress, setTaskProgress] = useState<Record<string, TaskProgressInfo>>({});

  const wsRef = useRef<AdminWsClient | null>(null);
  const cleanupRef = useRef<(() => void) | null>(null);
  const metaCleanupRef = useRef<(() => void) | null>(null);
  const progressCleanupRef = useRef<(() => void) | null>(null);

  // Stable connect
  const connect = useCallback(() => {
    // Tear down any existing connection first
    if (wsRef.current) {
      wsRef.current.disconnect();
      wsRef.current = null;
    }
    if (cleanupRef.current) {
      cleanupRef.current();
      cleanupRef.current = null;
    }

    const ws = new AdminWsClient(token ?? undefined, {
      onStatusChange: (conn) => setConnected(conn),
    });

    // Register the single handler that keeps React state in sync
    const unsub = ws.onMessage((msg) => {
      if (msg.type === 'task_update') {
        const { event, task: updatedTask } = msg;
        switch (event) {
          case 'created':
            setTasks((prev) => [updatedTask as Task, ...prev]);
            break;
          case 'updated':
          case 'status_changed':
            setTasks((prev) =>
              prev.map((t) =>
                t.id === updatedTask.id ? { ...t, ...updatedTask } : t,
              ),
            );
            break;
          case 'deleted':
            setTasks((prev) => prev.filter((t) => t.id !== updatedTask.id));
            break;
          case 'comment_added':
            // comment_added affects the detail drawer, not the board list
            break;
        }
      } else if (msg.type === 'task_list') {
        setTasks(msg.tasks as Task[]);
      }
      setLastUpdate(Date.now());
    });

    cleanupRef.current = unsub;
    wsRef.current = ws;
    ws.connect();

    // Register meta handler for pong with task_counts
    const unsubMeta = ws.onMeta((meta) => {
      if (meta.type === 'pong' && meta.task_counts) {
        setTaskCounts(meta.task_counts as Record<string, number>);
      }
      setLastUpdate(Date.now());
    });
    metaCleanupRef.current = unsubMeta;

    // Register progress handler for real-time task_progress updates
    const unsubProgress = ws.onProgress((msg) => {
      setTaskProgress((prev) => ({
        ...prev,
        [msg.task_id]: {
          progress: msg.progress,
          message: msg.message,
          status: msg.status,
          updatedAt: Date.now(),
        },
      }));
      // Also update the task in the main list if it exists
      setTasks((prev) =>
        prev.map((t) =>
          t.id === msg.task_id
            ? { ...t, progress: msg.progress, status: msg.status }
            : t,
        ),
      );
      setLastUpdate(Date.now());
    });
    progressCleanupRef.current = unsubProgress;

    // Auto-subscribe to progress for all active tasks
    setTimeout(() => {
      ws.subscribeProgress(['*']);
    }, 500);
  }, [token]);

  // Stable disconnect
  const disconnect = useCallback(() => {
    if (cleanupRef.current) {
      cleanupRef.current();
      cleanupRef.current = null;
    }
    if (metaCleanupRef.current) {
      metaCleanupRef.current();
      metaCleanupRef.current = null;
    }
    if (progressCleanupRef.current) {
      progressCleanupRef.current();
      progressCleanupRef.current = null;
    }
    if (wsRef.current) {
      wsRef.current.disconnect();
      wsRef.current = null;
    }
    setConnected(false);
  }, []);

  // Auto-connect when token becomes available; clean up on unmount
  useEffect(() => {
    if (token) {
      connect();
    }
    return () => {
      disconnect();
    };
  }, [token, connect, disconnect]);

  return { connected, tasks, lastUpdate, taskCounts, taskProgress, connect, disconnect };
}

export default useWebSocket;