import React, { useEffect, useState } from 'react';
import { statsAPI, taskAPI, agentAPI } from '../api/client';
import { useStore } from '../store/useStore';
import StatusTag from '../components/StatusTag';
import TaskRealtimePanel from '../components/TaskRealtimePanel';
import TaskDetailPanel from '../components/TaskDetailPanel';
import { useWebSocket } from '../hooks/useWebSocket';
import type { Task } from '../hooks/useWebSocket';

const Dashboard: React.FC = () => {
  const { addToast, stats, setStats } = useStore();
  const [recentAgents, setRecentAgents] = useState<any[]>([]);
  const [recentTasks, setRecentTasks] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);

  // WebSocket for real-time task data
  const token = sessionStorage.getItem('token') || localStorage.getItem('token');
  const { connected, tasks, taskCounts, taskProgress } = useWebSocket(token);

  // Detail drawer state
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 30000);
    return () => clearInterval(interval);
  }, []);

  const loadData = async () => {
    try {
      const [health, agents, tasks] = await Promise.all([
        statsAPI.health().catch(() => null),
        agentAPI.list({ limit: '5' }).catch(() => ({ agents: [] })),
        taskAPI.listV2({ limit: '10' }).catch(() => ({ tasks: [] })),
      ]);
      if (health) setStats(health.stats || health);
      setRecentAgents(agents.agents || []);
      setRecentTasks(tasks.tasks || []);
    } catch (e: any) {
      addToast('error', `加载仪表盘失败: ${e?.message || '未知错误'}`);
    } finally {
      setLoading(false);
    }
  };

  const openTaskDetail = (task: Task) => {
    setSelectedTask(task);
    setDetailOpen(true);
  };

  const closeDetail = () => {
    setDetailOpen(false);
    setSelectedTask(null);
  };

  const totalTasks = stats?.totalTasks ?? recentTasks.length;

  const statCards = [
    { label: 'Agent 总数', value: stats?.totalAgents ?? recentAgents.length, color: '#007AFF' },
    { label: '在线 Agent', value: stats?.aliveAgents ?? 0, color: '#30D158' },
    { label: 'Stale Agent', value: stats?.staleAgents ?? 0, color: '#FFD60A' },
    { label: '任务总数', value: totalTasks, color: '#BF5AF2' },
  ];

  return (
    <div>
      <h1 className="page-title">📊 Dashboard</h1>

      {loading ? (
        <div className="stats-grid">
          {[1, 2, 3, 4].map((i) => (
            <div key={i} className="stat-card">
              <div className="loading-skeleton" style={{ height: 12, width: 60, marginBottom: 8 }} />
              <div className="loading-skeleton" style={{ height: 30, width: 80 }} />
            </div>
          ))}
        </div>
      ) : (
        <>
          <div className="stats-grid">
            {statCards.map((c) => (
              <div key={c.label} className="stat-card">
                <div className="stat-indicator" style={{ background: c.color }} />
                <div className="stat-label">{c.label}</div>
                <div className="stat-value">{c.value}</div>
              </div>
            ))}
          </div>

          {/* Real-time task panel — P2.1: ping/pong 携带任务状态, WebUI 实时面板 */}
          <div style={{ marginBottom: 24 }}>
            <TaskRealtimePanel
              taskCounts={taskCounts}
              tasks={tasks}
              connected={connected}
              onTaskClick={openTaskDetail}
            />
          </div>

          <div className="charts-row">
            <div className="chart-card">
              <h3>🤖 Recent Agents</h3>
              {recentAgents.length === 0 ? (
                <div style={{ color: 'var(--text-secondary)', padding: 20, textAlign: 'center' }}>暂无 Agent</div>
              ) : (
                <table className="macos-table">
                  <thead>
                    <tr>
                      <th>Name</th>
                      <th>Status</th>
                      <th>Connection</th>
                    </tr>
                  </thead>
                  <tbody>
                    {recentAgents.slice(0, 5).map((a: any) => (
                      <tr key={a.id}>
                        <td>
                          <div style={{ fontWeight: 500 }}>{a.name}</div>
                          <div style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>{a.id}</div>
                        </td>
                        <td><StatusTag status={a.status || 'unknown'} /></td>
                        <td style={{ color: 'var(--text-secondary)' }}>{a.connection || 'http'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
            <div className="chart-card">
              <h3>📋 Recent Tasks</h3>
              {recentTasks.length === 0 ? (
                <div style={{ color: 'var(--text-secondary)', padding: 20, textAlign: 'center' }}>暂无任务</div>
              ) : (
                <table className="macos-table">
                  <thead>
                    <tr>
                      <th>Title</th>
                      <th>Status</th>
                      <th>Priority</th>
                    </tr>
                  </thead>
                  <tbody>
                    {recentTasks.slice(0, 5).map((t: any) => (
                      <tr key={t.id} style={{ cursor: 'pointer' }} onClick={() => openTaskDetail({
                        id: t.id, title: t.title, status: t.status,
                        assignee: t.assignee, priority: t.priority,
                        created_at: t.created_at, started_at: t.started_at,
                        completed_at: t.completed_at, result: t.result,
                      } as Task)}>
                        <td>
                          <div style={{ fontWeight: 500 }}>{t.title}</div>
                          <div style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>{t.id}</div>
                        </td>
                        <td><StatusTag status={t.status || 'todo'} /></td>
                        <td style={{ color: 'var(--text-secondary)' }}>{t.priority || 'normal'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>

          {/* Footer info */}
          <div className="macos-card" style={{ marginTop: 8, textAlign: 'center', color: 'var(--text-secondary)' }}>
            A2A Registry v1.0.0 — Dashboard auto-refreshes every 30s {connected ? '🟢 WS 已连接' : '🔴 WS 未连接'}
          </div>
        </>
      )}

      {/* P2.2: Task detail panel with progress bar, timeline, metrics */}
      <TaskDetailPanel
        task={selectedTask}
        progressInfo={selectedTask ? taskProgress[selectedTask.id] : undefined}
        open={detailOpen}
        onClose={closeDetail}
        fetchDetail={true}
      />
    </div>
  );
};

export default Dashboard;