import { create } from 'zustand';
import { getMe, login as apiLogin, logout as apiLogout } from '../api/client';

interface User {
  username: string;
  role: string;
  display_name?: string;
}

interface AuthState {
  user: User | null;
  checking: boolean;
  error: string | null;
  checkAuth: () => Promise<void>;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  setUser: (u: User | null) => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  checking: true,
  error: null,

  checkAuth: async () => {
    set({ checking: true });
    try {
      const data = await getMe();
      set({ user: data.user || data, checking: false });
    } catch {
      set({ user: null, checking: false });
    }
  },

  login: async (username, password) => {
    set({ error: null });
    try {
      const data = await apiLogin(username, password);
      if (data.token) {
        sessionStorage.setItem('token', data.token);
      }
      set({ user: data.user || { username, role: 'admin' }, error: null });
    } catch (err: any) {
      const msg = err?.response?.data?.detail || err?.response?.data?.error || '登录失败';
      set({ error: msg });
      throw new Error(msg);
    }
  },

  logout: async () => {
    try { await apiLogout(); } catch {}
    sessionStorage.removeItem('token');
    localStorage.removeItem('token');
    localStorage.removeItem('user');
    set({ user: null });
  },

  setUser: (u) => set({ user: u }),
}));
