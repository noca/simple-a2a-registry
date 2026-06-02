/**
 * TaskTimeline — 任务状态变迁时间线组件
 *
 * 展示 task_events 的事件序列，按时间倒序排列。
 * 每个事件带图标、事件类型中文描述、时间戳、运行 ID。
 *
 * 使用方式：
 *   <TaskTimeline events={events} />
 */
import React from 'react';
import { Tag, Tooltip } from 'antd';
import {
  PlusCircleOutlined,
  PlayCircleOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  LockOutlined,
  UnlockOutlined,
  HeartOutlined,
  EditOutlined,
  DeleteOutlined,
  SwapOutlined,
  MinusCircleOutlined,
  ClockCircleOutlined,
} from '@ant-design/icons';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface TimelineEvent {
  id?: number;
  task_id?: string;
  kind: string;
  payload?: string | null;
  run_id?: number | null;
  created_at: number;
}

// ---------------------------------------------------------------------------
// Event kind configuration
// ---------------------------------------------------------------------------

interface EventConfig {
  icon: React.ReactNode;
  label: string;
  color: string;
}

const EVENT_CONFIGS: Record<string, EventConfig> = {
  created: {
    icon: <PlusCircleOutlined />,
    label: '创建',
    color: '#1677ff',
  },
  claimed: {
    icon: <PlayCircleOutlined />,
    label: '认领',
    color: '#722ed1',
  },
  started: {
    icon: <PlayCircleOutlined />,
    label: '开始执行',
    color: '#722ed1',
  },
  heartbeat: {
    icon: <HeartOutlined />,
    label: '心跳',
    color: '#13c2c2',
  },
  completed: {
    icon: <CheckCircleOutlined />,
    label: '完成',
    color: '#52c41a',
  },
  failed: {
    icon: <CloseCircleOutlined />,
    label: '失败',
    color: '#ff4d4f',
  },
  blocked: {
    icon: <LockOutlined />,
    label: '阻塞',
    color: '#fa8c16',
  },
  unblocked: {
    icon: <UnlockOutlined />,
    label: '解除阻塞',
    color: '#52c41a',
  },
  commented: {
    icon: <EditOutlined />,
    label: '评论',
    color: '#1677ff',
  },
  updated: {
    icon: <EditOutlined />,
    label: '更新',
    color: '#1677ff',
  },
  archived: {
    icon: <DeleteOutlined />,
    label: '归档',
    color: '#8c8c8c',
  },
  dependency_promoted: {
    icon: <SwapOutlined />,
    label: '依赖就绪 → 提升',
    color: '#1677ff',
  },
  cancelled: {
    icon: <MinusCircleOutlined />,
    label: '取消',
    color: '#8c8c8c',
  },
};

const DEFAULT_EVENT_CONFIG: EventConfig = {
  icon: <ClockCircleOutlined />,
  label: '事件',
  color: '#8c8c8c',
};

function getEventConfig(kind: string): EventConfig {
  return EVENT_CONFIGS[kind] || DEFAULT_EVENT_CONFIG;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString();
}

function formatPayload(payload?: string | null): string {
  if (!payload) return '';
  try {
    const parsed = JSON.parse(payload);
    if (typeof parsed === 'object') {
      return Object.entries(parsed)
        .map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`)
        .join(', ');
    }
    return String(parsed);
  } catch {
    return payload;
  }
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface TaskTimelineProps {
  events: TimelineEvent[];
  /** Max items to show (0 = all, default 50) */
  maxItems?: number;
}

const TaskTimeline: React.FC<TaskTimelineProps> = ({ events, maxItems = 50 }) => {
  if (!events || events.length === 0) {
    return (
      <div style={{ color: 'var(--text-tertiary)', fontSize: 12, padding: '12px 0' }}>
        暂无事件记录
      </div>
    );
  }

  // Sort by created_at descending (newest first)
  const sorted = [...events]
    .sort((a, b) => b.created_at - a.created_at)
    .slice(0, maxItems || undefined);

  return (
    <div style={{ position: 'relative' }}>
      {/* Vertical line */}
      <div style={{
        position: 'absolute',
        left: 11,
        top: 0,
        bottom: 0,
        width: 2,
        background: 'rgba(0,0,0,0.06)',
        borderRadius: 1,
      }} />

      <div style={{ display: 'flex', flexDirection: 'column', gap: 0 }}>
        {sorted.map((event, idx) => {
          const cfg = getEventConfig(event.kind);
          const isLast = idx === sorted.length - 1;

          return (
            <div key={`${event.id || idx}-${event.created_at}`}
              style={{
                display: 'flex',
                gap: 12,
                paddingBottom: isLast ? 0 : 16,
                position: 'relative',
              }}
            >
              {/* Icon circle */}
              <div style={{
                width: 24,
                height: 24,
                borderRadius: '50%',
                background: cfg.color,
                color: '#fff',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: 12,
                flexShrink: 0,
                zIndex: 1,
                position: 'relative',
              }}>
                {cfg.icon}
              </div>

              {/* Content */}
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 6,
                  flexWrap: 'wrap',
                }}>
                  <span style={{ fontSize: 13, fontWeight: 500 }}>{cfg.label}</span>
                  {event.run_id && (
                    <Tag style={{ fontSize: 10, lineHeight: '16px', padding: '0 4px', margin: 0 }}>
                      run #{event.run_id}
                    </Tag>
                  )}
                  <span style={{ fontSize: 11, color: 'var(--text-tertiary)', marginLeft: 'auto' }}>
                    {formatTime(event.created_at)}
                  </span>
                </div>

                {event.payload && (
                  <Tooltip title={formatPayload(event.payload)}>
                    <div style={{
                      fontSize: 11,
                      color: 'var(--text-secondary)',
                      marginTop: 2,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                      maxWidth: 360,
                    }}>
                      {formatPayload(event.payload)}
                    </div>
                  </Tooltip>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};

export default TaskTimeline;