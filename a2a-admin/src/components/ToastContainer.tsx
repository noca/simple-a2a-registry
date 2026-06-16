import React from 'react';
import { useStore } from '../store/useStore';

const ToastContainer: React.FC = () => {
  const toasts = useStore((s) => s.toasts);
  const removeToast = useStore((s) => s.removeToast);

  if (toasts.length === 0) return null;

  return (
    <div className="macos-toast">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`macos-toast-item toast-${t.type}`}
          onClick={() => removeToast(t.id)}
        >
          {t.type === 'error' ? '❌ ' : t.type === 'success' ? '✅ ' : 'ℹ️ '}
          {t.message}
        </div>
      ))}
    </div>
  );
};

export default ToastContainer;
