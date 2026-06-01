import React, { useEffect, useState, useCallback } from 'react';
import {
  Card, Table, Button, Modal, Form, Input, Space, Tag, Typography, Spin, message, Empty,
} from 'antd';
import { PlusOutlined, ReloadOutlined, DeleteOutlined, KeyOutlined } from '@ant-design/icons';
import { listClients, createClient, deleteClient } from '../api/client';
import PageTitle from '../components/PageTitle';

const { Text } = Typography;

interface Client {
  client_id: string;
  client_secret?: string;
  agent_card_id?: string;
  description?: string;
  scopes?: string[];
  allowed_scopes?: string[];
  tenant?: string;
  disabled?: boolean;
  created_at?: number;
  token_count?: number;
}

const Clients: React.FC = () => {
  const [clients, setClients] = useState<Client[]>([]);
  const [loading, setLoading] = useState(true);
  const [createOpen, setCreateOpen] = useState(false);
  const [createdSecret, setCreatedSecret] = useState<string | null>(null);
  const [form] = Form.useForm();

  const fetchClients = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listClients();
      setClients(Array.isArray(data) ? data : []);
    } catch {
      setClients([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchClients(); }, [fetchClients]);

  const handleCreate = async (values: any) => {
    try {
      const scopes = values.allowed_scopes ? values.allowed_scopes.split(',').map((s: string) => s.trim()).filter(Boolean) : undefined;
      const data = await createClient({
        agent_card_id: values.agent_card_id || '',
        description: values.description || '',
        allowed_scopes: scopes,
      });
      setCreatedSecret(data.client_secret || '');
      message.success('Client created');
      fetchClients();
    } catch (err: any) {
      message.error(err?.response?.data?.detail || 'Failed to create client');
    }
  };

  const handleDelete = (client: Client) => {
    Modal.confirm({
      title: 'Delete Client',
      content: `Delete client "${client.client_id}"? This action cannot be undone.`,
      okText: 'Delete',
      okType: 'danger',
      onOk: async () => {
        try {
          await deleteClient(client.client_id);
          message.success('Client deleted');
          fetchClients();
        } catch (err: any) {
          message.error(err?.response?.data?.detail || 'Failed to delete client');
        }
      },
    });
  };

  const columns = [
    {
      title: 'Client ID', dataIndex: 'client_id', key: 'client_id',
      render: (id: string) => <code style={{ fontSize: 11 }}>{id}</code>,
    },
    {
      title: 'Agent Card', dataIndex: 'agent_card_id', key: 'agent_card_id',
      render: (a: string) => a || <Text type="secondary">-</Text>,
    },
    {
      title: 'Scopes', key: 'scopes',
      render: (_: any, r: Client) => {
        const scopes = r.scopes || r.allowed_scopes || [];
        return (
          <Space size={4} wrap>
            {scopes.map((s: string) => (
              <Tag key={s} style={{ borderRadius: 4, fontSize: 10, lineHeight: '20px' }}>{s}</Tag>
            ))}
          </Space>
        );
      },
    },
    {
      title: 'Tokens', dataIndex: 'token_count', key: 'token_count',
      render: (c: number) => c ?? 0,
    },
    { title: 'Tenant', dataIndex: 'tenant', key: 'tenant', render: (t: string) => t || '-' },
    {
      title: 'Created', dataIndex: 'created_at', key: 'created_at',
      render: (t: number) => t ? new Date(t * 1000).toLocaleString() : '-',
    },
    {
      title: 'Actions', key: 'actions',
      render: (_: any, r: Client) => (
        <Button size="small" danger icon={<DeleteOutlined />} onClick={() => handleDelete(r)} />
      ),
    },
  ];

  return (
    <div>
      <PageTitle
        title="OAuth Clients"
        count={clients.length}
        label="clients"
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={fetchClients}>Refresh</Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => { setCreateOpen(true); setCreatedSecret(null); }}>
              Create Client
            </Button>
          </Space>
        }
      />

      <Spin spinning={loading}>
        {clients.length === 0 && !loading ? (
          <Card style={{ borderRadius: 10, textAlign: 'center', padding: 40 }}>
            <Empty description="No clients found" />
          </Card>
        ) : (
          <Card bodyStyle={{ padding: 0 }} style={{ borderRadius: 10 }}>
            <Table
              dataSource={clients}
              columns={columns}
              rowKey="client_id"
              pagination={{ pageSize: 20 }}
              size="middle"
            />
          </Card>
        )}
      </Spin>

      <Modal
        title="Create OAuth Client"
        open={createOpen && !createdSecret}
        onCancel={() => { setCreateOpen(false); form.resetFields(); }}
        onOk={() => form.submit()}
        okText="Create"
      >
        <Form form={form} layout="vertical" onFinish={handleCreate}>
          <Form.Item name="agent_card_id" label="Agent Card ID">
            <Input placeholder="Optional: link to agent" />
          </Form.Item>
          <Form.Item name="description" label="Description">
            <Input placeholder="My A2A Agent Client" />
          </Form.Item>
          <Form.Item name="allowed_scopes" label="Scopes (comma-separated)">
            <Input placeholder="task:read, task:write, agent:read" />
          </Form.Item>
        </Form>
      </Modal>

      {/* Show secret modal */}
      <Modal
        title="Client Created"
        open={!!createdSecret}
        onCancel={() => { setCreatedSecret(null); setCreateOpen(false); form.resetFields(); }}
        footer={<Button type="primary" onClick={() => { setCreatedSecret(null); setCreateOpen(false); form.resetFields(); }}>Done</Button>}
      >
        <div style={{ textAlign: 'center', padding: 20 }}>
          <KeyOutlined style={{ fontSize: 48, color: 'var(--orange)', marginBottom: 16 }} />
          <div style={{ fontSize: 14, marginBottom: 8 }}>Client Secret (copy now — won't be shown again):</div>
          <Input.TextArea
            value={createdSecret || ''}
            readOnly
            rows={3}
            style={{ fontSize: 11, fontFamily: 'monospace', textAlign: 'center' }}
          />
        </div>
      </Modal>
    </div>
  );
};

export default Clients;