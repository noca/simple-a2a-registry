import React from 'react';

interface StatusTagProps {
  status: string;
  dot?: boolean;
}

const statusColors: Record<string, { dot: string; bg: string; text: string }> = {
  alive: { dot: '#30D158', bg: 'rgba(48,209,88,0.1)', text: '#248A3D' },
  online: { dot: '#30D158', bg: 'rgba(48,209,88,0.1)', text: '#248A3D' },
  active: { dot: '#30D158', bg: 'rgba(48,209,88,0.1)', text: '#248A3D' },
  stale: { dot: '#FFD60A', bg: 'rgba(255,214,10,0.12)', text: '#AD7D00' },
  offline: { dot: '#FF453A', bg: 'rgba(255,69,58,0.1)', text: '#BF3A2E' },
  disabled: { dot: '#FF453A', bg: 'rgba(255,69,58,0.1)', text: '#BF3A2E' },
  running: { dot: '#BF5AF2', bg: 'rgba(191,90,242,0.1)', text: '#8B3DB8' },
  ready: { dot: '#007AFF', bg: 'rgba(0,122,255,0.08)', text: '#0066D6' },
  completed: { dot: '#30D158', bg: 'rgba(48,209,88,0.1)', text: '#248A3D' },
  blocked: { dot: '#FF9F0A', bg: 'rgba(255,159,10,0.1)', text: '#B37500' },
  failed: { dot: '#FF453A', bg: 'rgba(255,69,58,0.1)', text: '#BF3A2E' },
  todo: { dot: '#86868B', bg: 'rgba(0,0,0,0.05)', text: '#6B6B70' },
  cancelled: { dot: '#86868B', bg: 'rgba(0,0,0,0.05)', text: '#6B6B70' },
};

const StatusTag: React.FC<StatusTagProps> = ({ status, dot = true }) => {
  const normStatus = status?.toLowerCase() || 'todo';
  const colors = statusColors[normStatus] || { dot: '#86868B', bg: 'rgba(0,0,0,0.05)', text: '#6B6B70' };

  return (
    <span
      className="status-badge"
      style={{ backgroundColor: colors.bg, color: colors.text }}
    >
      {dot && (
        <span
          className={`status-dot ${normStatus}`}
          style={{
            animation: normStatus === 'alive' || normStatus === 'online' || normStatus === 'running'
              ? 'pulse 2s ease-in-out infinite'
              : 'none',
          }}
        />
      )}
      {status}
    </span>
  );
};

export default StatusTag;