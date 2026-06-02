import React, { useEffect, useState, useCallback } from 'react';
import {
  Card, Table, Button, Tag, Row, Col, Select, Spin, Empty, Typography,
} from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { listV1Tasks } from '../api/client';
import StatusTag from '../components/StatusTag';
import PageTitle from '../components/PageTitle';
import TaskDetailPanel from '../components/TaskDetailPanel';

const { Text } = Typography;

interface TaskItem {
  id: string;
  agent_id: string;
  query: string;
  state: string;
  result?: any;
  error?: string;
  session_id?: string;
  created_at?: number;
  updated_at?: number;
  tenant?: string;
}

const Tasks: React.FC = () => {
  const [tasks, setTasks] = useState<TaskItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [stateFilter, setStateFilter] = useState('');

  const [selectedTask, setSelectedTask] = useState<TaskItem | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);

  const fetchTasks = useCallback(async () => {
    setLoading(true);
    try {
      const params: Record<string, any> = {};
      if (stateFilter) params.state = stateFilter;
      const data = await listV1Tasks(params);
      setTasks(data.tasks || []);
    } catch {
      setTasks([]);
    } finally {
      setLoading(false);
    }
  }, [stateFilter]);

  useEffect(() => { fetchTasks(); }, [fetchTasks]);

  const openDetail = async (task: TaskItem) => {
    setSelectedTask(task);
    setDrawerOpen(true);
    setDetailLoading(false);
  };

  const closeDrawer = () => {
    setDrawerOpen(false);
    setSelectedTask(null);
  };

  const formatTime = (ts?: number) =>
    ts ? new Date(ts * 1000).toLocaleString() : '-';

  const columns = [
    {
      title: 'ID', dataIndex: 'id', key: 'id',
      render: (id: string) => <code style={{ fontSize: 11 }}>{id?.substring(0, 12)}…</code>,
    },
    {
      title: 'Agent', dataIndex: 'agent_id', key: 'agent_id',
      render: (a: string) => <span style={{ fontWeight: 500 }}>{a}</span>,
    },
    {
      title: 'Query', dataIndex: 'query', key: 'query',
      render: (q: string) => (
        <div style={{ maxWidth: 300, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {q?.substring(0, 60)}{q?.length > 60 ? '…' : ''}
        </div>
      ),
    },
    {
      title: 'State', dataIndex: 'state', key: 'state',
      render: (s: string) => <StatusTag status={s} />,
    },
    {
      title: 'Created', dataIndex: 'created_at', key: 'created_at',
      render: (t: number) => formatTime(t),
    },
    {
      title: 'Result', key: 'result',
      render: (_: any, r: TaskItem) => {
        if (r.error) return <Tag color="red" style={{ borderRadius: 4 }}>Error</Tag>;
        if (r.result) return <Tag color="green" style={{ borderRadius: 4 }}>Completed</Tag>;
        return '-';
      },
    },
  ];

  return (
    <div>
      <PageTitle
        title="Tasks"
        count={tasks.length}
        label="total"
        extra={<Button icon={<ReloadOutlined />} onClick={fetchTasks}>Refresh</Button>}
      />

      <Card bodyStyle={{ padding: '12px 16px' }} style={{ marginBottom: 16, borderRadius: 10 }}>
        <Row gutter={12} align="middle">
          <Col>
            <span style={{ fontSize: 12, color: 'var(--text-secondary)', marginRight: 8 }}>State:</span>
            <Select value={stateFilter} onChange={setStateFilter} style={{ width: 150 }} placeholder="All States">
              <Select.Option value="">All States</Select.Option>
              <Select.Option value="dispatched">Dispatched</Select.Option>
              <Select.Option value="forwarded">Forwarded</Select.Option>
              <Select.Option value="working">Working</Select.Option>
              <Select.Option value="completed">Completed</Select.Option>
              <Select.Option value="failed">Failed</Select.Option>
            </Select>
          </Col>
        </Row>
      </Card>

      <Spin spinning={loading}>
        {tasks.length === 0 && !loading ? (
          <Card style={{ borderRadius: 10, textAlign: 'center', padding: 40 }}>
            <Empty description="No tasks found" />
          </Card>
        ) : (
          <Card bodyStyle={{ padding: 0 }} style={{ borderRadius: 10 }}>
            <Table
              dataSource={tasks}
              columns={columns}
              rowKey="id"
              pagination={{ pageSize: 20, showTotal: (t) => `${t} tasks` }}
              size="middle"
              onRow={(record) => ({
                onClick: () => openDetail(record),
                style: { cursor: 'pointer' },
              })}
            />
          </Card>
        )}
      </Spin>

      {/* Use the new TaskDetailPanel component with progress bar, timeline, metrics */}
      <TaskDetailPanel
        task={selectedTask ? {
          id: selectedTask.id,
          title: selectedTask.query || selectedTask.id,
          status: selectedTask.state,
          assignee: selectedTask.agent_id,
          created_at: selectedTask.created_at,
          updated_at: selectedTask.updated_at,
          result: selectedTask.result,
          [Symbol('extra')]: selectedTask,
        } : null}
        open={drawerOpen}
        onClose={closeDrawer}
        fetchDetail={true}
      />
    </div>
  );
};

export default Tasks;