import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuthStore } from '../store/authStore';

const Login: React.FC = () => {
  const navigate = useNavigate();
  const { login, error } = useAuthStore();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!username.trim() || !password) return;
    setSubmitting(true);
    try {
      await login(username, password);
      navigate('/dashboard', { replace: true });
    } catch {
      // error is set in the store
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div style={{
      minHeight: '100vh',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      background: 'linear-gradient(135deg, #F5F5F7 0%, #E8E8ED 100%)',
    }}>
      <div style={{
        background: 'rgba(255,255,255,0.85)',
        backdropFilter: 'blur(20px)',
        borderRadius: 20,
        padding: 40,
        width: 400,
        boxShadow: '0 12px 40px rgba(0,0,0,0.12), 0 4px 12px rgba(0,0,0,0.06)',
      }}>
        {/* Logo */}
        <div style={{ textAlign: 'center', marginBottom: 32 }}>
          <div style={{
            width: 56, height: 56, borderRadius: 14,
            background: 'linear-gradient(135deg, #007AFF, #AF52DE)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            color: 'white', fontSize: 24, fontWeight: 700,
            margin: '0 auto 16px',
          }}>A2</div>
          <h1 style={{ fontSize: 22, fontWeight: 700, color: '#1D1D1F', marginBottom: 4 }}>A2A Registry</h1>
          <p style={{ fontSize: 13, color: '#86868B' }}>管理后台</p>
        </div>

        <form onSubmit={handleSubmit}>
          <div style={{ marginBottom: 16 }}>
            <label style={{ display: 'block', fontSize: 12, fontWeight: 600, color: '#86868B', marginBottom: 6 }}>
              用户名
            </label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="输入用户名"
              style={{
                width: '100%', padding: '10px 14px', borderRadius: 10,
                border: '1px solid rgba(0,0,0,0.12)', background: 'white',
                fontSize: 14, outline: 'none', boxSizing: 'border-box',
              }}
              autoFocus
            />
          </div>
          <div style={{ marginBottom: 24 }}>
            <label style={{ display: 'block', fontSize: 12, fontWeight: 600, color: '#86868B', marginBottom: 6 }}>
              密码
            </label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="输入密码"
              style={{
                width: '100%', padding: '10px 14px', borderRadius: 10,
                border: '1px solid rgba(0,0,0,0.12)', background: 'white',
                fontSize: 14, outline: 'none', boxSizing: 'border-box',
              }}
            />
          </div>

          {error && (
            <div style={{
              padding: '8px 12px', borderRadius: 8,
              background: 'rgba(255,69,58,0.1)', color: '#BF3A2E',
              fontSize: 12, marginBottom: 16, textAlign: 'center',
            }}>
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={submitting}
            style={{
              width: '100%', padding: '12px 0', borderRadius: 10,
              border: 'none', background: 'linear-gradient(135deg, #007AFF, #AF52DE)',
              color: 'white', fontSize: 14, fontWeight: 600, cursor: submitting ? 'not-allowed' : 'pointer',
              opacity: submitting ? 0.7 : 1,
            }}
          >
            {submitting ? '登录中...' : '登录'}
          </button>
        </form>
      </div>
    </div>
  );
};

export default Login;