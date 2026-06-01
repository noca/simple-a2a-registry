import React, { useEffect, useState, useCallback } from 'react';
import {
  Card, Table, Button, Space, Select, Tag, Row, Col, Spin, Empty, Modal,
} from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { listAuditLog } from '../api/client';
import PageTitle from '../components/PageTitle';

interface AuditEvent {
  id?: number;
  event_type: string;
  actor: string;
  target?: string;
  detail?: string;
  success: boolean;
  created_at: number;
  timestamp?: number;
  request_id?: string;
  extra?: Record<string, any>;
}

const EVENT_COLORS: Record<string, string> = {
  AGENT_REGISTER: '#007AFF',
  AGENT_DEREGISTER: '#FF453A',
  AGENT_UPDATE: '#30D158',
  TASK_DISPATCH: '#BF5AF2',
  CLIENT_CREATE: '#007AFF',
  CLIENT_DELETE: '#FF453A',
  USER_LOGIN: '#30D158',
  USER_LOGOUT: '#86868B',
  TOKEN_ISSUE: '#FF9F0A',
};

const AuditLog: React.FC = () => {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [stats, setStats] = useState<Record<string, unknown>>({});
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [typeFilter, setTypeFilter] = useState('');
  const [detailOpen, setDetailOpen] = useState<string | null>(null);

  const fetchEvents = useCallback(async () => {
    setLoading(true);
    try {
      const params: Record<string, any> = { limit: 100 };
      if (typeFilter) params.event_type = typeFilter;
      const data = await listAuditLog(params);
      // Defensive: validate response shape
      if (data && typeof data === 'object') {
        setEvents(Array.isArray(data.events) ? data.events : []);
        setTotal(typeof data.total === 'number' ? data.total : 0);
        if (data.stats && typeof data.stats === 'object') setStats(data.stats as Record<string, unknown>);
      } else {
        setEvents([]);
        setStats({});
      }
    } catch {
      setEvents([]);
      setStats({});
    } finally {
      setLoading(false);
    }
  }, [typeFilter]);

  useEffect(() => { fetchEvents(); }, [fetchEvents]);

  const eventTypes = [...new Set(events.map((e) => e.event_type))].filter(Boolean);

  const columns = [
    {
      title: 'Time', key: 'time', width: 160,
      render: (_: any, r: AuditEvent) => {
        const ts = r.created_at || r.timestamp || 0;
        return ts ? new Date(ts * 1000).toLocaleString() : '-';
      },
    },
    {
      title: 'Event Type', dataIndex: 'event_type', key: 'event_type',
      render: (t: string) => (
        <Tag style={{ borderRadius: 4, color: EVENT_COLORS[t] || '#86868B', border: 'none', background: `${EVENT_COLORS[t] || '#86868B'}15` }}>
          {String(t ?? 'UNKNOWN')}
        </Tag>
      ),
    },
    { title: 'Actor', dataIndex: 'actor', key: 'actor', render: (a: string) => <span style={{ fontWeight: 500 }}>{a}</span> },
    { title: 'Target', dataIndex: 'target', key: 'target', render: (t: string) => t ? <code style={{ fontSize: 11 }}>{t}</code> : '-' },
    {
      title: 'Detail', dataIndex: 'detail', key: 'detail',
      render: (d: string) => (
        <div style={{ maxWidth: 250, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: 12, color: 'var(--text-secondary)' }}>
          {d || '-'}
        </div>
      ),
    },
    {
      title: 'Result', dataIndex: 'success', key: 'success',
      render: (s: boolean) => s
        ? <Tag color="green" style={{ borderRadius: 4 }}>Success</Tag>
        : <Tag color="red" style={{ borderRadius: 4 }}>Failed</Tag>,
    },
    {
      title: 'Actions', key: 'actions',
      render: (_: any, r: AuditEvent) => (
        <Button size="small" type="link" onClick={() => setDetailOpen(JSON.stringify(r, null, 2))}>
          JSON
        </Button>
      ),
    },
  ];

  return (
    <div>
      <PageTitle
        title="Audit Log"
        count={total}
        label="events"
        extra={<Button icon={<ReloadOutlined />} onClick={fetchEvents}>Refresh</Button>}
      />

      <Card bodyStyle={{ padding: '12px 16px' }} style={{ marginBottom: 16, borderRadius: 10 }}>
        <Row gutter={12} align="middle">
          <Col>
            <span style={{ fontSize: 12, color: 'var(--text-secondary)', marginRight: 8 }}>Event Type:</span>
            <Select value={typeFilter} onChange={setTypeFilter} style={{ width: 200 }} placeholder="All Types" allowClear>
              <Select.Option value="">All Types</Select.Option>
              {eventTypes.map((t) => (
                <Select.Option key={t} value={t}>{t}</Select.Option>
              ))}
            </Select>
          </Col>
        </Row>
      </Card>

      <Spin spinning={loading}>
        {events.length === 0 && !loading ? (
          <Card style={{ borderRadius: 10, textAlign: 'center', padding: 40 }}>
            <Empty description="No audit events found" />
          </Card>
        ) : (
          <>
            {stats && Object.keys(stats).length > 0 && (
              <Card size="small" bodyStyle={{ padding: '8px 16px' }} style={{ marginBottom: 12, borderRadius: 10 }}>
                <Space split={<span style={{ color: 'var(--separator)' }}>·</span>} wrap>
                  {Object.entries(stats).filter(([_, v]) => typeof v === 'number' || typeof v === 'string').map(([k, v]) => (
                    <span key={k} style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
                      {k}: <span style={{ fontWeight: 600, color: 'var(--text)' }}>{String(v)}</span>
                    </span>
                  ))}
                </Space>
              </Card>
            )}
            <Card bodyStyle={{ padding: 0 }} style={{ borderRadius: 10 }}>
              <Table
                dataSource={events}
                columns={columns}
                rowKey={(r) => String(r.id || r.created_at || Math.random())}
                pagination={{ pageSize: 25, showTotal: (t) => `${t} events` }}
                size="middle"
              />
            </Card>
          </>
        )}
      </Spin>

      <Modal
        title="Event Detail (JSON)"
        open={!!detailOpen}
        onCancel={() => setDetailOpen(null)}
        footer={null}
        width={600}
      >
        <pre style={{
          fontSize: 11,
          fontFamily: 'SF Mono, Menlo, monospace',
          background: 'var(--bg)',
          padding: 16,
          borderRadius: 8,
          maxHeight: 400,
          overflow: 'auto',
          whiteSpace: 'pre-wrap',
        }}>
          {detailOpen}
        </pre>
      </Modal>
    </div>
  );
};

export default AuditLog;