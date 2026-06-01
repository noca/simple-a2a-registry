import React, { useEffect, useState, useCallback } from 'react';
import { taskAPI, swarmAPI } from '../api/client';
import { useStore } from '../store/useStore';
import StatusTag from '../components/StatusTag';

/* ------------------------------------------------------------------ */
/*  Types                                                             */
/* ------------------------------------------------------------------ */

interface SwarmNode {
  id: string;
  role: 'root' | 'worker' | 'verifier' | 'synthesizer';
  status: string;
  assignee: string;
  created_at?: number;
  started_at?: number;
  completed_at?: number;
  title?: string;
}

interface SwarmData {
  swarm: { root_id: string; status: string; worker_ids: string[]; verifier_id: string; synthesizer_id: string };
  workers: SwarmNode[];
  verifier: SwarmNode | null;
  synthesizer: SwarmNode | null;
  blackboard: Record<string, any>;
}

/* ------------------------------------------------------------------ */
/*  Constants                                                         */
/* ------------------------------------------------------------------ */

const NODE_COLORS: Record<string, { fill: string; stroke: string; text: string }> = {
  root:       { fill: '#007AFF', stroke: '#0066D6', text: '#FFFFFF' },
  worker:     { fill: '#BF5AF2', stroke: '#9B3DD4', text: '#FFFFFF' },
  verifier:   { fill: '#FF9F0A', stroke: '#D68500', text: '#1D1D1F' },
  synthesizer: { fill: '#30D158', stroke: '#248A3D', text: '#1D1D1F' },
};

const STATUS_DOT_COLORS: Record<string, string> = {
  todo: '#86868B', ready: '#007AFF', running: '#BF5AF2',
  completed: '#30D158', blocked: '#FF9F0A', failed: '#FF453A',
  cancelled: '#86868B', archived: '#86868B',
};

const LAYER = {
  ROOT_Y: 50,
  WORKER_Y: 180,
  VERIFIER_Y: 340,
  SYNTHESIZER_Y: 460,
  NODE_W: 140,
  NODE_H: 60,
  GAP_X: 40,
};

/* ------------------------------------------------------------------ */
/*  SVG Topology Diagram                                              */
/* ------------------------------------------------------------------ */

