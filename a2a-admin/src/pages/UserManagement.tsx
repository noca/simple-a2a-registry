import React, { useEffect, useState, useCallback } from 'react';
import {
  Card, Table, Button, Modal, Form, Input, Select, Space, Tag, Spin, message, Empty, Popconfirm,
} from 'antd';
import { PlusOutlined, ReloadOutlined, DeleteOutlined, EditOutlined, UserOutlined } from '@ant-design/icons';
import { listUsers, createUser, updateUser, deleteUser } from '../api/client';
import PageTitle from '../components/PageTitle';

interface User {
  username: string;
  role: string;
  display_name?: string;
  disabled?: boolean;
  created_at?: number;
  last_login?: number;
}

const UserManagement: React.FC = () => {
  const [users, setUsers] = useState<User[]>([]);
  const [loading, setLoading] = useState(true);
  const [createOpen, setCreateOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [selectedUser, setSelectedUser] = useState<User | null>(null);
  const [form] = Form.useForm();
  const [editForm] = Form.useForm();

  const fetchUsers = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listUsers();
      setUsers(Array.isArray(data) ? data : data.users || []);
    } catch {
      setUsers([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchUsers(); }, [fetchUsers]);

  const handleCreate = async (values: any) => {
    try {
      await createUser(values);
      message.success('User created');
      setCreateOpen(false);
      form.resetFields();
      fetchUsers();
    } catch (err: any) {
      message.error(err?.response?.data?.detail || 'Failed to create user');
    }
  };

  const handleEdit = async (values: any) => {
    if (!selectedUser) return;
    try {
      const payload: Record<string, any> = {};
      if (values.display_name) payload.display_name = values.display_name;
      if (values.role) payload.role = values.role;
      if (values.password) payload.password = values.password;
      await updateUser(selectedUser.username, payload);
      message.success('User updated');
      setEditOpen(false);
      setSelectedUser(null);
      editForm.resetFields();
      fetchUsers();
    } catch (err: any) {
      message.error(err?.response?.data?.detail || 'Failed to update user');
    }
  };

  const handleDelete = async (username: string) => {
    try {
      await deleteUser(username);
      message.success('User deleted');
      fetchUsers();
    } catch (err: any) {
      message.error(err?.response?.data?.detail || 'Failed to delete user');
    }
  };

  const openEdit = (user: User) => {
    setSelectedUser(user);
    editForm.setFieldsValue({
      display_name: user.display_name || user.username,
      role: user.role,
      password: '',
    });
    setEditOpen(true);
  };

  const columns = [
    {
      title: 'Username', dataIndex: 'username', key: 'username',
      render: (u: string) => (
        <Space>
          <UserOutlined style={{ color: 'var(--text-secondary)' }} />
          <span style={{ fontWeight: 500 }}>{u}</span>
        </Space>
      ),
    },
    { title: 'Display Name', dataIndex: 'display_name', key: 'display_name', render: (d: string) => d || '-' },
    {
      title: 'Role', dataIndex: 'role', key: 'role',
      render: (r: string) => (
        <Tag color={r === 'admin' ? 'blue' : 'default'} style={{ borderRadius: 4 }}>
          {r || 'user'}
        </Tag>
      ),
    },
    { title: 'Status', dataIndex: 'disabled', key: 'disabled',
      render: (d: boolean) => d
        ? <Tag color="red" style={{ borderRadius: 4 }}>Disabled</Tag>
        : <Tag color="green" style={{ borderRadius: 4 }}>Active</Tag>,
    },
    {
      title: 'Last Login', dataIndex: 'last_login', key: 'last_login',
      render: (t: number) => t ? new Date(t * 1000).toLocaleString() : '-',
    },
    {
      title: 'Actions', key: 'actions',
      render: (_: any, r: User) => (
        <Space>
          <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(r)} />
          <Popconfirm
            title="Delete this user?"
            onConfirm={() => handleDelete(r.username)}
            okText="Delete"
            okType="danger"
          >
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <PageTitle
        title="User Management"
        count={users.length}
        label="users"
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={fetchUsers}>Refresh</Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
              Create User
            </Button>
          </Space>
        }
      />

      <Spin spinning={loading}>
        {users.length === 0 && !loading ? (
          <Card style={{ borderRadius: 10, textAlign: 'center', padding: 40 }}>
            <Empty description="No users found" />
          </Card>
        ) : (
          <Card bodyStyle={{ padding: 0 }} style={{ borderRadius: 10 }}>
            <Table
              dataSource={users}
              columns={columns}
              rowKey="username"
              pagination={{ pageSize: 20 }}
              size="middle"
            />
          </Card>
        )}
      </Spin>

      {/* Create Modal */}
      <Modal
        title="Create User"
        open={createOpen}
        onCancel={() => { setCreateOpen(false); form.resetFields(); }}
        onOk={() => form.submit()}
        okText="Create"
      >
        <Form form={form} layout="vertical" onFinish={handleCreate}>
          <Form.Item name="username" label="Username" rules={[{ required: true }]}>
            <Input placeholder="username" />
          </Form.Item>
          <Form.Item name="password" label="Password" rules={[{ required: true }]}>
            <Input.Password placeholder="••••••••" />
          </Form.Item>
          <Form.Item name="display_name" label="Display Name">
            <Input placeholder="Display name" />
          </Form.Item>
          <Form.Item name="role" label="Role" initialValue="user">
            <Select>
              <Select.Option value="admin">Admin</Select.Option>
              <Select.Option value="user">User</Select.Option>
            </Select>
          </Form.Item>
        </Form>
      </Modal>

      {/* Edit Modal */}
      <Modal
        title={`Edit User: ${selectedUser?.username}`}
        open={editOpen}
        onCancel={() => { setEditOpen(false); setSelectedUser(null); editForm.resetFields(); }}
        onOk={() => editForm.submit()}
        okText="Save"
      >
        <Form form={editForm} layout="vertical" onFinish={handleEdit}>
          <Form.Item name="display_name" label="Display Name">
            <Input />
          </Form.Item>
          <Form.Item name="role" label="Role">
            <Select>
              <Select.Option value="admin">Admin</Select.Option>
              <Select.Option value="user">User</Select.Option>
            </Select>
          </Form.Item>
          <Form.Item name="password" label="New Password (leave blank to keep)">
            <Input.Password placeholder="Leave blank to keep current" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default UserManagement;