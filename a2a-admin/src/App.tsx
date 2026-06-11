import React, { useEffect } from 'react';
import { HashRouter, Routes, Route, Navigate } from 'react-router-dom';
import { ConfigProvider, theme as antTheme } from 'antd';
import AppLayout from './components/AppLayout';
import ErrorBoundary from './components/ErrorBoundary';
import Login from './pages/Login';
import Dashboard from './pages/Dashboard';
import Agents from './pages/Agents';
import KanbanBoard from './pages/KanbanBoard';
import Tasks from './pages/Tasks';
import Clients from './pages/Clients';
import AuditLog from './pages/AuditLog';
import Settings from './pages/Settings';
import UserManagement from './pages/UserManagement';
import SwarmTopology from './pages/SwarmTopology';
import SecurityEvents from './pages/SecurityEvents';
import AuthorizationMatrix from './pages/AuthorizationMatrix';
import { useAuthStore } from './store/authStore';

const GuestRoute: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { user, checking } = useAuthStore();
  const token = sessionStorage.getItem('token') || localStorage.getItem('token');

  if (checking) {
    // While checking auth, show login page content briefly
    return <>{children}</>;
  }

  // Already authenticated — redirect to dashboard
  if (token || user) {
    return <Navigate to="/dashboard" replace />;
  }

  return <>{children}</>;
};

const ProtectedRoute: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { user, checking } = useAuthStore();
  const token = sessionStorage.getItem('token') || localStorage.getItem('token');

  if (checking) {
    // Quick check — if there's a token or the user is already set, show content
    return <>{children}</>;
  }

  // If no auth token stored, show login
  if (!token && !user) {
    return <Navigate to="/login" replace />;
  }

  return <>{children}</>;
};

const App: React.FC = () => {
  const { checkAuth } = useAuthStore();

  useEffect(() => {
    const token = sessionStorage.getItem('token') || localStorage.getItem('token');
    if (token) {
      checkAuth();
    } else {
      useAuthStore.setState({ checking: false });
    }
  }, []);

  return (
    <ConfigProvider
      theme={{
        algorithm: antTheme.defaultAlgorithm,
        token: {
          colorPrimary: '#007AFF',
          borderRadius: 6,
          colorBgBase: '#F5F5F7',
          colorTextBase: '#1D1D1F',
          fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Helvetica Neue', sans-serif",
        },
      }}
    >
      <HashRouter>
        <Routes>
          <Route path="/login" element={<GuestRoute><Login /></GuestRoute>} />
          <Route
            path="*"
            element={
              <ProtectedRoute>
                <AppLayout>
                  <Routes>
                    <Route path="/dashboard" element={<ErrorBoundary><Dashboard /></ErrorBoundary>} />
                    <Route path="/agents" element={<ErrorBoundary><Agents /></ErrorBoundary>} />
                    <Route path="/kanban" element={<ErrorBoundary><KanbanBoard /></ErrorBoundary>} />
                    <Route path="/tasks" element={<ErrorBoundary><Tasks /></ErrorBoundary>} />
                    <Route path="/clients" element={<ErrorBoundary><Clients /></ErrorBoundary>} />
                    <Route path="/audit" element={<ErrorBoundary><AuditLog /></ErrorBoundary>} />
                    <Route path="/settings" element={<ErrorBoundary><Settings /></ErrorBoundary>} />
                    <Route path="/users" element={<ErrorBoundary><UserManagement /></ErrorBoundary>} />
                    <Route path="/swarm" element={<ErrorBoundary><SwarmTopology /></ErrorBoundary>} />
                    <Route path="/security-events" element={<ErrorBoundary><SecurityEvents /></ErrorBoundary>} />
                    <Route path="/authorizations" element={<ErrorBoundary><AuthorizationMatrix /></ErrorBoundary>} />
                    <Route path="/" element={<Navigate to="/dashboard" replace />} />
                    <Route path="*" element={<Navigate to="/dashboard" replace />} />
                  </Routes>
                </AppLayout>
              </ProtectedRoute>
            }
          />
        </Routes>
      </HashRouter>
    </ConfigProvider>
  );
};

export default App;