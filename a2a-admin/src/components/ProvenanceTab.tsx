/**
 * ProvenanceTab — 任务溯源链面板
 *
 * 在任务详情 Drawer 中展示 Provenance Trace 信息：
 * origin_agent, root_task_id, depth, hops Timeline
 */
import React, { useEffect, useState } from 'react';
import { Spin, Tag, Typography, Descriptions, Empty } from 'antd';
import { taskAPI } from '../api/client';

const { Text } = Typography;

/* ------------------------------------------------------------------ */
/* Types                                                              */
/* ------------------------------------------------------------------ */

interface ProvenanceHop {
  from_agent: string;
  to_agent: string;
  action: string;
  scope_at?: string;
  timestamp?: number;
}

interface ProvenanceChain {
  chain_id: string;
  origin_agent: string;
  origin_tenant?: string;
  root_task_id: string;
  depth: number;
  hops: ProvenanceHop[];
}

interface ProvenanceResponse {
  task_id: string;
  provenance?: ProvenanceChain;
}

/* ------------------------------------------------------------------ */
/* Component                                                          */
/* ------------------------------------------------------------------ */

interface ProvenanceTabProps {
  taskId: string;
}

const ProvenanceTab: React.FC<ProvenanceTabProps> = ({ taskId }) => {
  const [provenance, setProvenance] = useState<ProvenanceChain | null>(null);
  const [loading, setLoading] = useState(true);
  const [hasChain, setHasChain] = useState(true);

  useEffect(() => {
    if (!taskId) return;

    setLoading(true);
    setProvenance(null);
    setHasChain(true);

    taskAPI.getProvenance(taskId)
      .then((resp: ProvenanceResponse) => {
        if (resp?.provenance) {
          setProvenance(resp.provenance);
          setHasChain(true);
        } else {
          setHasChain(false);
        }
      })
      .catch(() => {
        setHasChain(false);
      })
      .finally(() => setLoading(false));
  }, [taskId]);

  if (loading) {
    return (
      <div style={{ textAlign: 'center', padding: 40 }}>
        <Spin tip="加载中..." />
      </div>
    );
  }

  if (!hasChain || !provenance) {
    return (
      <div style={{ padding: 24, textAlign: 'center' }}>
        <Empty description="No provenance record" />
        <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginTop: 8 }}>
          This task has no delegation provenance chain recorded.
        </div>
      </div>
    );
  }

  const formatTime = (ts?: number) =>
    ts ? new Date(ts * 1000).toLocaleString() : '-';

  return (
    <div>
      {/* ── Provenance Summary ── */}
      <Descriptions column={1} size="small" bordered
        styles={{
          label: { width: 130, fontWeight: 500 },
          content: { wordBreak: 'break-all' },
        }}
        style={{ marginBottom: 16 }}
      >
        <Descriptions.Item label="Chain ID">
          <code style={{ fontSize: 11, color: '#86868B' }}>{provenance.chain_id}</code>
        </Descriptions.Item>
        <Descriptions.Item label="Origin Agent">
          <Text strong>{provenance.origin_agent}</Text>
        </Descriptions.Item>
        <Descriptions.Item label="Origin Tenant">
          <Tag style={{ borderRadius: 4, fontSize: 11 }}>
            {provenance.origin_tenant || 'default'}
          </Tag>
        </Descriptions.Item>
        <Descriptions.Item label="Root Task ID">
          <code style={{ fontSize: 11, cursor: 'pointer', color: 'var(--primary)' }}>
            {provenance.root_task_id}
          </code>
        </Descriptions.Item>
        <Descriptions.Item label="Depth">
          <span style={{ fontWeight: 600 }}>{provenance.depth}</span>
        </Descriptions.Item>
      </Descriptions>

      {/* ── Delegation Hops Timeline ── */}
      {provenance.hops && provenance.hops.length > 0 && (
        <div>
          <div style={{
            fontSize: 13, fontWeight: 600, marginBottom: 12,
            display: 'flex', alignItems: 'center', gap: 6,
          }}>
            Delegation Hops
            <span style={{ fontSize: 11, color: 'var(--text-secondary)', fontWeight: 400 }}>
              ({provenance.hops.length} hops)
            </span>
          </div>

          {/* Timeline */}
          <div style={{ position: 'relative', paddingLeft: 24 }}>
            {/* Vertical line */}
            <div style={{
              position: 'absolute', left: 8, top: 4, bottom: 4,
              width: 2, background: 'var(--border, #e0e0e0)', borderRadius: 1,
            }} />

            {provenance.hops.map((hop, idx) => (
              <div key={idx} style={{ position: 'relative', marginBottom: 16 }}>
                {/* Dot */}
                <div style={{
                  position: 'absolute', left: -18, top: 6,
                  width: 10, height: 10, borderRadius: '50%',
                  background: idx === 0 ? '#30D158' : '#007AFF',
                  border: '2px solid var(--bg)',
                  zIndex: 1,
                }} />
                {/* Content */}
                <div style={{
                  background: 'var(--bg-secondary)',
                  borderRadius: 8,
                  padding: '10px 14px',
                  border: '1px solid var(--border)',
                }}>
                  <div style={{
                    display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap',
                  }}>
                    <Text strong style={{ fontFamily: 'SF Mono, Menlo, monospace', fontSize: 13 }}>
                      {hop.from_agent}
                    </Text>
                    <span style={{ color: 'var(--text-tertiary)' }}>→</span>
                    <Text strong style={{ fontFamily: 'SF Mono, Menlo, monospace', fontSize: 13 }}>
                      {hop.to_agent}
                    </Text>
                    <Tag color={hop.action === 'claim' ? 'blue' : 'purple'}
                      style={{ borderRadius: 4, fontSize: 11, marginLeft: 4 }}>
                      {hop.action}
                    </Tag>
                  </div>
                  <div style={{
                    display: 'flex', gap: 12, marginTop: 6, fontSize: 12,
                    color: 'var(--text-secondary)',
                  }}>
                    {hop.scope_at && (
                      <Tag style={{ borderRadius: 4, fontSize: 10, opacity: 0.7 }}>
                        scope: {hop.scope_at}
                      </Tag>
                    )}
                    {hop.timestamp && (
                      <span>{formatTime(hop.timestamp)}</span>
                    )}
                  </div>
                </div>
              </div>
            ))}

            {/* End of chain marker */}
            <div style={{ position: 'relative', paddingLeft: 0 }}>
              <div style={{
                position: 'absolute', left: -18, top: 4,
                width: 10, height: 10, borderRadius: '50%',
                background: '#86868B', border: '2px solid var(--bg)',
                zIndex: 1,
              }} />
              <div style={{
                fontSize: 11, color: 'var(--text-tertiary)',
                padding: '4px 0',
              }}>
                End of chain
              </div>
            </div>
          </div>
        </div>
      )}

      {/* No hops edge case */}
      {(!provenance.hops || provenance.hops.length === 0) && (
        <div style={{ fontSize: 12, color: 'var(--text-tertiary)', textAlign: 'center', padding: 12 }}>
          No delegation hops recorded in this chain.
        </div>
      )}
    </div>
  );
};

export default ProvenanceTab;
