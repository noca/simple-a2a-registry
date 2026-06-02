/**
 * TaskRealtimePanel — 实时任务面板组件
 *
 * 展示实时任务状态汇总（来自 WebSocket pong 的 task_counts）
 * 以及当前进行中的任务列表（来自 task_list / task_update 事件）。
 *
 * 使用方式：
 *   <TaskRealtimePanel
 *     taskCounts={{ pending: 3, running: 2, completed: 45, failed: 1 }}
 *     tasks={[...]}
 *     connected={true}
 *   />
 */
import React, { useMemo } from 'react';
import StatusTag from './StatusTag';
import type { Task } from '../hooks/useWebSocket';

interface TaskRealtimePanelProps {
  /** 任务状态计数（来自 pong 消息） */
  taskCounts: Record<string, number>;
  /** 当前任务列表 */
  tasks: Task[];
  /** WebSocket 连接状态 */
  connected: boolean;
  /** 点击任务时的回调（跳转详情） */
  onTaskClick?: (task: Task) => void;
}

const STATUS_COLORS: Record<string, string> = {
  pending: 'var(--text-tertiary)',
  todo: 'var(--text-tertiary)',
  ready: 'var(--blue)',
  running: 'var(--purple)',
  accepted: 'var(--purple)',
  working: 'var(--purple)',
  completed: 'var(--green)',
  failed: 'var(--red)',
  blocked: 'var(--orange)',
};

const STATUS_LABELS: Record<string, string> = {
  pending: '待处理',
  todo: '待办',
  ready: '就绪',
  running: '进行中',
  accepted: '已接收',
  working: '执行中',
  completed: '已完成',
  failed: '失败',
  blocked: '阻塞',
};

const TaskRealtimePanel: React.FC<TaskRealtimePanelProps> = ({
  taskCounts,
  tasks,
  connected,
  onTaskClick,
}) => {
  // Compute summary bars from taskCounts
  const statusBars = useMemo(() => {
    const total = Object.values(taskCounts).reduce((s: number, v) => s + (v as number), 0);
    if (total === 0) return [];
    return Object.entries(taskCounts).map(([status, count]) => ({
      status,
      label: STATUS_LABELS[status] || status,
      count: count as number,
      pct: total > 0 ? ((count as number) / total) * 100 : 0,
      color: STATUS_COLORS[status] || 'var(--text-tertiary)',
    }));
  }, [taskCounts]);

  // Filter to running / non-terminal tasks
  const activeTasks = useMemo(() => {
    return tasks.filter((t) => {
      const s = (t.status || '').toLowerCase();
      return !['completed', 'failed', 'canceled'].includes(s);
    });
  }, [tasks]);

  // Most recent 5 completed tasks
  const recentDone = useMemo(() => {
    return tasks
      .filter((t) => {
        const s = (t.status || '').toLowerCase();
        return s === 'completed' || s === 'failed';
      })
      .slice(0, 5);
  }, [tasks]);

  return (
    <div className="chart-card">
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
        <h3 style={{ margin: 0, fontSize: 14, fontWeight: 600 }}>📡 实时任务面板</h3>
        <span
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 4,
            fontSize: 11,
            color: connected ? 'var(--green)' : 'var(--red)',
          }}
        >
          <span
            style={{
              width: 6,
              height: 6,
              borderRadius: '50%',
              background: connected ? 'var(--green)' : 'var(--red)',
              display: 'inline-block',
            }}
          />
          {connected ? '已连接' : '已断开'}
        </span>
      </div>

      {/* Status bars */}
      {statusBars.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <div
            style={{
              display: 'flex',
              height: 24,
              borderRadius: 6,
              overflow: 'hidden',
              marginBottom: 8,
            }}
          >
            {statusBars.map((sb) => (
              <div
                key={sb.status}
                style={{
                  width: `${sb.pct}%`,
                  minWidth: sb.count > 0 ? 4 : 0,
                  background: sb.color,
                  opacity: 0.7,
                  transition: 'width 300ms ease',
                }}
                title={`${sb.label}: ${sb.count}`}
              />
            ))}
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 12px', fontSize: 11, color: 'var(--text-secondary)' }}>
            {statusBars.map((sb) => (
              <span key={sb.status} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <span style={{ width: 8, height: 8, borderRadius: '50%', background: sb.color, display: 'inline-block' }} />
                {sb.label}: <strong>{sb.count}</strong>
              </span>
            ))}
          </div>
        </div>
      )}

      {!connected && (
        <div style={{ textAlign: 'center', padding: 20, color: 'var(--text-tertiary)', fontSize: 12 }}>
          WebSocket 未连接，等待连接中...
        </div>
      )}

      {connected && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
          {/* Active tasks */}
          <div>
            <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', marginBottom: 8 }}>
              进行中任务 ({activeTasks.length})
            </div>
            {activeTasks.length === 0 ? (
              <div style={{ color: 'var(--text-tertiary)', fontSize: 11, padding: '4px 0' }}>
                暂无进行中任务
              </div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {activeTasks.slice(0, 6).map((t) => (
                  <div
                    key={t.id}
                    onClick={() => onTaskClick?.(t)}
                    style={{ cursor: 'pointer',
                      padding: '6px 8px',
                      borderRadius: 6,
                      background: 'rgba(0,0,0,0.02)',
                      borderLeft: `3px solid ${STATUS_COLORS[t.status?.toLowerCase() || ''] || 'var(--text-tertiary)'}`,
                    }}
                  >
                    <div style={{ fontSize: 12, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {t.title || t.id}
                    </div>
                    <div style={{ fontSize: 10, color: 'var(--text-tertiary)', marginTop: 2, display: 'flex', justifyContent: 'space-between' }}>
                      <StatusTag status={t.status || 'unknown'} />
                      <span>{t.assignee || ''}</span>
                    </div>
                  </div>
                ))}
                {activeTasks.length > 6 && (
                  <div style={{ fontSize: 10, color: 'var(--text-tertiary)', textAlign: 'center' }}>
                    还有 {activeTasks.length - 6} 个...
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Recent completed */}
          <div>
            <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', marginBottom: 8 }}>
              最近完成 ({recentDone.length})
            </div>
            {recentDone.length === 0 ? (
              <div style={{ color: 'var(--text-tertiary)', fontSize: 11, padding: '4px 0' }}>
                暂无已完成任务
              </div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {recentDone.map((t) => (
                  <div
                    key={t.id}
                    onClick={() => onTaskClick?.(t)}
                    style={{ cursor: 'pointer',
                      padding: '6px 8px',
                      borderRadius: 6,
                      background: 'rgba(0,0,0,0.02)',
                      borderLeft: `3px solid ${STATUS_COLORS[t.status?.toLowerCase() || ''] || 'var(--text-tertiary)'}`,
                    }}
                  >
                    <div style={{ fontSize: 12, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {t.title || t.id}
                    </div>
                    <div style={{ fontSize: 10, color: 'var(--text-tertiary)', marginTop: 2 }}>
                      <StatusTag status={t.status || 'unknown'} />
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

export default TaskRealtimePanel;