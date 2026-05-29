import React, { useEffect, useState, useCallback } from 'react';
import { Card, Row, Col, Input, Button, Spin, Modal, Form, message, Typography, Select, Tag, Drawer, Descriptions } from 'antd';
import { PlusOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons';
import { listV2Tasks, createV2Task } from '../api/client';

const { Text } = Typography;
const STATUSES = ['todo', 'ready', 'running', 'completed', 'blocked', 'failed', 'cancelled'];
const HC: Record<string, string> = {
  todo: '#C7C7CC', ready: '#007AFF', running: '#BF5AF2',
  completed: '#30D158', blocked: '#FF9F0A', failed: '#FF453A', cancelled: '#C7C7CC',
};

const Kanban: React.FC = () => {
  const [ts, setTs] = useState<any[]>([]);
  const [lo, setLo] = useState(true);
  const [sr, setSr] = useState('');
  const [co, setCo] = useState(false);
  const [dd, setDd] = useState(false);
  const [st, setSt] = useState<any>(null);
  const [f] = Form.useForm();

  const fetch = useCallback(async () => {
    setLo(true);
    try {
      const d = await listV2Tasks({ limit: 200, ...(sr ? { q: sr } : {}) });
      setTs(d.tasks || []);
    } catch (e) { console.warn('[Kanban] fetch failed', e); } finally { setLo(false); }
  }, [sr]);

  useEffect(() => { fetch(); }, [fetch]);

  const create = async (v: any) => {
    try {
      await createV2Task(v);
      message.success('Created');
      setCo(false);
      f.resetFields();
      fetch();
    } catch (e: any) {
      message.error(e?.response?.data?.detail || 'Failed');
    }
  };

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
        <Typography.Title level={3} style={{ margin: 0, fontWeight: 600, fontSize: 22 }}>
          Kanban V2
          <span style={{ fontSize: 13, fontWeight: 400, color: 'var(--text-secondary)', marginLeft: 12 }}>
            {ts.length} tasks
          </span>
        </Typography.Title>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCo(true)}>
          Create Task
        </Button>
      </div>

      <Card bodyStyle={{ padding: '12px 16px' }} style={{ marginBottom: 16, borderRadius: 10 }}>
        <Row gutter={[12, 12]} align="middle">
          <Col xs={18} sm={12} md={8}>
            <Input
              prefix={<SearchOutlined style={{ color: 'var(--text-tertiary)' }} />}
              placeholder="Search cards..."
              value={sr}
              onChange={(e: any) => setSr(e.target.value)}
              allowClear
            />
          </Col>
          <Col>
            <Button icon={<ReloadOutlined />} onClick={fetch}>Refresh</Button>
          </Col>
        </Row>
      </Card>

      <Spin spinning={lo}>
        <div style={{ display: 'flex', gap: 12, overflowX: 'auto', paddingBottom: 12, minHeight: 400 }}>
          {STATUSES.map(st => {
            const cards = ts.filter(t => t.status === st);
            return (
              <div key={st} style={{ minWidth: 260, maxWidth: 260, flexShrink: 0 }}>
                <div style={{
                  padding: '8px 12px', borderRadius: '6px 6px 0 0',
                  background: 'var(--bg-card)', borderBottom: `3px solid ${HC[st] || '#C7C7CC'}`,
                  display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                  marginBottom: 8, boxShadow: 'var(--shadow-sm)',
                }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    <span style={{ width: 8, height: 8, borderRadius: '50%', background: HC[st] || '#86868B' }} />
                    <span style={{ fontWeight: 600, fontSize: 12, textTransform: 'uppercase', letterSpacing: 0.5, color: 'var(--text-secondary)' }}>
                      {st}
                    </span>
                  </div>
                  <span style={{ fontSize: 12, fontWeight: 500, color: 'var(--text-secondary)' }}>{cards.length}</span>
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {cards.length === 0 ? (
                    <div style={{ padding: 16, textAlign: 'center' }}>
                      <Text type="secondary" style={{ fontSize: 12 }}>Empty</Text>
                    </div>
                  ) : (
                    cards.map(t => (
                      <Card key={t.id} size="small" bodyStyle={{ padding: 12 }}
                        style={{ borderRadius: 8, boxShadow: 'var(--shadow-sm)' }} hoverable
                        onClick={() => { setSt(t); setDd(true); }}>
                        <div style={{ fontWeight: 500, fontSize: 13, marginBottom: 6 }}>{t.title}</div>
                        {t.body && (
                          <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 6, lineHeight: 1.4 }}>
                            {t.body.substring(0, 60)}{t.body.length > 60 ? '...' : ''}
                          </div>
                        )}
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                          {t.assignee ? <Tag style={{ fontSize: 10, lineHeight: '20px', borderRadius: 4 }}>{t.assignee}</Tag> : <span />}
                          <div style={{ fontSize: 10, color: 'var(--text-tertiary)' }}>
                            {t.priority > 0 && (
                              <span style={{ color: t.priority > 1 ? 'var(--red)' : 'var(--orange)', marginRight: 6 }}>
                                P{t.priority}
                              </span>
                            )}
                            {t.id.substring(0, 8)}
                          </div>
                        </div>
                      </Card>
                    ))
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </Spin>

      <Drawer title={st?.title||'Task Detail'} placement="right" width={480}
        onClose={()=>{setDd(false);setSt(null);}} open={dd}>
        {st && <Descriptions column={1} size="small" bordered>
          <Descriptions.Item label="ID">{st.id}</Descriptions.Item>
          <Descriptions.Item label="Status"><Tag color={HC[st.status]||'#C7C7CC'}>{st.status}</Tag></Descriptions.Item>
          <Descriptions.Item label="Assignee">{st.assignee||'-'}</Descriptions.Item>
          <Descriptions.Item label="Priority">P{st.priority??0}</Descriptions.Item>
          <Descriptions.Item label="Created">{st.created_at?new Date(st.created_at*1000).toLocaleString():'-'}</Descriptions.Item>
          <Descriptions.Item label="Body">{st.body||'-'}</Descriptions.Item>
        </Descriptions>}
      </Drawer>

      <Modal
        title="Create Task" open={co}
        onCancel={() => { setCo(false); f.resetFields(); }}
        onOk={() => f.submit()} okText="Create" width={500}
      >
        <Form form={f} layout="vertical" onFinish={create}>
          <Form.Item name="title" label="Title" rules={[{ required: true }]}>
            <Input placeholder="Task title" />
          </Form.Item>
          <Form.Item name="body" label="Description">
            <Input.TextArea rows={4} placeholder="Markdown description" />
          </Form.Item>
          <Form.Item name="assignee" label="Assignee">
            <Input placeholder="worker profile" />
          </Form.Item>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item name="priority" label="Priority">
                <Select>
                  <Select.Option value={0}>Normal</Select.Option>
                  <Select.Option value={1}>High</Select.Option>
                  <Select.Option value={2}>Urgent</Select.Option>
                </Select>
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Modal>
    </div>
  );
};

export default Kanban;