const TopologyDiagram: React.FC<{ data: SwarmData }> = ({ data }) => {
  const { swarm, workers, verifier, synthesizer } = data;
  const workerCount = workers.length;
  const cols = Math.max(workerCount, 1);
  const svgW = cols * (LAYER.NODE_W + LAYER.GAP_X) + 80;
  const svgH = 560;

  const centerX = (idx: number) =>
    40 + idx * (LAYER.NODE_W + LAYER.GAP_X) + LAYER.NODE_W / 2;

  const rootCx = svgW / 2;
  const verifierCx = svgW / 2;
  const synthCx = svgW / 2;

  const nodeBox = (cx: number, cy: number) => ({
    x: cx - LAYER.NODE_W / 2,
    y: cy - LAYER.NODE_H / 2,
    w: LAYER.NODE_W,
    h: LAYER.NODE_H,
  });

  // Connection lines
  const connections: { x1: number; y1: number; x2: number; y2: number }[] = [];

  // Root → each Worker
  for (let i = 0; i < workerCount; i++) {
    const wx = centerX(i);
    connections.push({ x1: rootCx, y1: LAYER.ROOT_Y + LAYER.NODE_H / 2, x2: wx, y2: LAYER.WORKER_Y - LAYER.NODE_H / 2 });
  }

  // Each Worker → Verifier
  for (let i = 0; i < workerCount; i++) {
    const wx = centerX(i);
    connections.push({ x1: wx, y1: LAYER.WORKER_Y + LAYER.NODE_H / 2, x2: verifierCx, y2: LAYER.VERIFIER_Y - LAYER.NODE_H / 2 });
  }

  // Verifier → Synthesizer
  connections.push({ x1: verifierCx, y1: LAYER.VERIFIER_Y + LAYER.NODE_H / 2, x2: synthCx, y2: LAYER.SYNTHESIZER_Y - LAYER.NODE_H / 2 });

  const renderNode = (cx: number, cy: number, node: SwarmNode) => {
    const roleColors = NODE_COLORS[node.role] || NODE_COLORS.root;
    const box = nodeBox(cx, cy);
    return (
      <g key={node.id}>
        {/* Shadow */}
        <rect x={box.x + 2} y={box.y + 2} width={box.w} height={box.h} rx={10} fill="rgba(0,0,0,0.08)" />
        {/* Card */}
        <rect x={box.x} y={box.y} width={box.w} height={box.h} rx={10} fill="#FFFFFF" stroke={roleColors.stroke} strokeWidth={1.5} />
        {/* Role badge */}
        <rect x={box.x} y={box.y} width={box.w} height={22} rx={10} ry={10} fill={roleColors.fill} />
        <rect x={box.x} y={box.y + 12} width={box.w} height={10} fill={roleColors.fill} />
        <text x={cx} y={box.y + 15} textAnchor="middle" fill={roleColors.text} fontSize={10} fontWeight={600}>
          {node.role.toUpperCase()}
        </text>
        {/* Status indicator */}
        <circle cx={box.x + box.w - 14} cy={box.y + box.h - 14} r={5}
          fill={STATUS_DOT_COLORS[node.status] || '#86868B'} />
        {/* Assignee */}
        <text x={cx} y={box.y + 37} textAnchor="middle" fill="#1D1D1F" fontSize={11} fontWeight={500}>
          {node.assignee || '-'}
        </text>
        <text x={cx} y={box.y + 50} textAnchor="middle" fill="#86868B" fontSize={9}>
          {node.status}
        </text>
      </g>
    );
  };

  return (
    <svg width={svgW} height={svgH} viewBox={`0 0 ${svgW} ${svgH}`} style={{ display: 'block', margin: '0 auto' }}>
      {/* Layer labels */}
      <text x={12} y={LAYER.ROOT_Y} fill="#C7C7CC" fontSize={10} fontWeight={500}>Root</text>
      <text x={12} y={LAYER.WORKER_Y} fill="#C7C7CC" fontSize={10} fontWeight={500}>Workers</text>
      <text x={12} y={LAYER.VERIFIER_Y} fill="#C7C7CC" fontSize={10} fontWeight={500}>Verifier</text>
      <text x={12} y={LAYER.SYNTHESIZER_Y} fill="#C7C7CC" fontSize={10} fontWeight={500}>Synthesizer</text>

      {/* Connection lines */}
      {connections.map((c, i) => (
        <line key={i} x1={c.x1} y1={c.y1} x2={c.x2} y2={c.y2}
          stroke="#C7C7CC" strokeWidth={1.5} strokeDasharray="4 3" />
      ))}

      {/* Root node */}
      {renderNode(rootCx, LAYER.ROOT_Y, {
        id: swarm.root_id,
        role: 'root',
        status: swarm.status,
        assignee: 'swarm-orch',
      })}

      {/* Worker nodes */}
      {workers.map((w, i) => renderNode(centerX(i), LAYER.WORKER_Y, { ...w, role: 'worker' }))}

      {/* Verifier node */}
      {verifier && renderNode(verifierCx, LAYER.VERIFIER_Y, { ...verifier, role: 'verifier' })}

      {/* Synthesizer node */}
      {synthesizer && renderNode(synthCx, LAYER.SYNTHESIZER_Y, { ...synthesizer, role: 'synthesizer' })}
    </svg>
  );
};

/* ------------------------------------------------------------------ */
/*  Node Status Panel                                                 */
/* ------------------------------------------------------------------ */

