import React, { useEffect, useState, useCallback } from 'react';
import {
  Card, Table, Button, Modal, Form, Input, Select, Space, Tag, Typography, Spin, message, Empty, InputNumber,
} from 'antd';
import { PlusOutlined, ReloadOutlined, DeleteOutlined, CloseOutlined } from '@ant-design/icons';
import { delegationAPI, agentAPI } from '../api/client';
import PageTitle from '../components/PageTitle';

const { Text } = Typography;

/* ------------------------------------------------------------------ */
/* Types                                                              */
/* ------------------------------------------------------------------ */

interface Delegation {
  id: string;
  source_agent_id: string;
  target_agent_id: string;
  allowed_actions: string[];
  max_depth?: number;
  is_active?: boolean;
  created_at?: number;
  updated_at?: number;
}

interface Agent {
  agent_id: string;
  display_name?: string;
  metadata?: Record<string, any>;
}

/* ------------------------------------------------------------------ */
/* Constants                                                          */
/* ------------------------------------------------------------------ */

const AVAILABLE_SCOPES = [
  'task:*',
  'task:read',
  'task:write',
  'task:admin',
  'agent:*',
  'agent:read',
  'agent:register',
  'agent:admin',
];

/* ------------------------------------------------------------------ */
/* Component                                                          */
/* ------------------------------------------------------------------ */

