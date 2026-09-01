const API_BASE = '/api/v1';

async function fetchApi<T>(path: string, options?: RequestInit): Promise<T> {
  const token = typeof window !== 'undefined' ? sessionStorage.getItem('slc_access_token') : null;
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options?.headers,
    },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ message: res.statusText }));
    throw new Error(body.message || `API Error: ${res.status}`);
  }
  return res.json();
}

export const api = {
  auth: {
    token: (credentials: { email: string; password: string }) =>
      fetchApi<{ access_token: string; token_type: string; expires_in: number; user: Record<string, unknown> }>('/auth/token', { method: 'POST', body: JSON.stringify(credentials) }),
  },
  applications: {
    list: (params?: { status?: string; page?: number; limit?: number }) => {
      const qs = new URLSearchParams();
      if (params?.status) qs.set('status', params.status);
      if (params?.page) qs.set('page', String(params.page));
      if (params?.limit) qs.set('limit', String(params.limit));
      return fetchApi<{ data: unknown[]; total: number; page: number; limit: number }>(`/applications?${qs}`);
    },
    get: (id: string) => fetchApi<unknown>(`/applications/${id}`),
    update: (id: string, data: unknown) => fetchApi<unknown>(`/applications/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  },
  policies: {
    list: (params?: { status?: string; page?: number; limit?: number }) => {
      const qs = new URLSearchParams();
      if (params?.status) qs.set('status', params.status);
      if (params?.page) qs.set('page', String(params.page));
      if (params?.limit) qs.set('limit', String(params.limit));
      return fetchApi<{ data: unknown[]; total: number; page: number; limit: number }>(`/policies?${qs}`);
    },
    get: (id: string) => fetchApi<unknown>(`/policies/${id}`),
  },
  claims: {
    list: (params?: { status?: string; page?: number; limit?: number }) => {
      const qs = new URLSearchParams();
      if (params?.status) qs.set('status', params.status);
      if (params?.page) qs.set('page', String(params.page));
      if (params?.limit) qs.set('limit', String(params.limit));
      return fetchApi<{ data: unknown[]; total: number; page: number; limit: number }>(`/claims?${qs}`);
    },
    get: (id: string) => fetchApi<unknown>(`/claims/${id}`),
    create: (data: unknown) => fetchApi<unknown>('/claims', { method: 'POST', body: JSON.stringify(data) }),
  },
};
