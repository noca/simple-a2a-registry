import axios from 'axios';
import type { AxiosInstance } from 'axios';

const BASE_URL = '';

const api: AxiosInstance = axios.create({
  baseURL: BASE_URL,
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
});

// Add auth token interceptor
api.interceptors.request.use((config) => {
  const token = sessionStorage.getItem('token') || localStorage.getItem('token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

api.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err.response?.status === 401) {
      sessionStorage.removeItem('token');
      localStorage.removeItem('token');
      window.location.reload();
    }
    return Promise.reject(err);
  }
);

// --- Agent APIs ---
export const agentAPI = {
  list: (params?: Record<string, string>) =>
    api.get('/v1/agents', { params }).then((r) => r.data),
  get: (id: string) => api.get(`/v1/agents/${id}`).then((r) => r.data),
  register: (data: Record<string, unknown>) =>
    api.post('/v1/agents', data).then((r) => r.data),
  remove: (id: string) => api.delete(`/v1/agents/${id}`).then((r) => r.data),
  update: (id: string, data: Record<string, unknown>) =>
    api.patch(`/v1/agents/${id}`, data).then((r) => r.data),
  heartbeat: (id: string) =>
    api.post(`/v1/agents/${id}/heartbeat`).then((r) => r.data),
  toggle: (id: string) =>
    api.post(`/v1/agents/${id}/toggle`).then((r) => r.data),
};

// --- Task V2 APIs ---
export const taskAPI = {
  listV2: (params?: Record<string, string>) =>
    api.get('/v2/tasks', { params }).then((r) => r.data),
  getV2: (id: string) => api.get(`/v2/tasks/${id}`).then((r) => r.data),
  createV2: (data: Record<string, unknown>) =>
    api.post('/v2/tasks', data).then((r) => r.data),
  updateV2: (id: string, data: Record<string, unknown>) =>
    api.patch(`/v2/tasks/${id}`, data).then((r) => r.data),
  deleteV2: (id: string) => api.delete(`/v2/tasks/${id}`).then((r) => r.data),
  claim: (id: string) => api.post(`/v2/tasks/${id}/claim`).then((r) => r.data),
  complete: (id: string, data?: Record<string, unknown>) =>
    api.post(`/v2/tasks/${id}/complete`, data).then((r) => r.data),
  block: (id: string, reason: string) =>
    api.post(`/v2/tasks/${id}/block`, { reason }).then((r) => r.data),
  unblock: (id: string) => api.post(`/v2/tasks/${id}/unblock`).then((r) => r.data),
  heartbeat: (id: string, note?: string) =>
    api.post(`/v2/tasks/${id}/heartbeat`, { note }).then((r) => r.data),
  comment: (id: string, body: string) =>
    api.post(`/v2/tasks/${id}/comment`, { body }).then((r) => r.data),
  depend: (id: string, parentId: string) =>
    api.post(`/v2/tasks/${id}/depend`, { parent_id: parentId }).then((r) => r.data),
  undepend: (id: string, parentId: string) =>
    api.delete(`/v2/tasks/${id}/depend/${parentId}`).then((r) => r.data),
  batchUpdateStatus: (body: { task_ids: string[]; status: string }) =>
    api.post('/v2/tasks/batch/status', body).then((r) => r.data),
  batchDelete: (body: { task_ids: string[] }) =>
    api.post('/v2/tasks/batch/delete', body).then((r) => r.data),
};

// --- Kanban Stats ---
export const statsAPI = {
  health: () => api.get('/health').then((r) => r.data),
  agentStats: () => api.get('/admin/agent-stats').then((r) => r.data),
  v2Stats: () => api.get('/v2/stats').then((r) => r.data),
};

// --- Admin APIs ---
export const adminAPI = {
  listClients: () => api.get('/admin/clients').then((r) => r.data),
  createClient: (data: Record<string, unknown>) =>
    api.post('/admin/clients', data).then((r) => r.data),
  deleteClient: (id: string) =>
    api.delete(`/admin/clients/${id}`).then((r) => r.data),
  listAudit: (params?: Record<string, string>) =>
    api.get('/admin/audit', { params }).then((r) => r.data),
  listUsers: () => api.get('/admin/users').then((r) => r.data),
  createUser: (data: Record<string, unknown>) =>
    api.post('/admin/users', data).then((r) => r.data),
  updateUser: (username: string, data: Record<string, unknown>) =>
    api.put(`/admin/users/${username}`, data).then((r) => r.data),
  deleteUser: (username: string) =>
    api.delete(`/admin/users/${username}`).then((r) => r.data),
  listSettings: () => api.get('/admin/settings').then((r) => r.data),
  updateSettings: (data: Record<string, unknown>) =>
    api.put('/admin/settings', data).then((r) => r.data),
};

// --- Swarm APIs ---
export const swarmAPI = {
  create: (data: Record<string, unknown>) =>
    api.post('/v2/swarm', data).then((r) => r.data),
  get: (rootId: string) =>
    api.get(`/v2/swarm/${rootId}`).then((r) => r.data),
  comment: (rootId: string, data: Record<string, unknown>) =>
    api.post(`/v2/swarm/${rootId}/comment`, data).then((r) => r.data),
  blackboard: (rootId: string) =>
    api.get(`/v2/swarm/${rootId}/blackboard`).then((r) => r.data),
};

// --- Auth APIs ---
export const authAPI = {
  login: (username: string, password: string) =>
    api.post('/api/login', { username, password }).then((r) => r.data),
  logout: () => api.post('/api/logout').then((r) => r.data),
  me: () => api.get('/api/me').then((r) => r.data),
  token: (data: Record<string, string>) =>
    api.post('/auth/token', data).then((r) => r.data),
};

// --- Named exports (backward compat for page components) ---
export const getMe = () => authAPI.me();
export const login = authAPI.login;
export const logout = authAPI.logout;

export const listV1Tasks = (params?: Record<string, any>) =>
  api.get('/v1/tasks', { params }).then((r) => r.data);

export const listClients = () => adminAPI.listClients();
export const createClient = (data: Record<string, unknown>) => adminAPI.createClient(data);
export const deleteClient = (id: string) => adminAPI.deleteClient(id);

export const listAuditLog = (params?: Record<string, any>) => adminAPI.listAudit(params);

export const listUsers = () => adminAPI.listUsers();
export const createUser = (data: Record<string, unknown>) => adminAPI.createUser(data);
export const updateUser = (username: string, data: Record<string, unknown>) => adminAPI.updateUser(username, data);
export const deleteUser = (username: string) => adminAPI.deleteUser(username);

export default api;