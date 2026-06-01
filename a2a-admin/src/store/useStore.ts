import { create } from 'zustand';

interface AppState {
  // Auth
  user: { username: string; role: string } | null;
  authEnabled: boolean;
  login: (user: { username: string; role: string }) => void;
  logout: () => void;

  // Toast notifications
  toasts: Array<{ id: number; type: string; message: string }>;
  addToast: (type: string, message: string) => void;
  removeToast: (id: number) => void;

  // Agent stats (dashboard)
  stats: {
    totalAgents: number;
    aliveAgents: number;
    staleAgents: number;
    totalTasks: number;
    completedTasks: number;
    blockedTasks: number;
  } | null;
  setStats: (s: any) => void;

  // Theme
  darkMode: boolean;
  toggleTheme: () => void;
}

let toastId = 0;

export const useStore = create<AppState>((set) => ({
  user: null,
  authEnabled: (window as any).__AUTH_CONFIG?.enabled ?? false,
  login: (user) => set({ user }),
  logout: () => {
    sessionStorage.removeItem('token');
    localStorage.removeItem('token');
    set({ user: null });
    window.location.reload();
  },

  toasts: [],
  addToast: (type, message) => {
    const id = ++toastId;
    set((s) => ({ toasts: [...s.toasts, { id, type, message }] }));
    setTimeout(() => {
      set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) }));
    }, 3000);
  },
  removeToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),

  stats: null,
  setStats: (s) => set({ stats: s }),

  darkMode: false,
  toggleTheme: () => set((s) => ({ darkMode: !s.darkMode })),
}));