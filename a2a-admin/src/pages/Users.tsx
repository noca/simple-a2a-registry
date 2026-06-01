import React, { useEffect, useState } from 'react';
import { adminAPI } from '../api/client';
import { useStore } from '../store/useStore';

const Users: React.FC = () => {
  const { addToast } = useStore();
  const [users, setUsers] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [editingUser, setEditingUser] = useState<string | null>(null);
  const [form, setForm] = useState({ username: '', password: '', role: 'user' });

  useEffect(() => { fetchUsers(); }, []);

  const fetchUsers = async () => {
    try {
      const data = await adminAPI.listUsers();
      setUsers(data.users || data || []);
    } catch (e: any) {
      addToast('error', `加载用户失败: ${e.message}`);
    } finally {
      setLoading(false);
    }
  };

  const handleSubmit = async () => {
    if (!form.username.trim()) { addToast('error', '用户名不能为空'); return; }
    try {
      if (editingUser) {
        await adminAPI.updateUser(editingUser, form);
        addToast('success', `用户 "${form.username}" 已更新`);
      } else {
        await adminAPI.createUser(form);
        addToast('success', `用户 "${form.username}" 创建成功`);
      }
      setShowCreate(false);
      setEditingUser(null);
      setForm({ username: '', password: '', role: 'user' });
      fetchUsers();
    } catch (e: any) {
      addToast('error', `操作失败: ${e.response?.data?.detail || e.message}`);
    }
  };

  const handleEdit = (u: any) => {
    setEditingUser(u.username);
    setForm({ username: u.username, password: '', role: u.role || 'user' });
    setShowCreate(true);
  };

  const handleDelete = async (username: string) => {
    if (!confirm(`确定删除用户 "${username}"?`)) return;
    try {
      await adminAPI.deleteUser(username);
      addToast('success', `用户 "${username}" 已删除`);
      fetchUsers();
    } catch (e: any) {
      addToast('error', `删除失败: ${e.message}`);
    }
  };

  return (
    <div>
      <h1 className="page-title">👥 User Management</h1>

      <div className="toolbar">
        <span className="spacer" />
        <button className="btn" onClick={fetchUsers} style={btnStyle}>⟳ Refresh</button>
        <button onClick={() => { setEditingUser(null); setForm({ username: '', password: '', role: 'user' }); setShowCreate(true); }}
          style={{ ...btnStyle, ...btnPrimaryStyle }}>+ New User</button>
      </div>

      {loading ? (
        <div className="macos-card">
          {[1, 2, 3].map((i) => (
            <div key={i} className="loading-skeleton" style={{ height: 20, marginBottom: 12 }} />
          ))}
        </div>
      ) : (
        <div className="macos-card" style={{ padding: 0 }}>
          <table className="macos-table">
            <thead>
              <tr>
                <th>Username</th>
                <th>Role</th>
                <th>Created</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id || u.username}>
                  <td style={{ fontWeight: 500 }}>{u.username}</td>
                  <td>
                    <span className="macos-tag">{u.role || 'user'}</span>
                  </td>
                  <td style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
                    {u.created_at ? new Date(u.created_at).toLocaleDateString() : '—'}
                  </td>
                  <td>
                    <button onClick={() => handleEdit(u)}
                      style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--accent)', fontSize: 12, marginRight: 8 }}>编辑</button>
                    <button onClick={() => handleDelete(u.username)}
                      style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--red)', fontSize: 12 }}>删除</button>
                  </td>
                </tr>
              ))}
              {users.length === 0 && (
                <tr>
                  <td colSpan={4} style={{ textAlign: 'center', padding: 40, color: 'var(--text-secondary)' }}>
                    暂无用户
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* Create/Edit Modal */}
      {showCreate && (
        <div style={{
          position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.3)', backdropFilter: 'blur(4px)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 200,
        }}>
          <div style={{ background: 'var(--bg-card)', borderRadius: 14, padding: 24, width: 400, boxShadow: 'var(--shadow-lg)' }}>
            <h3 style={{ fontSize: 16, fontWeight: 600, marginBottom: 16 }}>
              {editingUser ? '编辑用户' : '创建用户'}
            </h3>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <input placeholder="Username *" value={form.username}
                onChange={(e) => setForm({ ...form, username: e.target.value })}
                disabled={!!editingUser}
                style={{
                  padding: '10px 14px', borderRadius: 8, border: '1px solid var(--border)',
                  background: editingUser ? 'rgba(0,0,0,0.03)' : 'var(--bg)',
                  fontSize: 13, outline: 'none',
                }} />
              {!editingUser && (
                <input placeholder="Password *" type="password" value={form.password}
                  onChange={(e) => setForm({ ...form, password: e.target.value })}
                  style={{ padding: '10px 14px', borderRadius: 8, border: '1px solid var(--border)', background: 'var(--bg)', fontSize: 13, outline: 'none' }} />
              )}
              <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}
                style={{ padding: '10px 14px', borderRadius: 8, border: '1px solid var(--border)', background: 'var(--bg)', fontSize: 13, outline: 'none' }}>
                <option value="user">user</option>
                <option value="admin">admin</option>
              </select>
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 16 }}>
              <button onClick={() => { setShowCreate(false); setEditingUser(null); }}
                style={{ ...btnStyle, background: 'transparent', border: '1px solid var(--border)' }}>Cancel</button>
              <button onClick={handleSubmit} style={{ ...btnStyle, ...btnPrimaryStyle }}>
                {editingUser ? 'Update' : 'Create'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

const btnStyle: React.CSSProperties = {
  padding: '8px 16px', borderRadius: 8, border: 'none', cursor: 'pointer', fontSize: 12, fontWeight: 500,
};
const btnPrimaryStyle: React.CSSProperties = {
  background: 'linear-gradient(135deg, #007AFF, #AF52DE)', color: 'white',
};

export default Users;