/**
 * TaskDetailPanel — 任务详情面板
 *
 * 展示选定任务的完整详情：状态、进度条、耗量、输出量、时间线
 *
 * 使用方式：
 *   <TaskDetailPanel
 *     task={selectedTask}
 *     progressInfo={taskProgress[selectedTask.id]}
 *     open={drawerOpen}
 *     onClose={closeDrawer}
 *     onFetchDetail={fetchTaskDetail}
 *   />
 */
import React, { useEffect, useState } from 'react';
import {
  Drawer, Descriptions, Tag, Progress, Spin, Typography, Space, Tabs,
} from 'antd';
import {
  ClockCircleOutlined, DatabaseOutlined, NumberOutlined, UserOutlined,
} from '@ant-design/icons';
import StatusTag from './StatusTag';
import TaskTimeline from './TaskTimeline';
import ProvenanceTab from './ProvenanceTab';
import { taskAPI } from '../api/client';
import type { Task, TaskProgressInfo } from '../hooks/useWebSocket';

const { Text } = Typography;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Format seconds into a human-readable duration string. */
function formatDuration(seconds?: number | null): string {
  if (seconds === undefined || seconds === null || seconds === 0) return '-';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) {
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    return `${m}m ${s}s`;
  }
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `${h}h ${m}m`;
}

function formatTime(ts?: number | string | null): string {
  if (ts === undefined || ts === null) return '-';
  const num = typeof ts === 'string' ? parseInt(ts, 10) : ts;
  if (num === 0) return '-';
  return new Date(num * 1000).toLocaleString();
}