const NodeStatusTable: React.FC<{ nodes: SwarmNode[]; title: string }> = ({ nodes, title }) => (
  <div className="swarm-card">
    <h3 className="swarm-card-title">{title}</h3>
    {nodes.length === 0 ? (
      <div className="swarm-empty">暂无节点</div>
    ) : (
      <table className="macos-table">
        <thead>
          <tr>
            <th>ID</th>
            <th>角色</th>
            <th>状态</th>
            <th>Assignee</th>
            <th>开始时间</th>
            <th>完成时间</th>
          </tr>
        </thead>
        <tbody>
          {nodes.map((n) => (
            <tr key={n.id}>
              <td style={{ maxWidth: 130, overflow: 'hidden', textOverflow: 'ellipsis', fontSize: 11 }}>{n.id}</td>
              <td><span className={`swarm-role-badge ${n.role}`}>{n.role}</span></td>
              <td><StatusTag status={n.status} /></td>
              <td style={{ color: 'var(--text-secondary)' }}>{n.assignee || '-'}</td>
              <td style={{ color: 'var(--text-secondary)' }}>{n.started_at ? new Date(n.started_at * 1000).toLocaleString() : '-'}</td>
              <td style={{ color: 'var(--text-secondary)' }}>{n.completed_at ? new Date(n.completed_at * 1000).toLocaleString() : '-'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    )}
  </div>
);

/* ------------------------------------------------------------------ */
/*  Blackboard Panel                                                  */
/* ------------------------------------------------------------------ */

const BlackboardPanel: React.FC<{ blackboard: Record<string, any> }> = ({ blackboard }) => {
  if (!blackboard || Object.keys(blackboard).length === 0) {
    return (
      <div className="swarm-card">
        <h3 className="swarm-card-title">📋 Blackboard</h3>
        <div className="swarm-empty">暂无黑板数据</div>
      </div>
    );
  }

  const entries = Object.entries(blackboard).filter(([k]) => k !== '_authors');
  const authors = blackboard._authors || {};

  return (
    <div className="swarm-card">
      <h3 className="swarm-card-title">📋 Blackboard</h3>
      {entries.length === 0 ? (
        <div className="swarm-empty">暂无黑板条目</div>
      ) : (
        <table className="macos-table">
          <thead>
            <tr>
              <th>Key</th>
              <th>Value</th>
              <th>Author</th>
            </tr>
          </thead>
          <tbody>
            {entries.map(([key, value]) => (
              <tr key={key}>
                <td style={{ fontWeight: 500 }}>{key}</td>
                <td style={{ fontSize: 11, maxWidth: 300, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {typeof value === 'object' ? JSON.stringify(value, null, 1).substring(0, 200) : String(value).substring(0, 200)}
                </td>
                <td style={{ color: 'var(--text-secondary)' }}>{authors[key] || '-'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
};

/* ------------------------------------------------------------------ */
/*  Create Swarm Form                                                 */
/* ------------------------------------------------------------------ */

const CreateSwarmForm: React.FC<{ onCreated: (rootId: string) => void }> = ({ onCreated }) => {
  const { addToast } = useStore();
  const [open, setOpen] = useState(false);
  const [goal, setGoal] = useState('');
  const [rootTitle, setRootTitle] = useState('');
  const [verifierProfile, setVerifierProfile] = useState('');
  const [synthesizerProfile, setSynthesizerProfile] = useState('');
  const [workerSpecs, setWorkerSpecs] = useState<{ profile: string; title: string; body?: string; skills?: string[] }[]>([
    { profile: '', title: '' },
  ]);
  const [creating, setCreating] = useState(false);

  const addWorker = () => setWorkerSpecs([...workerSpecs, { profile: '', title: '' }]);
  const removeWorker = (i: number) => {
    if (workerSpecs.length <= 1) return;
    setWorkerSpecs(workerSpecs.filter((_, idx) => idx !== i));
  };
  const updateWorker = (i: number, field: string, value: string) => {
    const updated = [...workerSpecs];
    (updated[i] as any)[field] = value;
    setWorkerSpecs(updated);
  };

  const handleCreate = async () => {
    if (!goal.trim()) { addToast('error', '请输入 goal'); return; }
    if (!verifierProfile.trim()) { addToast('error', '请输入 Verifier profile'); return; }
    if (!synthesizerProfile.trim()) { addToast('error', '请输入 Synthesizer profile'); return; }
    const validWorkers = workerSpecs.filter((w) => w.profile.trim() && w.title.trim());
    if (validWorkers.length === 0) { addToast('error', '至少需要一个 Worker'); return; }

    setCreating(true);
    try {
      const result = await swarmAPI.create({
        goal: goal.trim(),
        root_title: rootTitle.trim() || undefined,
        workers: validWorkers.map((w) => ({
          profile: w.profile.trim(),
          title: w.title.trim(),
          body: w.body?.trim() || undefined,
          skills: w.skills && w.skills.length > 0 ? w.skills : undefined,
        })),
        verifier: { profile: verifierProfile.trim() },
        synthesizer: { profile: synthesizerProfile.trim() },
      });
      addToast('success', `Swarm 创建成功: ${result.swarm.root_id}`);
      onCreated(result.swarm.root_id);
      setOpen(false);
      // Reset form
      setGoal('');
      setRootTitle('');
      setVerifierProfile('');
      setSynthesizerProfile('');
      setWorkerSpecs([{ profile: '', title: '' }]);
    } catch (e: any) {
      addToast('error', `创建失败: ${e?.response?.data?.detail || e?.message || '未知错误'}`);
    } finally {
      setCreating(false);
    }
  };

  if (!open) {
    return (
      <button className="swarm-create-btn" onClick={() => setOpen(true)}>
        + 创建新 Swarm
      </button>
    );
  }

  return (
    <div className="swarm-card swarm-form-card">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <h3 className="swarm-card-title" style={{ margin: 0 }}>🚀 创建新 Swarm</h3>
        <button className="macos-btn-secondary" onClick={() => setOpen(false)} style={{ fontSize: 12 }}>取消</button>
      </div>

      <div className="swarm-form-grid">
        <div className="swarm-form-field">
          <label>Goal *</label>
          <textarea value={goal} onChange={(e) => setGoal(e.target.value)} rows={2} placeholder="Swarm 目标描述" />
        </div>
        <div className="swarm-form-field">
          <label>Root Title（可选）</label>
          <input value={rootTitle} onChange={(e) => setRootTitle(e.target.value)} placeholder="Swarm: ..." />
        </div>
      </div>

      <div className="swarm-form-section">
        <h4>🎯 Verifier & Synthesizer</h4>
        <div className="swarm-form-grid" style={{ gridTemplateColumns: '1fr 1fr' }}>
          <div className="swarm-form-field">
            <label>Verifier Profile *</label>
            <input value={verifierProfile} onChange={(e) => setVerifierProfile(e.target.value)} placeholder="verifier" />
          </div>
          <div className="swarm-form-field">
            <label>Synthesizer Profile *</label>
            <input value={synthesizerProfile} onChange={(e) => setSynthesizerProfile(e.target.value)} placeholder="synthesizer" />
          </div>
        </div>
      </div>

      <div className="swarm-form-section">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h4>👷 Workers</h4>
          <button className="macos-btn-secondary" onClick={addWorker} style={{ fontSize: 11, padding: '2px 10px' }}>+ 添加 Worker</button>
        </div>
        {workerSpecs.map((w, i) => (
          <div key={i} className="swarm-worker-row">
            <div className="swarm-form-field" style={{ flex: 1 }}>
              <label>Profile</label>
              <input value={w.profile} onChange={(e) => updateWorker(i, 'profile', e.target.value)} placeholder="worker-profile" />
            </div>
            <div className="swarm-form-field" style={{ flex: 1 }}>
              <label>Title</label>
              <input value={w.title} onChange={(e) => updateWorker(i, 'title', e.target.value)} placeholder="Worker task title" />
            </div>
            <button className="macos-btn-danger" onClick={() => removeWorker(i)} disabled={workerSpecs.length <= 1}
              style={{ fontSize: 16, padding: '4px 8px', marginTop: 20, height: 30 }}>×</button>
          </div>
        ))}
      </div>

      <button className="macos-btn-primary" onClick={handleCreate} disabled={creating}
        style={{ marginTop: 16, width: '100%' }}>
        {creating ? '创建中...' : '创建 Swarm'}
      </button>
    </div>
  );
};

/* ------------------------------------------------------------------ */
/*  Swarm Detail View                                                 */
/* ------------------------------------------------------------------ */

const SwarmDetail: React.FC<{ rootId: string; onBack: () => void }> = ({ rootId, onBack }) => {
  const { addToast } = useStore();
  const [data, setData] = useState<SwarmData | null>(null);
  const [loading, setLoading] = useState(true);
  const [autoRefresh, setAutoRefresh] = useState(false);
  let refreshTimer: ReturnType<typeof setInterval> | null = null;

  const fetchSwarm = useCallback(async () => {
    try {
      const result = await swarmAPI.get(rootId);
      setData(result);
    } catch (e: any) {
      if (!refreshTimer) {
        addToast('error', `加载失败: ${e?.response?.data?.detail || e?.message || '未知错误'}`);
        setLoading(false);
      }
    } finally {
      setLoading(false);
    }
  }, [rootId]);

  useEffect(() => {
    fetchSwarm();
  }, [fetchSwarm]);

  useEffect(() => {
    if (autoRefresh) {
      refreshTimer = setInterval(fetchSwarm, 10000);
    } else {
      if (refreshTimer) clearInterval(refreshTimer);
      refreshTimer = null;
    }
    return () => { if (refreshTimer) clearInterval(refreshTimer); };
  }, [autoRefresh, fetchSwarm]);

  if (loading) {
    return (
      <div className="swarm-card" style={{ textAlign: 'center', padding: 40 }}>
        <div className="loading-skeleton" style={{ width: 200, height: 16, margin: '0 auto 12px' }} />
        <div className="loading-skeleton" style={{ width: 140, height: 12, margin: '0 auto' }} />
      </div>
    );
  }

  if (!data) {
    return (
      <div className="swarm-card" style={{ textAlign: 'center', padding: 40 }}>
        <p>无法加载 Swarm 数据。请检查 root_id 是否正确。</p>
        <button className="macos-btn-secondary" onClick={onBack}>返回</button>
      </div>
    );
  }

  const allNodes: SwarmNode[] = [
    ...(data.swarm ? [{ id: data.swarm.root_id, role: 'root' as const, status: data.swarm.status, assignee: 'swarm-orch' }] : []),
    ...(data.workers || []).map((w: any) => ({ ...w, role: 'worker' as const })),
    ...(data.verifier ? [{ ...data.verifier, role: 'verifier' as const }] : []),
    ...(data.synthesizer ? [{ ...data.synthesizer, role: 'synthesizer' as const }] : []),
  ];

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <div>
          <button className="macos-btn-secondary" onClick={onBack} style={{ marginRight: 12, fontSize: 12 }}>← 返回列表</button>
          <span className="swarm-detail-title">Swarm: {rootId}</span>
        </div>
        <label className="swarm-toggle-label">
          <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>自动刷新 (10s)</span>
          <label className="macos-toggle" style={{ marginLeft: 6 }} onClick={() => setAutoRefresh(!autoRefresh)}>
            <input type="checkbox" checked={autoRefresh} onChange={() => setAutoRefresh(!autoRefresh)} />
            <span className="slider" />
          </label>
        </label>
      </div>

      {/* Topology Diagram */}
      <div className="swarm-card" style={{ overflow: 'auto' }}>
        <TopologyDiagram data={data} />
      </div>

      {/* Status panels */}
      <div className="swarm-detail-grid">
        <NodeStatusTable nodes={allNodes} title="📊 全部节点状态" />
        <BlackboardPanel blackboard={data.blackboard || {}} />
      </div>
    </div>
  );
};

/* ------------------------------------------------------------------ */
/*  Main Page Component                                               */
/* ------------------------------------------------------------------ */

const SwarmTopology: React.FC = () => {
  const { addToast } = useStore();
  const [swarmList, setSwarmList] = useState<{ id: string; title: string; status: string; created_at?: number }[]>([]);
  const [activeRootId, setActiveRootId] = useState<string | null>(null);
  const [searchId, setSearchId] = useState('');
  const [loadingList, setLoadingList] = useState(true);

  // Load swarm list: find tasks with "Swarm:" prefix in title
  const loadSwarms = useCallback(async () => {
    setLoadingList(true);
    try {
      const result = await taskAPI.listV2({ limit: '50' });
      const tasks: any[] = result.tasks || [];
      // Filter tasks that look like swarm roots (title starts with "Swarm:" or is a known pattern)
      const swarms = tasks
        .filter((t: any) => t.title?.startsWith('Swarm:') || t.title?.startsWith('Swarm —'))
        .map((t: any) => ({ id: t.id, title: t.title, status: t.status, created_at: t.created_at }))
        .sort((a: any, b: any) => (b.created_at || 0) - (a.created_at || 0));
      setSwarmList(swarms);
    } catch (e: any) {
      addToast('error', `加载列表失败: ${e?.message || '未知错误'}`);
    } finally {
      setLoadingList(false);
    }
  }, [addToast]);

  useEffect(() => {
    loadSwarms();
  }, [loadSwarms]);

  const handleSearch = () => {
    if (!searchId.trim()) return;
    setActiveRootId(searchId.trim());
  };

  const handleCreated = (rootId: string) => {
    setActiveRootId(rootId);
    loadSwarms();
  };

  if (activeRootId) {
    return (
      <div>
        <SwarmDetail rootId={activeRootId} onBack={() => setActiveRootId(null)} />
      </div>
    );
  }

  return (
    <div>
      <h1 className="page-title">🕸️ Swarm 拓扑</h1>

      {/* Create + Search */}
      <div style={{ display: 'flex', gap: 12, marginBottom: 20, alignItems: 'flex-start' }}>
        <CreateSwarmForm onCreated={handleCreated} />
        <div className="swarm-card" style={{ flex: 1, display: 'flex', alignItems: 'center', gap: 8, padding: '12px 16px' }}>
          <input
            className="macos-input"
            value={searchId}
            onChange={(e) => setSearchId(e.target.value)}
            placeholder="输入 Swarm root_id 查看详情"
            style={{ flex: 1, height: 32 }}
            onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
          />
          <button className="macos-btn-primary" onClick={handleSearch} style={{ height: 32, fontSize: 12 }}>
            查看
          </button>
        </div>
      </div>

      {/* Swarm List */}
      <div className="swarm-card">
        <h3 className="swarm-card-title">📋 已有 Swarm</h3>
        {loadingList ? (
          <div style={{ padding: 20, textAlign: 'center' }}>
            <div className="loading-skeleton" style={{ width: 120, height: 14, margin: '0 auto' }} />
          </div>
        ) : swarmList.length === 0 ? (
          <div className="swarm-empty">暂无 Swarm。创建一个新的或输入 root_id 查看。</div>
        ) : (
          <table className="macos-table">
            <thead>
              <tr>
                <th>标题</th>
                <th>ID</th>
                <th>状态</th>
                <th>创建时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {swarmList.map((s) => (
                <tr key={s.id}>
                  <td style={{ fontWeight: 500, maxWidth: 250, overflow: 'hidden', textOverflow: 'ellipsis' }}>{s.title}</td>
                  <td style={{ fontSize: 11, color: 'var(--text-secondary)' }}>{s.id}</td>
                  <td><StatusTag status={s.status} /></td>
                  <td style={{ color: 'var(--text-secondary)', fontSize: 11 }}>
                    {s.created_at ? new Date(s.created_at * 1000).toLocaleString() : '-'}
                  </td>
                  <td>
                    <button className="macos-btn-secondary" onClick={() => setActiveRootId(s.id)} style={{ fontSize: 11, padding: '2px 10px' }}>
                      查看拓扑
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
};

export default SwarmTopology;