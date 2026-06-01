import React, { useEffect, useState, useCallback } from 'react';
import { agentAPI } from '../api/client';
import { useStore } from '../store/useStore';
import StatusTag from '../components/StatusTag';

interface Agent {
  id: string;
  name: string;
  description?: string;
  url?: string;
  status?: string;
  connection?: string;
  skills?: Array<{ id?: string; name?: string; description?: string }>;
  tags?: string[];
  metadata?: Record<string, string>;
}

const Agents: React.FC = () => {
  const { addToast } = useStore();
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [viewMode, setViewMode] = useState<'grid' | 'table'>('grid');
  const [detailAgent, setDetailAgent] = useState<Agent | null>(null);
  const [showRegister, setShowRegister] = useState(false);
  const [regForm, setRegForm] = useState({ name: '', url: '', description: '' });

  const fetchAgents = useCallback(async () => {
    try {
      const params: Record<string, string> = {};
      if (search) params.q = search;
      if (statusFilter) params.status = statusFilter;
      const data = await agentAPI.list(params);
      setAgents(data.agents || []);
    } catch (e: any) {
      addToast('error', `加载 Agent 失败: ${e?.message || '未知错误'}`);
    } finally {
      setLoading(false);
    }
  }, [search, statusFilter]);

  useEffect(() => { fetchAgents(); }, [fetchAgents]);

  const handleRegister = async () => {
    if (!regForm.name.trim()) { addToast('error', 'Agent 名称不能为空'); return; }
    try {
      await agentAPI.register({ name: regForm.name, url: regForm.url, description: regForm.description });
      addToast('success', `Agent "${regForm.name}" 注册成功`);
      setShowRegister(false);
      setRegForm({ name: '', url: '', description: '' });
      fetchAgents();
    } catch (e: any) {
      addToast('error', `注册失败: ${e?.response?.data?.detail || e.message}`);
    }
  };

  const handleDelete = async (id: string, name: string) => {
    if (!confirm(`确定删除 Agent "${name}"?`)) return;
    try {
      await agentAPI.remove(id);
      addToast('success', `Agent "${name}" 已删除`);
      fetchAgents();
    } catch (e: any) {
      addToast('error', `删除失败: ${e.message}`);
    }
  };

  const handleToggle = async (id: string) => {
    try {
      await agentAPI.toggle(id);
      fetchAgents();
    } catch (e: any) {
      addToast('error', `操作失败: ${e.message}`);
    }
  };

  return (
    <div>
      <h1 className="page-title">🤖 Agents</h1>

      <div className="toolbar">
        <input
          type="text" placeholder="Search agents by name, URL, or ID..."
          value={search}
          onChange={(e) => { setSearch(e.target.value); setLoading(true); }}
          style={{ padding: '8px 12px', borderRadius: 8, border: '1px solid var(--border)', background: 'var(--bg)', fontSize: 13, width: 280, outline: 'none' }}
        />
        <select value={statusFilter} onChange={(e) => { setStatusFilter(e.target.value); setLoading(true); }}
          style={{ padding: '8px 12px', borderRadius: 8, border: '1px solid var(--border)', background: 'var(--bg)', fontSize: 13, outline: 'none' }}>
          <option value="">All Status</option>
          <option value="alive">Online</option>
          <option value="stale">Stale</option>
          <option value="disabled">Offline</option>
        </select>
        <button className="btn" onClick={fetchAgents} style={btnStyle}>⟳ Refresh</button>
        <span className="spacer" />
        <div className="view-toggle" style={{ display: 'flex', borderRadius: 6, overflow: 'hidden', border: '1px solid var(--border)' }}>
          <button onClick={() => setViewMode('grid')} style={{
            padding: '6px 12px', border: 'none', background: viewMode === 'grid' ? 'var(--accent)' : 'var(--bg-card)',
            color: viewMode === 'grid' ? 'white' : 'var(--text)', cursor: 'pointer', fontSize: 12,
          }}>⊞ Grid</button>
          <button onClick={() => setViewMode('table')} style={{
            padding: '6px 12px', border: 'none', background: viewMode === 'table' ? 'var(--accent)' : 'var(--bg-card)',
            color: viewMode === 'table' ? 'white' : 'var(--text)', cursor: 'pointer', fontSize: 12,
          }}>☰ Table</button>
        </div>
        <button onClick={() => setShowRegister(true)} style={{ ...btnStyle, ...btnPrimaryStyle }}>+ Register Agent</button>
      </div>

      {loading ? (
        <div className="agent-grid">
          {[1, 2, 3, 4].map((i) => (
            <div key={i} className="agent-card">
              <div className="loading-skeleton" style={{ height: 16, width: 140, marginBottom: 8 }} />
              <div className="loading-skeleton" style={{ height: 12, width: 200, marginBottom: 8 }} />
              <div className="loading-skeleton" style={{ height: 12, width: 100 }} />
            </div>
          ))}
        </div>
      ) : viewMode === 'grid' ? (
        <div className="agent-grid">
          {agents.map((a) => (
            <div key={a.id} className="agent-card" onClick={() => setDetailAgent(a)}>
              <div className="agent-actions">
                <button title="Toggle status" onClick={(e) => { e.stopPropagation(); handleToggle(a.id); }}
                  style={{ background: 'none', border: 'none', cursor: 'pointer', fontSize: 14 }}>⏯</button>
                <button title="Delete" onClick={(e) => { e.stopPropagation(); handleDelete(a.id, a.name); }}
                  style={{ background: 'none', border: 'none', cursor: 'pointer', fontSize: 14 }}>🗑</button>
              </div>
              <h4>{a.name}</h4>
              <div className="agent-id">{a.id}</div>
              <div style={{ marginBottom: 8 }}><StatusTag status={a.status || 'unknown'} /></div>
              <div className="agent-tags">
                {(a.skills || []).slice(0, 4).map((s, i) => (
                  <span key={i} className="macos-tag">{s.name || s.id || 'skill'}</span>
                ))}
              </div>
              <div className="agent-meta">
                {a.url && <div>URL: {a.url}</div>}
                <div>Conn: {a.connection || 'http'}</div>
              </div>
            </div>
          ))}
          {agents.length === 0 && (
            <div className="macos-card" style={{ textAlign: 'center', padding: 40, color: 'var(--text-secondary)', gridColumn: '1 / -1' }}>
              暂无 Agent 数据。
            </div>
          )}
        </div>
      ) : (
        <table className="macos-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>ID</th>
              <th>Status</th>
              <th>Connection</th>
              <th>Skills</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {agents.map((a) => (
              <tr key={a.id}>
                <td style={{ fontWeight: 500 }}>{a.name}</td>
                <td style={{ fontFamily: "'SF Mono','Menlo',monospace", fontSize: 11 }}>{a.id}</td>
                <td><StatusTag status={a.status || 'unknown'} /></td>
                <td style={{ color: 'var(--text-secondary)' }}>{a.connection || 'http'}</td>
                <td>{(a.skills || []).slice(0, 3).map((s, i) => (
                  <span key={i} className="macos-tag" style={{ marginRight: 4 }}>{s.name || s.id || 'skill'}</span>
                ))}</td>
                <td>
                  <button onClick={() => setDetailAgent(a)}
                    style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--accent)', fontSize: 12 }}>详情</button>
                  <button onClick={() => handleDelete(a.id, a.name)}
                    style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--red)', fontSize: 12, marginLeft: 8 }}>删除</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {detailAgent && (
        <div className="detail-drawer open">
          <div className="detail-drawer-header">
            <h3 style={{ fontSize: 16, fontWeight: 600 }}>{detailAgent.name}</h3>
            <button onClick={() => setDetailAgent(null)}
              style={{ background: 'none', border: 'none', fontSize: 20, cursor: 'pointer', color: 'var(--text-secondary)' }}>✕</button>
          </div>
          <div className="detail-drawer-body">
            <div style={{ marginBottom: 16 }}>
              <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 4 }}>ID</div>
              <div style={{ fontFamily: "'SF Mono','Menlo',monospace", fontSize: 12 }}>{detailAgent.id}</div>
            </div>
            <div style={{ marginBottom: 16 }}>
              <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 4 }}>Status</div>
              <StatusTag status={detailAgent.status || 'unknown'} />
            </div>
            {detailAgent.url && (
              <div style={{ marginBottom: 16 }}>
                <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 4 }}>URL</div>
                <div style={{ fontSize: 12 }}>{detailAgent.url}</div>
              </div>
            )}
            {detailAgent.description && (
              <div style={{ marginBottom: 16 }}>
                <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 4 }}>Description</div>
                <div style={{ fontSize: 12 }}>{detailAgent.description}</div>
              </div>
            )}
            <div style={{ marginBottom: 16 }}>
              <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 4 }}>Skills</div>
              <div className="agent-tags">
                {(detailAgent.skills || []).map((s, i) => (
                  <span key={i} className="macos-tag">{s.name || s.id || 'skill'}</span>
                ))}
                {(!detailAgent.skills || detailAgent.skills.length === 0) && <span style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>None</span>}
              </div>
            </div>
          </div>
        </div>
      )}

      {showRegister && (
        <div style={{
          position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.3)', backdropFilter: 'blur(4px)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 200,
        }}>
          <div style={{ background: 'var(--bg-card)', borderRadius: 14, padding: 24, width: 420, boxShadow: 'var(--shadow-lg)' }}>
            <h3 style={{ fontSize: 16, fontWeight: 600, marginBottom: 16 }}>Register Agent</h3>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <input placeholder="Agent Name *" value={regForm.name}
                onChange={(e) => setRegForm({ ...regForm, name: e.target.value })}
                style={{ padding: '10px 14px', borderRadius: 8, border: '1px solid var(--border)', background: 'var(--bg)', fontSize: 13, outline: 'none' }} />
              <input placeholder="Agent URL" value={regForm.url}
                onChange={(e) => setRegForm({ ...regForm, url: e.target.value })}
                style={{ padding: '10px 14px', borderRadius: 8, border: '1px solid var(--border)', background: 'var(--bg)', fontSize: 13, outline: 'none' }} />
              <textarea placeholder="Description" value={regForm.description} rows={3}
                onChange={(e) => setRegForm({ ...regForm, description: e.target.value })}
                style={{ padding: '10px 14px', borderRadius: 8, border: '1px solid var(--border)', background: 'var(--bg)', fontSize: 13, outline: 'none', resize: 'vertical' }} />
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 16 }}>
              <button onClick={() => setShowRegister(false)}
                style={{ ...btnStyle, background: 'transparent', border: '1px solid var(--border)' }}>Cancel</button>
              <button onClick={handleRegister} style={{ ...btnStyle, ...btnPrimaryStyle }}>Register</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

const btnStyle: React.CSSProperties = { padding: '8px 16px', borderRadius: 8, border: 'none', cursor: 'pointer', fontSize: 12, fontWeight: 500 };
const btnPrimaryStyle: React.CSSProperties = { background: 'linear-gradient(135deg, #007AFF, #AF52DE)', color: 'white' };

export default Agents;