function estimateOutputSize(result: any): string {
  if (!result) return '-';
  const str = typeof result === 'string' ? result : JSON.stringify(result);
  const bytes = new Blob([str]).size;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// ---------------------------------------------------------------------------
// Status colour map for progress bars
// ---------------------------------------------------------------------------

const STATUS_PROGRESS_COLORS: Record<string, string> = {
  pending: '#d9d9d9',
  todo: '#d9d9d9',
  ready: '#1677ff',
  running: '#722ed1',
  accepted: '#722ed1',
  working: '#722ed1',
  completed: '#52c41a',
  failed: '#ff4d4f',
  blocked: '#fa8c16',
  cancelled: '#8c8c8c',
  archived: '#8c8c8c',
};

// ---------------------------------------------------------------------------
// Computed metrics helpers
// ---------------------------------------------------------------------------

interface TaskMetrics {
  /** Elapsed seconds (or 0 if not started) */
  duration: number;
  /** Display-friendly duration */
  durationLabel: string;
  /** Output size string */
  outputSize: string;
  /** Number of runs */
  runCount: number;
  /** Progress percentage (0–100) */
  progressPct: number;
  /** Progress bar colour */
  progressColor: string;
}

function computeMetrics(
  task: Task,
  progressInfo?: TaskProgressInfo | null,
  detailRuns?: any[],
): TaskMetrics {
  const now = Math.floor(Date.now() / 1000);
  const startedAt = typeof task.started_at === 'number'
    ? task.started_at
    : (typeof task.started_at === 'string' ? parseInt(task.started_at, 10) : 0);
  const completedAt = typeof task.completed_at === 'number'
    ? task.completed_at
    : (typeof task.completed_at === 'string' ? parseInt(task.completed_at, 10) : 0);

  let duration = 0;
  if (completedAt > 0 && startedAt > 0) {
    duration = completedAt - startedAt;
  } else if (startedAt > 0) {
    duration = now - startedAt;
  }

  const outputSize = estimateOutputSize(task.result);

  // Progress: prefer real-time WS progress, then compute from time/result
  let progressPct = 0;
  if (progressInfo) {
    progressPct = Math.round(progressInfo.progress * 100);
  } else if (task.status === 'completed') {
    progressPct = 100;
  } else if (task.status === 'failed' || task.status === 'cancelled') {
    progressPct = 100;
  } else if (task.status === 'running' || task.status === 'working') {
    progressPct = 50; // indeterminate-ish
  }

  return {
    duration,
    durationLabel: formatDuration(duration),
    outputSize,
    runCount: detailRuns?.length || 0,
    progressPct: Math.min(progressPct, 100),
    progressColor: STATUS_PROGRESS_COLORS[task.status?.toLowerCase()] || '#1677ff',
  };
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface TaskDetailPanelProps {
  task: Task | null;
  progressInfo?: TaskProgressInfo | null;
  open: boolean;
  onClose: () => void;
  /** If provided, fetches full detail on open */
  fetchDetail?: boolean;
}

const TaskDetailPanel: React.FC<TaskDetailPanelProps> = ({
  task,
  progressInfo,
  open,
  onClose,
  fetchDetail = true,
}) => {
  const [fullTask, setFullTask] = useState<Task | null>(null);
  const [detailRuns, setDetailRuns] = useState<any[]>([]);
  const [detailEvents, setDetailEvents] = useState<any[]>([]);
  const [detailComments, setDetailComments] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);

  // Fetch full task detail when drawer opens
  useEffect(() => {
    if (!open || !task || !fetchDetail) return;

    setLoading(true);
    setFullTask(null);
    setDetailRuns([]);
    setDetailEvents([]);
    setDetailComments([]);

    taskAPI.getV2(task.id)
      .then((resp) => {
        setFullTask(resp.task as Task);
        setDetailRuns(resp.runs || []);
        setDetailEvents(resp.events || []);
        setDetailComments(resp.comments || []);
      })
      .catch(() => {
        // If detail fetch fails, fall back to the task prop
        setFullTask(task);
      })
      .finally(() => setLoading(false));
  }, [open, task, fetchDetail]);

  if (!task) return null;

  // Merge:
  // - fullTask from API detail (has runs/events)
  // - task from props (has WS real-time updates)
  // - progressInfo from WS progress
  const displayTask = fullTask ? { ...fullTask, ...task } : task;
  const metrics = computeMetrics(displayTask, progressInfo, detailRuns);

  return (
    <Drawer
      title={
        <Space>
          <span>任务详情</span>
          <code style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
            {displayTask.id}
          </code>
        </Space>
      }
      placement="right"
      width={580}
      open={open}
      onClose={onClose}
      loading={loading}
    >
      {loading ? (
        <div style={{ textAlign: 'center', padding: 40 }}>
          <Spin tip="加载中..." />
        </div>
      ) : (
        <>
          {/* ── 基本信息 ── */}
          <Descriptions column={1} size="small" bordered
            styles={{
              label: { width: 100, fontWeight: 500 },
              content: { wordBreak: 'break-all' },
            }}
          >
            <Descriptions.Item label="ID">
              <code>{displayTask.id}</code>
            </Descriptions.Item>
            <Descriptions.Item label="标题">
              <Text strong>{displayTask.title || '-'}</Text>
            </Descriptions.Item>
            <Descriptions.Item label="状态">
              <StatusTag status={displayTask.status || 'unknown'} />
            </Descriptions.Item>
            <Descriptions.Item label="负责人">
              <Text><UserOutlined style={{ marginRight: 4 }} />{displayTask.assignee || '-'}</Text>
            </Descriptions.Item>
          </Descriptions>

          {/* ── 进度条 ── */}
          <div style={{ marginTop: 16, padding: '12px 16px', background: 'var(--bg-secondary)', borderRadius: 8 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4, fontSize: 12 }}>
              <span style={{ fontWeight: 500 }}>
                {progressInfo?.message || '任务进度'}
              </span>
              <span style={{ color: metrics.progressColor, fontWeight: 600 }}>
                {metrics.progressPct}%
              </span>
            </div>
            <Progress
              percent={metrics.progressPct}
              strokeColor={metrics.progressColor}
              trailColor="rgba(0,0,0,0.06)"
              size="small"
              showInfo={false}
            />
          </div>

          {/* ── 指标卡片 ── */}
          <div style={{
            display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 16,
          }}>
            <div className="stat-card" style={{ padding: '12px 16px' }}>
              <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 4 }}>
                <ClockCircleOutlined style={{ marginRight: 4 }} />耗时
              </div>
              <div style={{ fontSize: 18, fontWeight: 600 }}>{metrics.durationLabel}</div>
            </div>
            <div className="stat-card" style={{ padding: '12px 16px' }}>
              <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 4 }}>
                <DatabaseOutlined style={{ marginRight: 4 }} />输出量
              </div>
              <div style={{ fontSize: 18, fontWeight: 600 }}>{metrics.outputSize}</div>
            </div>
            <div className="stat-card" style={{ padding: '12px 16px' }}>
              <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 4 }}>
                <NumberOutlined style={{ marginRight: 4 }} />运行次数
              </div>
              <div style={{ fontSize: 18, fontWeight: 600 }}>{metrics.runCount}</div>
            </div>
            <div className="stat-card" style={{ padding: '12px 16px' }}>
              <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 4 }}>优先级</div>
              <div style={{ fontSize: 18, fontWeight: 600 }}>
                <Tag color={displayTask.priority == null ? 'default' :
                  Number(displayTask.priority ?? 0) > 5 ? 'red' : Number(displayTask.priority ?? 0) > 0 ? 'orange' : 'default'
                }>
                  {displayTask.priority ?? 0}
                </Tag>
              </div>
            </div>
          </div>

          {/* ── 时间信息 ── */}
          <Descriptions column={2} size="small" bordered
            styles={{ label: { fontWeight: 500 } }}
            style={{ marginTop: 16 }}
          >
            <Descriptions.Item label="创建时间">
              {formatTime(displayTask.created_at)}
            </Descriptions.Item>
            <Descriptions.Item label="开始时间">
              {formatTime(displayTask.started_at)}
            </Descriptions.Item>
            <Descriptions.Item label="完成时间">
              {formatTime(displayTask.completed_at)}
            </Descriptions.Item>
            <Descriptions.Item label="最后更新">
              {formatTime(displayTask.updated_at)}
            </Descriptions.Item>
          </Descriptions>

          {/* ── 结果/错误 ── */}
          {displayTask.result && (
            <div style={{ marginTop: 16 }}>
              <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', marginBottom: 8 }}>执行结果</div>
              <pre style={{
                fontSize: 12, background: 'var(--bg-secondary)', padding: 12,
                borderRadius: 6, maxHeight: 300, overflow: 'auto', margin: 0,
                whiteSpace: 'pre-wrap', wordBreak: 'break-all',
              }}>
                {typeof displayTask.result === 'string'
                  ? displayTask.result
                  : JSON.stringify(displayTask.result, null, 2)}
              </pre>
            </div>
          )}

          {/* ── Tabs: Timeline / Provenance / Comments ── */}
          <div style={{ marginTop: 20 }}>
            <Tabs
              defaultActiveKey="timeline"
              items={[
                {
                  key: 'timeline',
                  label: (
                    <span>
                      📋 任务时间线
                      <span style={{ fontSize: 11, color: 'var(--text-secondary)', marginLeft: 6, fontWeight: 400 }}>
                        ({detailEvents.length} 个事件)
                      </span>
                    </span>
                  ),
                  children: detailEvents.length === 0 ? (
                    <div style={{ color: 'var(--text-tertiary)', fontSize: 12, padding: '12px 0' }}>
                      暂无事件记录
                    </div>
                  ) : (
                    <TaskTimeline events={detailEvents} />
                  ),
                },
                {
                  key: 'provenance',
                  label: (
                    <span>
                      🔗 溯源链
                    </span>
                  ),
                  children: <ProvenanceTab taskId={displayTask.id} />,
                },
                ...(detailComments.length > 0
                  ? [{
                      key: 'comments',
                      label: (
                        <span>
                          💬 评论 ({detailComments.length})
                        </span>
                      ),
                      children: detailComments.map((c: any) => (
                        <div key={c.id} style={{
                          padding: '8px 12px', marginBottom: 8,
                          background: 'var(--bg-secondary)', borderRadius: 6,
                        }}>
                          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: 'var(--text-tertiary)', marginBottom: 4 }}>
                            <Text strong style={{ fontSize: 11 }}>{c.author || 'anonymous'}</Text>
                            <span>{formatTime(c.created_at)}</span>
                          </div>
                          <div style={{ fontSize: 13, whiteSpace: 'pre-wrap' }}>{c.body}</div>
                        </div>
                      )),
                    }]
                  : []),
              ]}
            />
          </div>
        </>
      )}
    </Drawer>
  );
};

export default TaskDetailPanel;