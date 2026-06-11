import React, { useEffect, useState, useCallback } from 'react';
import {
  Card, Table, Button, Select, Tag, Row, Col, Spin, Empty, Modal,
} from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { securityEventsAPI } from '../api/client';
import PageTitle from '../components/PageTitle';

/* ------------------------------------------------------------------ */
/* Types                                                              */
/* ------------------------------------------------------------------ */

interface SecurityEvent {
  id?: string | number;
  timestamp: number;
  event_type: string;
  actor: string;
  target?: string;
  tenant?: string;
  decision: string;
  reason?: string;
  scope_used?: string;
  extra?: Record<string, any>;
  [key: string]: any;
}

/* ------------------------------------------------------------------ */
/* Constants                                                          */
/* ------------------------------------------------------------------ */

const EVENT_COLORS: Record<string, string> = {
  AUTH_FAILURE: '#FF453A',
  AUTH_DENIED: '#FF453A',
  AUTH_ALLOWED: '#30D158',
  DELEGATION_CREATED: '#007AFF',
  DELEGATION_REVOKED: '#BF5AF2',
  DELEGATION_EXPIRED: '#86868B',
  PROVENANCE_VIOLATION: '#FF9F0A',
  SCOPE_VIOLATION: '#FF9F0A',
  PERMISSION_DENIED: '#FF453A',
  TOKEN_CREATED: '#30D158',
  TOKEN_REVOKED: '#BF5AF2',
  SECURITY_AUDIT: '#007AFF',
};

const DECISION_COLORS: Record<string, string> = {
  allow: '#30D158',
  deny: '#FF453A',
  block: '#FF9F0A',
};

/* ------------------------------------------------------------------ */
/* Component                                                          */
/* ------------------------------------------------------------------ */

