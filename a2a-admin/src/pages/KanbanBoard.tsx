import React, { useEffect, useState, useCallback, useRef } from 'react';
import { taskAPI } from '../api/client';
import { useStore } from '../store/useStore';
import StatusTag from '../components/StatusTag';
import TaskTimeline from '../components/TaskTimeline';
import { AdminWsClient } from '../api/wsClient';
import type { TaskProgressInfo } from '../hooks/useWebSocket';

const COLUMNS = ['todo', 'ready', 'running', 'completed', 'blocked', 'failed', 'cancelled'];
const COLUMN_COLORS: Record<string, string> = {
  todo: '#86868B', ready: '#007AFF', running: '#BF5AF2',
  completed: '#30D158', blocked: '#FF9F0A', failed: '#FF453A', cancelled: '#86868B',
};
const PRIORITY_OPTIONS = ['low', 'normal', 'high'];

const formatTime = (ts: string | number | undefined) => {
  if (!ts) return '-';
  const d = new Date(Number(ts) * 1000);
  if (isNaN(d.getTime())) return '-';
  return d.toLocaleString('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  });
};

const KanbanBoard: React.FC = () => {
  const { addToast } = useStore();
  const [tasks, setTasks] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [showCreate, setShowCreate] = useState(false);
  const [newTask, setNewTask] = useState({ title: '', body: '', status: 'todo', priority: 'normal', assignee: '' });

  // Detail drawer state
  const [selectedTask, setSelectedTask] = useState<any>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editForm, setEditForm] = useState({ title: '', description: '', assignee: '', priority: 'normal' });
  const [commentText, setCommentText] = useState('');
  const [confirmDelete, setConfirmDelete] = useState(false);
  const selectedTaskRef = useRef(selectedTask);
  useEffect(() => { selectedTaskRef.current = selectedTask; }, [selectedTask]);

  // --- Batch selection state ---
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [batchTargetStatus, setBatchTargetStatus] = useState('completed');
  const [batchDeleteConfirm, setBatchDeleteConfirm] = useState(false);
  const [batchProcessing, setBatchProcessing] = useState(false);
  const [batchErrors, setBatchErrors] = useState<string[]>([]);

  const [wsConnected, setWsConnected] = useState(false);
  const [resultExpanded, setResultExpanded] = useState(false);
  const [errorExpanded, setErrorExpanded] = useState(false);
  const wsRef = useRef<AdminWsClient | null>(null);

  // ── P2.2 Real-time progress & metrics state ──
  const [taskProgress, setTaskProgress] = useState<Record<string, TaskProgressInfo>>({});
  const [detailEvents, setDetailEvents] = useState<any[]>([]);
  const [detailRuns, setDetailRuns] = useState<any[]>([]);

  const fetchTasks = useCallback(async () => {
    try {
      const params: Record<string, string> = {};
      if (search) params.q = search;
      const data = await taskAPI.listV2(params);
      setTasks(data.tasks || []);
    } catch (e: any) {
      addToast('error', `加载看板失败: ${e.message}`);
    } finally {
      setLoading(false);
    }
  }, [search]);

  useEffect(() => { fetchTasks(); }, [fetchTasks]);

  // WebSocket — real-time task sync
  useEffect(() => {
    const token = sessionStorage.getItem('token') || localStorage.getItem('token');
    const ws = new AdminWsClient(token || undefined, {
      onStatusChange: (connected) => setWsConnected(connected),
    });
    wsRef.current = ws;

    ws.onMessage((msg) => {
      if (msg.type === 'task_update') {
        const { event } = msg;
        const updated = msg.task;

        switch (event) {
          case 'created':
            setTasks((prev) => [updated, ...prev]);
            break;

          case 'updated':
          case 'status_changed':
            setTasks((prev) =>
              prev.map((t) => (t.id === updated.id ? { ...t, ...updated } : t)),
            );
            // Update the detail drawer if this task is currently open
            setSelectedTask((prev: any) =>
              prev && prev.id === updated.id ? { ...prev, ...updated } : prev,
            );
            break;

          case 'deleted':
            setTasks((prev) => prev.filter((t) => t.id !== updated.id));
            setSelectedTask((prev: any) =>
              prev && prev.id === updated.id ? null : prev,
            );
            setSelectedIds((prev) => { const s = new Set(prev); s.delete(updated.id); return s; });
            break;

          case 'comment_added': {
            // Merge the new comment into the detail drawer if this task is open
            const comment = updated.comment;
            if (comment) {
              setSelectedTask((prev: any) => {
                if (!prev || prev.id !== updated.id) return prev;
                const existing = prev.comments || [];
                const isDuplicate = existing.some(
                  (c: any) => c.id === comment.id || (c.body === comment.body && c.created_at === comment.created_at),
                );
                if (isDuplicate) return prev;
                return { ...prev, comments: [...existing, comment] };
              });
            }
            break;
          }
        }
      } else if (msg.type === 'task_list') {
        // Full task list sync (reconnect)
        setTasks(msg.tasks);
      } else {
        // P2.2: Real-time task progress (and any other messages)
        const raw = msg as any;
        if (raw.type === 'task_progress') {
          const { task_id, progress, message, status } = raw;
          setTaskProgress((prev) => ({
            ...prev,
            [task_id]: {
              progress,
              message,
              status,
              updatedAt: Date.now(),
            },
          }));
          // Also sync status into the tasks list for live card updates
          setTasks((prev) =>
            prev.map((t) =>
              t.id === task_id ? { ...t, progress, status } : t,
            ),
          );
          // Sync into selectedTask detail drawer if open
          setSelectedTask((prev: any) =>
            prev && prev.id === task_id ? { ...prev, progress, status } : prev,
          );
        }
      }
    });

    ws.connect();

    return () => {
      ws.disconnect();
      wsRef.current = null;
    };
  }, []);

  const handleCreate = async () => {
    if (!newTask.title.trim()) { addToast('error', '任务标题不能为空'); return; }
    try {
      await taskAPI.createV2(newTask);
      addToast('success', '任务创建成功');
      setShowCreate(false);
      setNewTask({ title: '', body: '', status: 'todo', priority: 'normal', assignee: '' });
      fetchTasks();
    } catch (e: any) {
      addToast('error', `创建失败: ${e.message}`);
    }
  };

  const handleMove = async (id: string, status: string) => {
    try {
      await taskAPI.updateV2(id, { status });
      // If the drawer is showing this task, update the status there too
      if (selectedTask && selectedTask.id === id) {
        setSelectedTask({ ...selectedTask, status });
      }
      fetchTasks();
    } catch (e: any) {
      addToast('error', `移动失败: ${e.message}`);
    }
  };

  // Open detail drawer
  const openDetail = async (id: string) => {
    setDrawerOpen(true);
    setDetailLoading(true);
    setEditing(false);
    setConfirmDelete(false);
    setCommentText('');
    try {
      const data = await taskAPI.getV2(id);
      const task = data.task || data;
      setSelectedTask({ ...task, comments: data.comments || [] });
      setDetailEvents(data.events || []);
      setDetailRuns(data.runs || []);
      setResultExpanded(false);
      setErrorExpanded(false);
      setEditForm({
        title: task.title || '',
        description: task.description || task.body || '',
        assignee: task.assignee || '',
        priority: task.priority || 'normal',
      });
    } catch (e: any) {
      addToast('error', `加载任务详情失败: ${e.message}`);
      closeDrawer();
    } finally {
      setDetailLoading(false);
    }
  };

  const closeDrawer = useCallback(() => {
    setDrawerOpen(false);
    setSelectedTask(null);
    setEditing(false);
    setConfirmDelete(false);
    setDetailEvents([]);
    setDetailRuns([]);
  }, []);

  // Save edits
  const handleSaveEdit = async () => {
    if (!selectedTask) return;
    if (!editForm.title.trim()) { addToast('error', '标题不能为空'); return; }
    try {
      await taskAPI.updateV2(selectedTask.id, {
        title: editForm.title,
        body: editForm.description,
        assignee: editForm.assignee,
        priority: editForm.priority,
      });
      addToast('success', '任务更新成功');
      setEditing(false);
      // Refresh detail
      const data = await taskAPI.getV2(selectedTask.id);
      const saved = data.task || data;
      setSelectedTask({ ...saved, comments: data.comments || [] });
      fetchTasks();
    } catch (e: any) {
      addToast('error', `更新失败: ${e.message}`);
    }
  };

  // Submit comment
  const handleSubmitComment = async () => {
    if (!selectedTask || !commentText.trim()) return;
    try {
      await taskAPI.comment(selectedTask.id, commentText.trim());
      setCommentText('');
      addToast('success', '评论已提交');
      // Refresh detail
      const data = await taskAPI.getV2(selectedTask.id);
      const updated = data.task || data;
      setSelectedTask({ ...updated, comments: data.comments || [] });
    } catch (e: any) {
      addToast('error', `评论提交失败: ${e.message}`);
    }
  };

  // Delete task
  const handleDelete = useCallback(async () => {
    const task = selectedTaskRef.current;
    if (!task) return;
    try {
      await taskAPI.deleteV2(task.id);
      addToast('success', '任务已删除');
      closeDrawer();
      fetchTasks();
    } catch (e: any) {
      addToast('error', `删除失败: ${e.message}`);
    }
  }, [addToast, closeDrawer, fetchTasks]);

  // --- Batch selection handlers ---

  const toggleSelect = (id: string) => {
    setSelectedIds((prev) => {
      const s = new Set(prev);
      if (s.has(id)) {
        s.delete(id);
      } else {
        s.add(id);
      }
      return s;
    });
    setBatchDeleteConfirm(false);
  };

  const selectColumn = (col: string, checked: boolean) => {
    setSelectedIds((prev) => {
      const s = new Set(prev);
      const colTasks = groupedTasks[col] || [];
      for (const t of colTasks) {
        if (checked) {
          s.add(t.id);
        } else {
          s.delete(t.id);
        }
      }
      return s;
    });
    setBatchDeleteConfirm(false);
  };

  const clearSelection = () => {
    setSelectedIds(new Set());
    setBatchDeleteConfirm(false);
    setBatchErrors([]);
  };

  const handleBatchStatusChange = async () => {
    const ids = Array.from(selectedIds);
    if (ids.length === 0) return;
    setBatchProcessing(true);
    setBatchErrors([]);
    try {
      const result = await taskAPI.batchUpdateStatus({ task_ids: ids, status: batchTargetStatus });
      const updated = result.updated ?? 0;
      const failed = result.failed ?? [];
      if (failed.length > 0) {
        const errMsgs = failed.map((f: any) => `${f.task_id}: ${f.error}`);
        setBatchErrors(errMsgs);
        addToast('warning', `${updated} 个成功, ${failed.length} 个失败`);
      } else {
        addToast('success', `已更新 ${updated} 个任务状态 → ${batchTargetStatus}`);
      }
      clearSelection();
      fetchTasks();
    } catch (e: any) {
      addToast('error', `批量状态变更失败: ${e.message}`);
    } finally {
      setBatchProcessing(false);
    }
  };

  const handleBatchDelete = async () => {
    const ids = Array.from(selectedIds);
    if (ids.length === 0) return;
    setBatchProcessing(true);
    setBatchErrors([]);
    try {
      const result = await taskAPI.batchDelete({ task_ids: ids });
      const deleted = result.deleted ?? 0;
      const failed = result.failed ?? [];
      if (failed.length > 0) {
        const errMsgs = failed.map((f: any) => `${f.task_id}: ${f.error}`);
        setBatchErrors(errMsgs);
        addToast('warning', `${deleted} 个已删除, ${failed.length} 个失败`);
      } else {
        addToast('success', `已删除 ${deleted} 个任务`);
      }
      clearSelection();
      fetchTasks();
    } catch (e: any) {
      addToast('error', `批量删除失败: ${e.message}`);
    } finally {
      setBatchProcessing(false);
    }
  };

  const groupedTasks = COLUMNS.reduce((acc, col) => {
    acc[col] = tasks.filter((t) => t.status === col || (col === 'todo' && !t.status));
    return acc;
  }, {} as Record<string, any[]>);

  const getCounts = () => {
    const counts = { todo: 0, ready: 0, running: 0, completed: 0, blocked: 0, failed: 0, cancelled: 0 };
    tasks.forEach((t) => { const s = t.status || 'todo'; if (s in counts) counts[s as keyof typeof counts]++; });
    return counts;
  };
  const counts = getCounts();

  // Dashboard-style mini stat row
  const statSummary = [
    { label: '任务总数', value: tasks.length, color: '#007AFF' },
    { label: '运行中', value: counts.running, color: '#BF5AF2' },
    { label: '已完成', value: counts.completed, color: '#30D158' },
    { label: '阻塞', value: counts.blocked, color: '#FF9F0A' },
  ];

  const detailField = (label: string, value: React.ReactNode) => (
    <div style={{ marginBottom: 12 }}>
      <div style={{ fontSize: 10, fontWeight: 600, color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.3px', marginBottom: 3 }}>{label}</div>
      <div style={{ fontSize: 13 }}>{value}</div>
    </div>
  );

  const copyToClipboard = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      addToast('success', '已复制到剪贴板');
    } catch {
      // Fallback for non-HTTPS
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      addToast('success', '已复制到剪贴板');
    }
  };

  const copyBtn = (text: string, label = '复制') => (
    <button onClick={(e) => { e.stopPropagation(); copyToClipboard(text); }}
      style={{
        background: 'transparent', border: '1px solid var(--separator)', borderRadius: 6,
        padding: '2px 8px', fontSize: 11, color: 'var(--text-tertiary)', cursor: 'pointer',
        marginLeft: 'auto',
      }}>{label}</button>
  );

  const unwrapResult = (result: any): { display: string; isJson: boolean } => {
    if (!result) return { display: '', isJson: false };
    // If result is a string, use it directly
    if (typeof result === 'string') return { display: result, isJson: false };
    // Common pattern: agent returns {"text": "..."} — unwrap
    if (typeof result === 'object' && !Array.isArray(result) && Object.keys(result).length === 1 && typeof result.text === 'string') {
      return { display: result.text, isJson: false };
    }
    // Structured JSON object — pretty-print
    return { display: JSON.stringify(result, null, 2), isJson: true };
  };

  const renderResultBlock = (result: any) => {
    const { display, isJson } = unwrapResult(result);
    if (!display) return null;
    const needsExpand = display.length > 500;
    const preview = needsExpand ? display.slice(0, 500) + '…' : display;

    return (
      <div style={{ borderTop: '1px solid var(--separator)', paddingTop: 16, marginBottom: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', marginBottom: 8 }}>
          <div style={{ fontSize: 10, fontWeight: 600, color: 'var(--green)', textTransform: 'uppercase', letterSpacing: '0.3px' }}>
            ✅ 执行结果
          </div>
          <div style={{ flex: 1 }} />
          {copyBtn(display, '复制结果')}
        </div>
        <div style={{ position: 'relative' }}>
          <pre style={{
            background: 'rgba(52,199,89,0.06)',
            border: '1px solid rgba(52,199,89,0.15)',
            borderRadius: 8,
            padding: '10px 12px',
            fontSize: 12,
            lineHeight: 1.5,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            overflow: 'auto',
            maxHeight: resultExpanded ? 'none' : 120,
            color: 'var(--fg)',
            margin: 0,
            fontFamily: isJson ? 'ui-monospace, "Cascadia Code", monospace' : undefined,
          }}>{resultExpanded ? display : preview}</pre>
          {needsExpand && (
            <button onClick={() => setResultExpanded(!resultExpanded)}
              style={{
                background: 'rgba(52,199,89,0.08)', border: '1px solid rgba(52,199,89,0.2)',
                borderRadius: 6, padding: '2px 10px', fontSize: 11, cursor: 'pointer',
                color: 'var(--green)', marginTop: 6,
              }}>{resultExpanded ? '收起 ↑' : '展开全部 ↓'}</button>
          )}
        </div>
      </div>
    );
  };

  const renderErrorBlock = (errMsg: string) => {
    const needsExpand = errMsg.length > 500;

    return (
      <div style={{ borderTop: '1px solid var(--separator)', paddingTop: 16, marginBottom: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', marginBottom: 8 }}>
          <div style={{ fontSize: 10, fontWeight: 600, color: 'var(--red)', textTransform: 'uppercase', letterSpacing: '0.3px' }}>
            ❌ 错误信息
          </div>
          <div style={{ flex: 1 }} />
          {copyBtn(errMsg, '复制错误')}
        </div>
        <pre style={{
          background: 'rgba(255,69,58,0.06)',
          border: '1px solid rgba(255,69,58,0.15)',
          borderRadius: 8,
          padding: '10px 12px',
          fontSize: 12,
          lineHeight: 1.5,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
          overflow: 'auto',
          maxHeight: errorExpanded ? 'none' : 120,
          color: 'var(--fg)',
          margin: 0,
        }}>{errorExpanded ? errMsg : errMsg.slice(0, 500) + '…'}</pre>
        {needsExpand && (
          <button onClick={() => setErrorExpanded(!errorExpanded)}
            style={{
              background: 'rgba(255,69,58,0.08)', border: '1px solid rgba(255,69,58,0.2)',
              borderRadius: 6, padding: '2px 10px', fontSize: 11, cursor: 'pointer',
              color: 'var(--red)', marginTop: 6,
            }}>{errorExpanded ? '收起 ↑' : '展开全部 ↓'}</button>
        )}
      </div>
    );
  };

  // Checkbox style shared by card and column header
  const checkboxStyle: React.CSSProperties = {
    width: 16, height: 16, borderRadius: 4, border: '1.5px solid var(--separator)',
    background: 'transparent', cursor: 'pointer', flexShrink: 0,
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    padding: 0, appearance: 'none', WebkitAppearance: 'none',
    outline: 'none',
  };

  return (
    <div style={{ paddingBottom: selectedIds.size > 0 ? 70 : 0 }}>
      <h1 className="page-title">📋 Kanban Board</h1>

      {/* Mini stats */}
      <div className="stats-grid" style={{ gridTemplateColumns: 'repeat(4, 1fr)', marginBottom: 16 }}>
        {statSummary.map((s) => (
          <div key={s.label} className="stat-card" style={{ padding: '12px 16px' }}>
            <div className="stat-indicator" style={{ background: s.color }} />
            <div className="stat-label">{s.label}</div>
            <div className="stat-value" style={{ fontSize: 22 }}>{s.value}</div>
          </div>
        ))}
      </div>

      <div className="toolbar">
        <input type="text" placeholder="Search cards..." value={search}
          onChange={(e) => { setSearch(e.target.value); setLoading(true); }}
          style={{
            padding: '8px 12px', borderRadius: 8, border: '1px solid var(--border)',
            background: 'var(--bg)', fontSize: 13, width: 280, outline: 'none',
          }} />
        <button className="btn" onClick={fetchTasks}
          style={btnStyle}>⟳ Refresh</button>
        <span style={{
          display: 'inline-flex', alignItems: 'center', gap: 4,
          fontSize: 10, color: wsConnected ? 'var(--green)' : 'var(--text-tertiary)',
        }}>
          <span style={{
            display: 'inline-block', width: 6, height: 6, borderRadius: '50%',
            background: wsConnected ? '#30D158' : '#FF453A',
          }} />
          {wsConnected ? 'Live' : 'Offline'}
        </span>
        <span className="spacer" />
        {selectedIds.size > 0 && (
          <button onClick={clearSelection}
            style={{ ...btnStyle, background: 'transparent', border: '1px solid var(--border)', color: 'var(--text-secondary)' }}>
            取消选择 ({selectedIds.size})
          </button>
        )}
        <button onClick={() => setShowCreate(true)}
          style={{ ...btnStyle, ...btnPrimaryStyle }}>+ New Task</button>
      </div>

      {loading ? (
        <div style={{ display: 'flex', gap: 12, overflowX: 'auto' }}>
          {[1, 2, 3, 4, 5].map((i) => (
            <div key={i} className="kanban-column" style={{ width: 260, flexShrink: 0 }}>
              <div className="loading-skeleton" style={{ height: 40, marginBottom: 8 }} />
              <div className="loading-skeleton" style={{ height: 100 }} />
            </div>
          ))}
        </div>
      ) : (
        <div className="kanban-board">
          {COLUMNS.map((col) => {
            const colTasks = groupedTasks[col] || [];
            const allSelected = colTasks.length > 0 && colTasks.every((t) => selectedIds.has(t.id));
            const someSelected = colTasks.some((t) => selectedIds.has(t.id));

            return (
              <div key={col} className="kanban-column">
                <div className="kanban-column-header" style={{ borderTopColor: COLUMN_COLORS[col] }}>
                  {/* Column select-all checkbox */}
                  <label style={{
                    display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer',
                    marginRight: 8, flexShrink: 0,
                  }}
                    onClick={(e) => { e.stopPropagation(); }}
                  >
                    <input
                      type="checkbox"
                      checked={allSelected}
                      ref={(el) => {
                        if (el) el.indeterminate = someSelected && !allSelected;
                      }}
                      onChange={(e) => {
                        e.stopPropagation();
                        selectColumn(col, e.target.checked);
                      }}
                      style={checkboxStyle}
                    />
                  </label>
                  <span><StatusTag status={col} /></span>
                  <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{colTasks.length}</span>
                </div>
                <div className="kanban-column-body">
                  {colTasks.map((t) => {
                    const isSelected = selectedIds.has(t.id);
                    return (
                      <div key={t.id}
                        className="kanban-card"
                        onClick={() => openDetail(t.id)}
                        style={{
                          ...(isSelected ? {
                            border: '2px solid var(--accent)',
                            boxShadow: '0 0 0 1px rgba(0,122,255,0.15)',
                          } : {}),
                          cursor: 'pointer',
                        }}
                      >
                        {/* Card-level checkbox */}
                        <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
                          <label style={{
                            flexShrink: 0, marginTop: 1, cursor: 'pointer',
                            display: 'flex', alignItems: 'center',
                          }}
                            onClick={(e) => { e.stopPropagation(); }}
                          >
                            <input
                              type="checkbox"
                              checked={isSelected}
                              onChange={() => toggleSelect(t.id)}
                              style={checkboxStyle}
                            />
                          </label>
                          <div style={{ flex: 1, minWidth: 0 }}>
                            <div className="k-title">{t.title}</div>
                            {(t.description || t.body) && (
                              <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 4 }}>{t.description || t.body}</div>
                            )}
                            <div className="k-meta">
                              <span>{t.assignee || 'unassigned'}</span>
                              <span>{t.priority || 'normal'}</span>
                            </div>
                            <div style={{ display: 'flex', gap: 4, marginTop: 8 }} onClick={(e) => e.stopPropagation()}>
                              {col === 'todo' && (
                                <>
                                  <button onClick={() => handleMove(t.id, 'ready')} style={miniBtn}>→ Ready</button>
                                  <button onClick={() => handleMove(t.id, 'blocked')} style={{ ...miniBtn, color: 'var(--orange)' }}>→ Blocked</button>
                                </>
                              )}
                              {col === 'ready' && <button onClick={() => handleMove(t.id, 'running')} style={miniBtn}>→ Run</button>}
                              {col === 'running' && (
                                <>
                                  <button onClick={() => handleMove(t.id, 'completed')} style={miniBtn}>→ Done</button>
                                  <button onClick={() => handleMove(t.id, 'ready')} style={miniBtn}>← Revert</button>
                                </>
                              )}
                              {col === 'blocked' && (
                                <>
                                  <button onClick={() => handleMove(t.id, 'ready')} style={miniBtn}>→ Ready</button>
                                  <button onClick={() => handleMove(t.id, 'cancelled')} style={{ ...miniBtn, color: 'var(--red)' }}>→ Cancel</button>
                                </>
                              )}
                              {col === 'failed' && <button onClick={() => handleMove(t.id, 'ready')} style={miniBtn}>→ Ready</button>}
                              {col === 'cancelled' && <button onClick={() => handleMove(t.id, 'ready')} style={miniBtn}>→ Ready</button>}
                            </div>
                          </div>
                        </div>
                      </div>
                    );
                  })}
                  {colTasks.length === 0 && (
                    <div style={{ textAlign: 'center', color: 'var(--text-tertiary)', fontSize: 12, padding: 12 }}>
                      空
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Detail Drawer */}
      {drawerOpen && (
        <>
          {/* Overlay */}
          <div onClick={closeDrawer} style={{
            position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.25)', zIndex: 99,
          }} />
          {/* Drawer */}
          <div className={`detail-drawer ${drawerOpen ? 'open' : ''}`}
            style={{ transform: drawerOpen ? 'translateX(0)' : 'translateX(100%)' }}>
            <div className="detail-drawer-header">
              <h3 style={{ fontSize: 16, fontWeight: 600 }}>
                {selectedTask?.title || '任务详情'}
              </h3>
              <div style={{ display: 'flex', gap: 6 }}>
                {!detailLoading && selectedTask && (
                  <>
                    {editing ? (
                      <>
                        <button onClick={handleSaveEdit} style={{ ...miniBtn, background: 'var(--accent)', color: 'white', border: 'none' }}>保存</button>
                        <button onClick={() => { setEditing(false); if (selectedTask) setEditForm({ title: selectedTask.title || '', description: selectedTask.description || selectedTask.body || '', assignee: selectedTask.assignee || '', priority: selectedTask.priority || 'normal' }); }}
                          style={{ ...miniBtn }}>取消</button>
                      </>
                    ) : (
                      <>
                        <button onClick={() => setEditing(true)} style={{ ...miniBtn }}>✏️ 编辑</button>
                        <button onClick={closeDrawer} style={{ ...miniBtn }}>✕ 关闭</button>
                      </>
                    )}
                  </>
                )}
              </div>
            </div>
            <div className="detail-drawer-body">
              {detailLoading ? (
                <div>
                  <div className="loading-skeleton" style={{ height: 20, marginBottom: 12 }} />
                  <div className="loading-skeleton" style={{ height: 40, marginBottom: 12 }} />
                  <div className="loading-skeleton" style={{ height: 80 }} />
                </div>
              ) : selectedTask ? (
                <div>
                  {/* Status & Priority row */}
                  <div style={{ display: 'flex', gap: 8, marginBottom: 16, alignItems: 'center' }}>
                    <StatusTag status={selectedTask.status || 'todo'} />
                    <select
                      value={selectedTask.status || 'todo'}
                      onChange={(e) => {
                        const newStatus = e.target.value;
                        if (newStatus !== selectedTask.status) {
                          handleMove(selectedTask.id, newStatus);
                        }
                      }}
                      style={{
                        padding: '2px 4px', borderRadius: 4, border: '1px solid var(--border)',
                        background: 'transparent', fontSize: 11, color: 'var(--text-secondary)',
                        cursor: 'pointer',
                      }}
                    >
                      {COLUMNS.map((s) => (
                        <option key={s} value={s}>{s}</option>
                      ))}
                    </select>
                    {selectedTask.priority && (
                      <span className="status-badge" style={{
                        background: selectedTask.priority === 'high' ? 'rgba(255,69,58,0.1)' :
                          selectedTask.priority === 'low' ? 'rgba(0,0,0,0.035)' : 'rgba(0,122,255,0.08)',
                        color: selectedTask.priority === 'high' ? 'var(--red)' :
                          selectedTask.priority === 'low' ? 'var(--text-secondary)' : 'var(--accent)',
                      }}>
                        {selectedTask.priority}
                      </span>
                    )}
                    <span style={{ flex: 1 }} />
                    {!confirmDelete ? (
                      <button onClick={(e) => { e.stopPropagation(); setConfirmDelete(true); }}
                        style={{ ...miniBtn, color: 'var(--red)', borderColor: 'rgba(255,69,58,0.3)' }}>🗑 删除</button>
                    ) : (
                      <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                        <span style={{ fontSize: 11, color: 'var(--red)' }}>确认删除？</span>
                        <button onClick={handleDelete} style={{ ...miniBtn, background: 'var(--red)', color: 'white', border: 'none' }}>是</button>
                        <button onClick={() => setConfirmDelete(false)} style={{ ...miniBtn }}>否</button>
                      </div>
                    )}
                  </div>

                  {/* Edit Mode */}
                  {editing ? (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginBottom: 20 }}>
                      <div>
                        <div style={{ fontSize: 10, fontWeight: 600, color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.3px', marginBottom: 3 }}>标题</div>
                        <input value={editForm.title}
                          onChange={(e) => setEditForm({ ...editForm, title: e.target.value })}
                          style={inputStyle} />
                      </div>
                      <div>
                        <div style={{ fontSize: 10, fontWeight: 600, color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.3px', marginBottom: 3 }}>描述</div>
                        <textarea value={editForm.description}
                          onChange={(e) => setEditForm({ ...editForm, description: e.target.value })}
                          rows={3} style={{ ...inputStyle, resize: 'vertical', fontFamily: 'inherit' }} />
                      </div>
                      <div>
                        <div style={{ fontSize: 10, fontWeight: 600, color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.3px', marginBottom: 3 }}>负责人 (assignee)</div>
                        <input value={editForm.assignee}
                          onChange={(e) => setEditForm({ ...editForm, assignee: e.target.value })}
                          style={inputStyle} />
                      </div>
                      <div>
                        <div style={{ fontSize: 10, fontWeight: 600, color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.3px', marginBottom: 3 }}>优先级</div>
                        <select value={editForm.priority}
                          onChange={(e) => setEditForm({ ...editForm, priority: e.target.value })}
                          style={inputStyle}>
                          {PRIORITY_OPTIONS.map((p) => <option key={p} value={p}>{p}</option>)}
                        </select>
                      </div>
                    </div>
                  ) : (
                    /* View Mode */
                    <div style={{ marginBottom: 20 }}>
                      {detailField('ID', <code style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>{selectedTask.id}</code>)}
                      {(selectedTask.description || selectedTask.body) && detailField('描述', selectedTask.description || selectedTask.body)}
                      {detailField('负责人', selectedTask.assignee || <span style={{ color: 'var(--text-tertiary)' }}>unassigned</span>)}

                      {/* ── P2.2: Progress Bar ── */}
                      {(() => {
                        const taskId = selectedTask.id;
                        const progInfo = taskProgress[taskId];
                        const isRunning = ['running', 'accepted', 'working'].includes((selectedTask.status || '').toLowerCase());
                        const isDone = ['completed', 'failed', 'cancelled'].includes((selectedTask.status || '').toLowerCase());
                        let pct = 0;
                        let progColor = 'var(--accent)';
                        if (progInfo) {
                          pct = Math.round(progInfo.progress * 100);
                        } else if (isDone) {
                          pct = 100;
                          progColor = selectedTask.status === 'failed' ? 'var(--red)' : 'var(--green)';
                        } else if (isRunning) {
                          pct = 0;
                        }
                        return (
                          <div style={{
                            marginTop: 12, marginBottom: 16,
                            padding: '12px 16px', background: 'var(--bg)', borderRadius: 8,
                          }}>
                            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6, fontSize: 11 }}>
                              <span style={{ fontWeight: 500, color: 'var(--text-secondary)' }}>
                                {progInfo?.message || (isRunning ? '任务执行中...' : '任务进度')}
                              </span>
                              <span style={{ fontWeight: 600, color: progColor }}>
                                {isRunning ? `${pct}%` : (isDone ? '100%' : '0%')}
                              </span>
                            </div>
                            <div style={{
                              height: 6, borderRadius: 3,
                              background: 'rgba(0,0,0,0.06)', overflow: 'hidden',
                            }}>
                              <div style={{
                                width: `${isRunning ? pct : (isDone ? 100 : 0)}%`,
                                height: '100%',
                                borderRadius: 3,
                                background: progColor,
                                transition: 'width 300ms ease',
                              }} />
                            </div>
                          </div>
                        );
                      })()}

                      {/* ── P2.2: Metrics Cards ── */}
                      <div style={{
                        display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8,
                        marginBottom: 16,
                      }}>
                        {(() => {
                          // Duration
                          const now = Math.floor(Date.now() / 1000);
                          const startedAt = typeof selectedTask.started_at === 'number'
                            ? selectedTask.started_at
                            : (typeof selectedTask.started_at === 'string' ? parseInt(selectedTask.started_at, 10) : 0);
                          const completedAt = typeof selectedTask.completed_at === 'number'
                            ? selectedTask.completed_at
                            : (typeof selectedTask.completed_at === 'string' ? parseInt(selectedTask.completed_at, 10) : 0);
                          let duration = 0;
                          if (completedAt > 0 && startedAt > 0) {
                            duration = completedAt - startedAt;
                          } else if (startedAt > 0) {
                            duration = now - startedAt;
                          }
                          const durLabel = duration < 60 ? `${Math.round(duration)}s` :
                            duration < 3600 ? `${Math.floor(duration / 60)}m ${Math.round(duration % 60)}s` :
                            `${Math.floor(duration / 3600)}h ${Math.floor((duration % 3600) / 60)}m`;

                          // Output size
                          let outSize = '-';
                          if (selectedTask.result) {
                            const str = typeof selectedTask.result === 'string'
                              ? selectedTask.result : JSON.stringify(selectedTask.result);
                            const bytes = new Blob([str]).size;
                            outSize = bytes < 1024 ? `${bytes} B` :
                              bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KB` :
                              `${(bytes / 1048576).toFixed(1)} MB`;
                          }

                          return (
                            <>
                              <div style={{ padding: '10px 12px', background: 'var(--bg)', borderRadius: 8 }}>
                                <div style={{ fontSize: 10, color: 'var(--text-secondary)', marginBottom: 2 }}>⏱ 耗时</div>
                                <div style={{ fontSize: 16, fontWeight: 600 }}>{duration > 0 ? durLabel : '-'}</div>
                              </div>
                              <div style={{ padding: '10px 12px', background: 'var(--bg)', borderRadius: 8 }}>
                                <div style={{ fontSize: 10, color: 'var(--text-secondary)', marginBottom: 2 }}>💾 输出量</div>
                                <div style={{ fontSize: 16, fontWeight: 600 }}>{outSize}</div>
                              </div>
                              <div style={{ padding: '10px 12px', background: 'var(--bg)', borderRadius: 8 }}>
                                <div style={{ fontSize: 10, color: 'var(--text-secondary)', marginBottom: 2 }}>🔄 运行次数</div>
                                <div style={{ fontSize: 16, fontWeight: 600 }}>{detailRuns.length}</div>
                              </div>
                              <div style={{ padding: '10px 12px', background: 'var(--bg)', borderRadius: 8 }}>
                                <div style={{ fontSize: 10, color: 'var(--text-secondary)', marginBottom: 2 }}>📅 开始时间</div>
                                <div style={{ fontSize: 13, fontWeight: 500 }}>{formatTime(selectedTask.started_at || selectedTask.created_at)}</div>
                              </div>
                            </>
                          );
                        })()}
                      </div>

                      {detailField('创建时间', formatTime(selectedTask.created_at || selectedTask.createdAt))}
                      {detailField('更新时间', formatTime(selectedTask.updated_at || selectedTask.updatedAt))}
                    </div>
                  )}

                  {/* ── P2.2: Timeline ── */}
                  {!editing && detailEvents.length > 0 && (
                    <div style={{ borderTop: '1px solid var(--separator)', paddingTop: 16, marginBottom: 16 }}>
                      <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.3px', marginBottom: 12 }}>
                        📋 任务时间线 ({detailEvents.length} 个事件)
                      </div>
                      <TaskTimeline events={detailEvents} maxItems={30} />
                    </div>
                  )}

                  {/* Kanban task result — from A2A agent WS output */}
                  {(selectedTask.status === 'completed' && selectedTask.result)
                    ? renderResultBlock(selectedTask.result)
                    : (selectedTask.status === 'failed' && selectedTask.last_failure_error)
                      ? renderErrorBlock(selectedTask.last_failure_error)
                      : null}

                  {/* Parent / Children info */}
                  {(selectedTask.parents?.length > 0 || selectedTask.children?.length > 0) && (
                    <div style={{ borderTop: '1px solid var(--separator)', paddingTop: 16, marginBottom: 16 }}>
                      {selectedTask.parents?.length > 0 && detailField('父任务', selectedTask.parents.join(', '))}
                      {selectedTask.children?.length > 0 && detailField('子任务', selectedTask.children.join(', '))}
                    </div>
                  )}

                  {/* Comments Section */}
                  <div style={{ borderTop: '1px solid var(--separator)', paddingTop: 16 }}>
                    <h4 style={{ fontSize: 12, fontWeight: 600, marginBottom: 12, color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.3px' }}>
                      评论 ({selectedTask.comments?.length || 0})
                    </h4>

                    {/* Comment input */}
                    <div style={{ display: 'flex', gap: 8, marginBottom: 16 }}>
                      <input
                        placeholder="添加评论..."
                        value={commentText}
                        onChange={(e) => setCommentText(e.target.value)}
                        onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSubmitComment(); } }}
                        style={{ ...inputStyle, flex: 1 }}
                      />
                      <button onClick={handleSubmitComment}
                        disabled={!commentText.trim()}
                        style={{ ...btnStyle, ...btnPrimaryStyle, opacity: commentText.trim() ? 1 : 0.5 }}>发送</button>
                    </div>

                    {/* Comments list */}
                    {(!selectedTask.comments || selectedTask.comments.length === 0) ? (
                      <div style={{ textAlign: 'center', color: 'var(--text-tertiary)', fontSize: 12, padding: 12 }}>
                        暂无评论
                      </div>
                    ) : (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                        {selectedTask.comments.map((c: any, idx: number) => (
                          <div key={c.id || idx} style={{
                            background: 'var(--bg)', borderRadius: 8, padding: '10px 12px',
                          }}>
                            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                              <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--accent)' }}>
                                {c.author || c.created_by || 'Anonymous'}
                              </span>
                              <span style={{ fontSize: 10, color: 'var(--text-tertiary)' }}>
                                {formatTime(c.created_at || c.createdAt)}
                              </span>
                            </div>
                            <div style={{ fontSize: 13, lineHeight: 1.5, whiteSpace: 'pre-wrap' }}>
                              {c.body || c.text || c.content}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              ) : (
                <div style={{ textAlign: 'center', color: 'var(--text-tertiary)', padding: 40, fontSize: 13 }}>
                  无法加载任务详情
                </div>
              )}
            </div>
          </div>
        </>
      )}

      {/* Create Modal */}
      {showCreate && (
        <div style={{
          position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.3)', backdropFilter: 'blur(4px)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 200,
        }}>
          <div style={{ background: 'var(--bg-card)', borderRadius: 14, padding: 24, width: 420, boxShadow: 'var(--shadow-lg)' }}>
            <h3 style={{ fontSize: 16, fontWeight: 600, marginBottom: 16 }}>创建新任务</h3>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <input placeholder="Task Title *" value={newTask.title}
                onChange={(e) => setNewTask({ ...newTask, title: e.target.value })}
                style={inputStyle} />
              <textarea placeholder="Description (optional)" value={newTask.body}
                onChange={(e) => setNewTask({ ...newTask, body: e.target.value })}
                rows={3} style={{ ...inputStyle, resize: 'vertical', fontFamily: 'inherit' }} />
              <select value={newTask.status} onChange={(e) => setNewTask({ ...newTask, status: e.target.value })}
                style={inputStyle}>
                {COLUMNS.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
              <select value={newTask.priority} onChange={(e) => setNewTask({ ...newTask, priority: e.target.value })}
                style={inputStyle}>
                <option value="low">Low</option>
                <option value="normal">Normal</option>
                <option value="high">High</option>
              </select>
              <input placeholder="Assignee" value={newTask.assignee}
                onChange={(e) => setNewTask({ ...newTask, assignee: e.target.value })}
                style={inputStyle} />
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 16 }}>
              <button onClick={() => setShowCreate(false)}
                style={{ ...btnStyle, background: 'transparent', border: '1px solid var(--border)' }}>Cancel</button>
              <button onClick={handleCreate} style={{ ...btnStyle, ...btnPrimaryStyle }}>Create</button>
            </div>
          </div>
        </div>
      )}

      {/* Floating Batch Action Bar */}
      {selectedIds.size > 0 && (
        <>
          {/* Batch errors detail */}
          {batchErrors.length > 0 && (
            <div style={{
              position: 'fixed', bottom: 76, left: 0, right: 0,
              display: 'flex', justifyContent: 'center', zIndex: 115,
              pointerEvents: 'none',
            }}>
              <div style={{
                background: 'rgba(255,69,58,0.08)', border: '1px solid rgba(255,69,58,0.25)',
                borderRadius: 10, padding: '8px 14px', fontSize: 11,
                color: 'var(--red)', maxWidth: 500,
                pointerEvents: 'auto',
              }}>
                <div style={{ fontWeight: 600, marginBottom: 4 }}>
                  部分操作失败 ({batchErrors.length})
                </div>
                {batchErrors.map((e, i) => (
                  <div key={i} style={{ lineHeight: 1.6 }}>• {e}</div>
                ))}
              </div>
            </div>
          )}

          <div style={{
            position: 'fixed', bottom: 0, left: 0, right: 0,
            background: 'var(--bg-card)', backdropFilter: 'blur(12px)',
            borderTop: '1px solid var(--separator)',
            padding: '12px 24px', display: 'flex', alignItems: 'center', gap: 12,
            zIndex: 110, boxShadow: '0 -4px 12px rgba(0,0,0,0.08)',
          }}>
            {/* Selected count */}
            <span style={{ fontSize: 13, fontWeight: 500, color: 'var(--accent)', whiteSpace: 'nowrap' }}>
              {selectedIds.size} 个已选择
            </span>

            <div style={{ width: 1, height: 24, background: 'var(--separator)' }} />

            {/* Status dropdown */}
            <select
              value={batchTargetStatus}
              onChange={(e) => setBatchTargetStatus(e.target.value)}
              style={{
                padding: '6px 10px', borderRadius: 6, border: '1px solid var(--border)',
                background: 'var(--bg)', fontSize: 12, color: 'var(--fg)',
                outline: 'none', cursor: 'pointer',
              }}
            >
              {COLUMNS.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>

            {/* Batch status change */}
            <button
              onClick={handleBatchStatusChange}
              disabled={batchProcessing}
              style={{
                ...btnStyle, background: 'var(--accent)', color: 'white', border: 'none',
                opacity: batchProcessing ? 0.6 : 1,
              }}
            >
              {batchProcessing ? '处理中...' : '变更状态'}
            </button>

            <div style={{ width: 1, height: 24, background: 'var(--separator)' }} />

            {/* Batch delete */}
            {!batchDeleteConfirm ? (
              <button
                onClick={() => setBatchDeleteConfirm(true)}
                disabled={batchProcessing}
                style={{
                  ...btnStyle, background: 'var(--red)', color: 'white', border: 'none',
                  opacity: batchProcessing ? 0.6 : 1,
                }}
              >
                删除
              </button>
            ) : (
              <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                <span style={{ fontSize: 11, color: 'var(--red)' }}>确认删除 {selectedIds.size} 个任务？</span>
                <button
                  onClick={handleBatchDelete}
                  disabled={batchProcessing}
                  style={{
                    ...btnStyle, background: 'var(--red)', color: 'white', border: 'none',
                    opacity: batchProcessing ? 0.6 : 1,
                  }}
                >
                  {batchProcessing ? '删除中...' : '确认删除'}
                </button>
                <button
                  onClick={() => setBatchDeleteConfirm(false)}
                  style={{ ...btnStyle, background: 'transparent', border: '1px solid var(--border)' }}
                >
                  取消
                </button>
              </div>
            )}

            <span style={{ flex: 1 }} />

            {/* Close selection */}
            <button
              onClick={clearSelection}
              style={{
                ...btnStyle, background: 'transparent', border: '1px solid var(--border)',
                color: 'var(--text-secondary)', fontSize: 11,
              }}
            >
              ✕ 取消选择
            </button>
          </div>
        </>
      )}
    </div>
  );
};

const btnStyle: React.CSSProperties = {
  padding: '8px 16px', borderRadius: 8, border: 'none', cursor: 'pointer',
  fontSize: 12, fontWeight: 500,
};
const btnPrimaryStyle: React.CSSProperties = {
  background: 'linear-gradient(135deg, #007AFF, #AF52DE)', color: 'white',
};
const miniBtn: React.CSSProperties = {
  padding: '2px 8px', borderRadius: 4, border: '1px solid var(--border)',
  background: 'transparent', cursor: 'pointer', fontSize: 10, color: 'var(--accent)',
};
const inputStyle: React.CSSProperties = {
  padding: '8px 12px', borderRadius: 8, border: '1px solid var(--border)',
  background: 'var(--bg)', fontSize: 13, outline: 'none', width: '100%',
};

export default KanbanBoard;