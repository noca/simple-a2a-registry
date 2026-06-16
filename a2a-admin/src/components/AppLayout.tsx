import React from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { useStore } from '../store/useStore';
import { useAuthStore } from '../store/authStore';

const navItems = [
  { key: 'dashboard', icon: '📊', label: 'Dashboard' },
  { key: 'agents', icon: '🤖', label: 'Agents' },
  { key: 'tasks', icon: '🔨', label: 'Tasks' },
  { key: 'kanban', icon: '📋', label: 'Kanban' },
  { key: 'swarm', icon: '🕸️', label: 'Swarm' },
  { key: 'clients', icon: '🔑', label: 'Clients' },
  { key: 'audit', icon: '📝', label: 'Audit Log' },
  { key: 'settings', icon: '⚙️', label: 'Settings' },
  { key: 'security-events', icon: '🔒', label: 'Security Events' },
  { key: 'authorizations', icon: '🔐', label: '授权管理' },
  { key: 'users', icon: '👥', label: 'Users' },
];

const AppLayout: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const navigate = useNavigate();
  const location = useLocation();
  const currentPath = location.pathname.replace('/', '') || 'dashboard';
  const { darkMode, toggleTheme } = useStore();
  const { user } = useAuthStore();

  const handleLogout = () => {
    useAuthStore.getState().logout().then(() => {
      navigate('/login', { replace: true });
    });
  };

  return (
    <div className="app-layout">
      {/* Sidebar */}
      <div className="sidebar">
        <div className="sidebar-logo">
          <div className="sidebar-logo-icon">A2</div>
          <span className="sidebar-logo-text">A2A Registry</span>
        </div>
        <nav className="sidebar-nav">
          {navItems.map((item) => (
            <button
              key={item.key}
              className={`sidebar-nav-item ${currentPath === item.key ? 'active' : ''}`}
              onClick={() => navigate(`/${item.key}`)}
            >
              <span className="sidebar-nav-icon">{item.icon}</span>
              <span>{item.label}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-footer">
          <div className="sidebar-user">
            <span>🌙</span>
            <span style={{ flex: 1 }}>Dark Mode</span>
            <label className="macos-toggle" onClick={(e) => { e.stopPropagation(); toggleTheme(); }}>
              <input type="checkbox" checked={darkMode} onChange={toggleTheme} />
              <span className="slider" />
            </label>
          </div>
          {/* Logout button */}
          <button className="sidebar-logout" onClick={handleLogout}>
            <span>🚪</span>
            <span>{user?.display_name || user?.username || '退出登录'}</span>
            <span style={{ marginLeft: 'auto', fontSize: 11, opacity: 0.5 }}>退出</span>
          </button>
        </div>
      </div>

      {/* Main Content */}
      <main className="main-content">{children}</main>
    </div>
  );
};

export default AppLayout;