const SecurityEvents: React.FC = () => {
  const [events, setEvents] = useState<SecurityEvent[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [typeFilter, setTypeFilter] = useState('');
  const [actorFilter, setActorFilter] = useState('');
  const [tenantFilter, setTenantFilter] = useState('');
  const [page, setPage] = useState(1);
  const [detailOpen, setDetailOpen] = useState<string | null>(null);
  const pageSize = 25;

  const fetchEvents = useCallback(async (pageNum: number) => {
    setLoading(true);
    try {
      const params: Record<string, string> = {
        limit: String(pageSize),
        offset: String((pageNum - 1) * pageSize),
      };
      if (typeFilter) params.event_type = typeFilter;
      if (actorFilter) params.actor = actorFilter;
      if (tenantFilter) params.tenant = tenantFilter;
      const data = await securityEventsAPI.list(params);
      if (data && typeof data === 'object') {
        setEvents(Array.isArray(data.events) ? data.events : []);
        setTotal(typeof data.total === 'number' ? data.total : 0);
      } else {
        setEvents([]);
      }
    } catch {
      setEvents([]);
    } finally {
      setLoading(false);
    }
  }, [typeFilter, actorFilter, tenantFilter]);

  useEffect(() => { fetchEvents(page); }, [fetchEvents, page]);

  const handleRefresh = () => { setPage(1); fetchEvents(1); };

  // Dynamic filter options from current data
  const eventTypes = [...new Set(events.map((e) => e.event_type))].filter(Boolean);
  const actors = [...new Set(events.map((e) => e.actor))].filter(Boolean);
  const tenants = [...new Set(events.map((e) => e.tenant).filter(Boolean))];

  const decisionTag = (d: string) => {
    const color = DECISION_COLORS[d?.toLowerCase()] || '#86868B';
    return (
      <Tag style={{
        borderRadius: 4, color: '#fff', border: 'none', background: color,
        fontWeight: 600,
      }}>
        {d?.toUpperCase() || 'UNKNOWN'}
      </Tag>
    );
  };

  const columns = [
    {
      title: 'Time', key: 'time', width: 170,
      render: (_: any, r: SecurityEvent) => {
        const ts = r.timestamp || 0;
        return ts ? new Date(ts * 1000).toLocaleString() : '-';
      },
    },
    {
      title: 'Event Type', dataIndex: 'event_type', key: 'event_type',
      render: (t: string) => {
        const color = EVENT_COLORS[t] || '#86868B';
        return (
          <Tag style={{
            borderRadius: 4, color, border: 'none',
            background: `${color}15`,
            fontWeight: 500,
          }}>
            {String(t ?? 'UNKNOWN')}
          </Tag>
        );
      },
    },
    {
      title: 'Actor', dataIndex: 'actor', key: 'actor',
      render: (a: string) => (
        <span style={{ fontWeight: 600, fontFamily: 'SF Mono, Menlo, monospace' }}>{a}</span>
      ),
    },
    {
      title: 'Target', dataIndex: 'target', key: 'target',
      render: (t: string) => t ? <code style={{ fontSize: 11 }}>{t}</code> : '-',
    },
    {
      title: 'Tenant', dataIndex: 'tenant', key: 'tenant',
      render: (t: string) => t ? (
        <Tag style={{ borderRadius: 4, fontSize: 11, opacity: 0.75 }}>{t}</Tag>
      ) : '-',
    },
    {
      title: 'Decision', dataIndex: 'decision', key: 'decision',
      render: (d: string) => decisionTag(d),
    },
    {
      title: 'Reason', dataIndex: 'reason', key: 'reason',
      render: (r: string) => r ? (
        <div style={{
          maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis',
          whiteSpace: 'nowrap', fontSize: 12, color: 'var(--text-secondary)',
        }}>
          {r}
        </div>
      ) : '-',
    },
    {
      title: 'Scope Used', dataIndex: 'scope_used', key: 'scope_used',
      render: (s: string) => s ? (
        <Tag style={{ borderRadius: 4, fontSize: 11, opacity: 0.7 }}>{s}</Tag>
      ) : '-',
    },
    {
      title: 'Actions', key: 'actions',
      render: (_: any, r: SecurityEvent) => (
        <Button size="small" type="link" onClick={() => setDetailOpen(JSON.stringify(r, null, 2))}>
          JSON
        </Button>
      ),
    },
  ];

  return (
    <div>
      <PageTitle
        title="🔒 Security Events"
        count={total}
        label="events"
        extra={<Button icon={<ReloadOutlined />} onClick={handleRefresh}>Refresh</Button>}
      />

      {/* Filters */}
      <Card bodyStyle={{ padding: '12px 16px' }} style={{ marginBottom: 16, borderRadius: 10 }}>
        <Row gutter={16} align="middle">
          <Col>
            <span style={{ fontSize: 12, color: 'var(--text-secondary)', marginRight: 8 }}>Event Type:</span>
            <Select
              value={typeFilter}
              onChange={(v) => { setTypeFilter(v); setPage(1); }}
              style={{ width: 180 }}
              placeholder="All Types"
              allowClear
            >
              <Select.Option value="">All Types</Select.Option>
              {eventTypes.map((t) => (
                <Select.Option key={t} value={t}>{t}</Select.Option>
              ))}
            </Select>
          </Col>
          <Col>
            <span style={{ fontSize: 12, color: 'var(--text-secondary)', marginRight: 8 }}>Actor:</span>
            <Select
              value={actorFilter}
              onChange={(v) => { setActorFilter(v); setPage(1); }}
              style={{ width: 180 }}
              placeholder="All Actors"
              allowClear
              showSearch
              filterOption={(input, option) =>
                (option?.label as string || '').toLowerCase().includes(input.toLowerCase())
              }
            >
              <Select.Option value="">All Actors</Select.Option>
              {actors.map((a) => (
                <Select.Option key={a} value={a}>{a}</Select.Option>
              ))}
            </Select>
          </Col>
          <Col>
            <span style={{ fontSize: 12, color: 'var(--text-secondary)', marginRight: 8 }}>Tenant:</span>
            <Select
              value={tenantFilter}
              onChange={(v) => { setTenantFilter(v); setPage(1); }}
              style={{ width: 180 }}
              placeholder="All Tenants"
              allowClear
            >
              <Select.Option value="">All Tenants</Select.Option>
              {tenants.map((t) => (
                <Select.Option key={t} value={t}>{t}</Select.Option>
              ))}
            </Select>
          </Col>
        </Row>
      </Card>

      {/* Table */}
      <Spin spinning={loading}>
        {events.length === 0 && !loading ? (
          <Card style={{ borderRadius: 10, textAlign: 'center', padding: 40 }}>
            <Empty description="No security events found" />
          </Card>
        ) : (
          <Card bodyStyle={{ padding: 0 }} style={{ borderRadius: 10 }}>
            <Table
              dataSource={events}
              columns={columns}
              rowKey={(r) => String(r.id || r.timestamp || Math.random())}
              pagination={{
                current: page,
                pageSize,
                total,
                onChange: (p) => setPage(p),
                showTotal: (t) => `${t} events`,
                showSizeChanger: false,
              }}
              size="middle"
            />
          </Card>
        )}
      </Spin>

      {/* JSON Detail Modal */}
      <Modal
        title="Security Event Detail (JSON)"
        open={!!detailOpen}
        onCancel={() => setDetailOpen(null)}
        footer={null}
        width={680}
      >
        <pre style={{
          fontSize: 11,
          fontFamily: 'SF Mono, Menlo, monospace',
          background: 'var(--bg)',
          padding: 16,
          borderRadius: 8,
          maxHeight: 450,
          overflow: 'auto',
          whiteSpace: 'pre-wrap',
        }}>
          {detailOpen}
        </pre>
      </Modal>
    </div>
  );
};

export default SecurityEvents;
