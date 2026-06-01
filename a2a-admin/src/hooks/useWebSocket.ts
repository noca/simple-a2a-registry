/**
 * useWebSocket — React hook wrapping AdminWsClient for real-time task sync.
 *
 * Manages the full lifecycle: connect → subscribe_all → receive task_list →
 * incremental task_update → auto-reconnect with exponential backoff.
 *
 * Usage:
 *   const { connected, tasks, lastUpdate, connect, disconnect } = useWebSocket(token);
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
  updated_at?: string | number;
  /** Parent task ids (dependency edges). */
  parents?: string[];
  /** Child task ids. */
  children?: string[];
  /** Arbitrary extra fields forwarded from the server. */
  [key: string]: unknown;
}

export interface UseWebSocketResult {
  /** Whether the WebSocket is currently open. */
  connected: boolean;
  /** The current task list — maintained from task_list (full sync) and
   *  task_update (incremental). */
  tasks: Task[];
  /** Unix timestamp (ms) of the last message received. */
  lastUpdate: number;
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

  const wsRef = useRef<AdminWsClient | null>(null);
  const cleanupRef = useRef<(() => void) | null>(null);

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
  }, [token]);

  // Stable disconnect
  const disconnect = useCallback(() => {
    if (cleanupRef.current) {
      cleanupRef.current();
      cleanupRef.current = null;
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

  return { connected, tasks, lastUpdate, connect, disconnect };
}

export default useWebSocket;