const AuthorizationMatrix: React.FC = () => {
  const [delegations, setDelegations] = useState<Delegation[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [createOpen, setCreateOpen] = useState(false);
  const [filterSource, setFilterSource] = useState('');
  const [filterTarget, setFilterTarget] = useState('');
  const [filterScope, setFilterScope] = useState('');
  const [form] = Form.useForm();

  const fetchDelegations = useCallback(async () => {
    setLoading(true);
    try {
      const params: Record<string, string> = {};
      if (filterSource) params.source = filterSource;
      if (filterTarget) params.target = filterTarget;
      if (filterScope) params.scope = filterScope;
      const data = await delegationAPI.list(params);
      setDelegations(Array.isArray(data) ? data : []);
    } catch {
      setDelegations([]);
    } finally {
      setLoading(false);
    }
  }, [filterSource, filterTarget, filterScope]);

  const fetchAgents = useCallback(async () => {
    try {
      const data = await agentAPI.list();
      setAgents(Array.isArray(data) ? data : []);
    } catch {
      setAgents([]);
    }
  }, []);

  useEffect(() => {
    fetchDelegations();
    fetchAgents();
  }, [fetchDelegations, fetchAgents]);

  const handleCreate = async (values: any) => {
    try {
      await delegationAPI.create({
        source_agent_id: values.source_agent_id,
        target_agent_id: values.target_agent_id,
        allowed_actions: values.allowed_actions || [],
        max_depth: values.max_depth ?? 3,
      });
      message.success('授权创建成功');
      setCreateOpen(false);
      form.resetFields();
      fetchDelegations();
    } catch (err: any) {
      message.error(err?.response?.data?.detail || '创建授权失败');
    }
  };

  const handleDelete = (delegation: Delegation) => {
    Modal.confirm({
      title: '撤销授权',
      content: `确定撤销 ${delegation.source_agent_id} → ${delegation.target_agent_id} 的授权吗？此操作不可撤销。`,
      okText: '撤销',
      okType: 'danger',
      cancelText: '取消',
      onOk: async () => {
        try {
          await delegationAPI.remove(delegation.id);
          message.success('授权已撤销');
          fetchDelegations();
        } catch (err: any) {
          message.error(err?.response?.data?.detail || '撤销授权失败');
        }
      },
    });
  };

  // Agent display name / id helper
  const agentDisplay = (id: string) => {
    const agent = agents.find((a) => a.agent_id === id);
    return agent?.display_name || agent?.metadata?.name || id;
  };

  const columns = [
    {
      title: '源 Agent', key: 'source_agent_id', dataIndex: 'source_agent_id',
      width: 200,
      render: (id: string) => (
        <span style={{ fontWeight: 600, fontFamily: 'SF Mono, Menlo, monospace', fontSize: 12 }}>
          {agentDisplay(id)}
        </span>
      ),
    },
    {
      title: '', key: 'arrow', width: 30,
      render: () => <span style={{ color: 'var(--text-secondary)', fontSize: 16 }}>→</span>,
    },
    {
      title: '目标 Agent', key: 'target_agent_id', dataIndex: 'target_agent_id',
      width: 200,
      render: (id: string) => (
        <span style={{ fontWeight: 600, fontFamily: 'SF Mono, Menlo, monospace', fontSize: 12 }}>
          {agentDisplay(id)}
        </span>
      ),
    },
    {
      title: '授权范围 (Scopes)', key: 'allowed_actions',
      render: (_: any, r: Delegation) => {
        const scopes = r.allowed_actions || [];
        return (
          <Space size={4} wrap>
            {scopes.map((s: string) => (
              <Tag key={s} color="blue" style={{ borderRadius: 4, fontSize: 10, lineHeight: '20px' }}>{s}</Tag>
            ))}
            {scopes.length === 0 && <Text type="secondary">-</Text>}
          </Space>
        );
      },
    },
    {
      title: '最大深度', dataIndex: 'max_depth', key: 'max_depth', width: 100,
      render: (d: number) => d ? <Tag style={{ borderRadius: 4 }}>{d}</Tag> : '-',
    },
    {
      title: '状态', dataIndex: 'is_active', key: 'is_active', width: 90,
      render: (a: boolean) => a !== false ? (
        <Tag color="green" style={{ borderRadius: 4 }}>启用</Tag>
      ) : (
        <Tag color="red" style={{ borderRadius: 4 }}>已撤销</Tag>
      ),
    },
    {
      title: '创建时间', dataIndex: 'created_at', key: 'created_at', width: 170,
      render: (t: number) => t ? new Date(t * 1000).toLocaleString() : '-',
    },
    {
      title: '操作', key: 'actions', width: 80,
      render: (_: any, r: Delegation) => (
        <Button
          size="small"
          danger
          icon={<DeleteOutlined />}
          onClick={() => handleDelete(r)}
          disabled={r.is_active === false}
        />
      ),
    },
  ];

  // Filter: get unique source agents for the source filter dropdown
  const sourceAgents = [...new Set(delegations.map((d) => d.source_agent_id))].filter(Boolean);
  const targetAgents = [...new Set(delegations.map((d) => d.target_agent_id))].filter(Boolean);

  // Agent dropdown options for create form
  const agentOptions = agents.map((a) => ({
    label: a.display_name || a.metadata?.name || a.agent_id,
    value: a.agent_id,
  }));

  // All scope tags (union across all delegations)
  const allScopes = [...new Set(delegations.flatMap((d) => d.allowed_actions || []))].filter(Boolean);

  return (
    <div>
      <PageTitle
        title="🔐 授权矩阵"
        count={delegations.length}
        label="授权策略"
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={fetchDelegations}>刷新</Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => { setCreateOpen(true); form.resetFields(); }}>
              创建授权
            </Button>
          </Space>
        }
      />

      {/* Filters */}
      <Card bodyStyle={{ padding: '12px 16px' }} style={{ marginBottom: 16, borderRadius: 10 }}>
        <Space wrap size={12}>
          <Space size={4}>
            <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>源 Agent:</span>
            <Select
              value={filterSource}
              onChange={(v) => { setFilterSource(v); }}
              style={{ width: 180 }}
              placeholder="全部"
              allowClear
              showSearch
              filterOption={(input, option) =>
                (option?.label as string || '').toLowerCase().includes(input.toLowerCase())
              }
            >
              <Select.Option value="">全部</Select.Option>
              {sourceAgents.map((a) => (
                <Select.Option key={a} value={a}>{agentDisplay(a)}</Select.Option>
              ))}
            </Select>
          </Space>
          <Space size={4}>
            <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>目标 Agent:</span>
            <Select
              value={filterTarget}
              onChange={(v) => { setFilterTarget(v); }}
              style={{ width: 180 }}
              placeholder="全部"
              allowClear
              showSearch
              filterOption={(input, option) =>
                (option?.label as string || '').toLowerCase().includes(input.toLowerCase())
              }
            >
              <Select.Option value="">全部</Select.Option>
              {targetAgents.map((a) => (
                <Select.Option key={a} value={a}>{agentDisplay(a)}</Select.Option>
              ))}
            </Select>
          </Space>
          <Space size={4}>
            <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>Scope:</span>
            <Select
              value={filterScope}
              onChange={(v) => { setFilterScope(v); }}
              style={{ width: 160 }}
              placeholder="全部"
              allowClear
            >
              <Select.Option value="">全部</Select.Option>
              {allScopes.map((s) => (
                <Select.Option key={s} value={s}>{s}</Select.Option>
              ))}
            </Select>
          </Space>
          {(filterSource || filterTarget || filterScope) && (
            <Button
              size="small"
              icon={<CloseOutlined />}
              onClick={() => { setFilterSource(''); setFilterTarget(''); setFilterScope(''); }}
            >
              清除筛选
            </Button>
          )}
        </Space>
      </Card>

      {/* Table */}
      <Spin spinning={loading}>
        {delegations.length === 0 && !loading ? (
          <Card style={{ borderRadius: 10, textAlign: 'center', padding: 40 }}>
            <Empty description="暂无授权策略 — 点击「创建授权」来添加第一条策略" />
          </Card>
        ) : (
          <Card bodyStyle={{ padding: 0 }} style={{ borderRadius: 10 }}>
            <Table
              dataSource={delegations}
              columns={columns}
              rowKey="id"
              pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 条` }}
              size="middle"
            />
          </Card>
        )}
      </Spin>

      {/* Create Modal */}
      <Modal
        title="创建授权策略"
        open={createOpen}
        onCancel={() => { setCreateOpen(false); form.resetFields(); }}
        onOk={() => form.submit()}
        okText="创建"
        cancelText="取消"
        width={560}
      >
        <Form form={form} layout="vertical" onFinish={handleCreate}>
          <Form.Item
            name="source_agent_id"
            label="源 Agent（授权方）"
            rules={[{ required: true, message: '请选择源 Agent' }]}
          >
            <Select
              showSearch
              placeholder="选择授权方 Agent"
              options={agentOptions}
              filterOption={(input, option) =>
                (option?.label as string || '').toLowerCase().includes(input.toLowerCase())
              }
            />
          </Form.Item>

          <Form.Item
            name="target_agent_id"
            label="目标 Agent（被授权方）"
            rules={[{ required: true, message: '请选择目标 Agent' }]}
          >
            <Select
              showSearch
              placeholder="选择被授权方 Agent"
              options={agentOptions}
              filterOption={(input, option) =>
                (option?.label as string || '').toLowerCase().includes(input.toLowerCase())
              }
            />
          </Form.Item>

          <Form.Item
            name="allowed_actions"
            label="授权范围 (Scopes)"
            rules={[{ required: true, message: '请至少选择一个 Scope' }]}
          >
            <Select
              mode="multiple"
              placeholder="选择授权的操作范围"
              options={AVAILABLE_SCOPES.map((s) => ({ label: s, value: s }))}
            />
          </Form.Item>

          <Form.Item
            name="max_depth"
            label="最大委托深度"
            initialValue={3}
            rules={[{ required: true, message: '请输入最大深度' }]}
          >
            <InputNumber min={1} max={10} style={{ width: '100%' }} placeholder="委托链最大深度（默认 3）" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default AuthorizationMatrix;