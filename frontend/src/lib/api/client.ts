export const API_BASE = import.meta.env.VITE_API_BASE ?? '';

const TENANT_STORAGE_KEY = 'datamorph_user_id';

/**
 * Returns or initializes a persistent anonymous user UUID for multi-tenancy data isolation.
 */
export function getTenantUserId(): string {
  if (typeof window === 'undefined') return 'default';
  try {
    let uid = localStorage.getItem(TENANT_STORAGE_KEY);
    if (!uid) {
      if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
        uid = crypto.randomUUID();
      } else {
        uid = 'usr_' + Math.random().toString(36).substring(2, 15) + Math.random().toString(36).substring(2, 15);
      }
      localStorage.setItem(TENANT_STORAGE_KEY, uid);
    }
    return uid;
  } catch {
    return 'default';
  }
}

/**
 * Attaches the current tenant user ID to standard headers.
 */
export function getAuthHeaders(customHeaders: Record<string, string> = {}): Record<string, string> {
  return {
    'X-User-Id': getTenantUserId(),
    ...customHeaders,
  };
}

/**
 * Standard fetch wrapper that automatically injects tenant identity headers.
 */
export async function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers || {});
  if (!headers.has('X-User-Id')) {
    headers.set('X-User-Id', getTenantUserId());
  }
  return fetch(input, {
    ...init,
    headers,
  });
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

export async function parseJsonOrThrow<T>(res: Response, fallbackMessage: string): Promise<T> {
  if (!res.ok) {
    const errorData = await res.json().catch(() => ({}));
    throw new Error(errorData.detail || fallbackMessage);
  }
  return res.json();
}

export async function triggerBlobDownload(res: Response, filename: string, fallbackMessage: string): Promise<void> {
  if (!res.ok) {
    const errorData = await res.json().catch(() => ({}));
    throw new Error(errorData.detail || fallbackMessage);
  }
  const blob = await res.blob();
  const downloadUrl = window.URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = downloadUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.URL.revokeObjectURL(downloadUrl);
}

export function xhrFormUpload<T>(
  url: string,
  formData: FormData,
  options: {
    onProgress?: (loaded: number, total: number) => void;
    defaultError: string;
    statusErrorPrefix: string;
    networkError: string;
    timeoutError?: string;
    abortError?: string;
  },
): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    xhr.setRequestHeader('X-User-Id', getTenantUserId());

    if (options.onProgress && xhr.upload) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          options.onProgress!(e.loaded, e.total);
        }
      };
    }

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText));
        } catch {
          reject(new Error('Invalid response received from server.'));
        }
      } else {
        try {
          const errData = JSON.parse(xhr.responseText);
          reject(new Error(errData.detail || options.defaultError));
        } catch {
          reject(new Error(`${options.statusErrorPrefix} ${xhr.status}`));
        }
      }
    };

    xhr.onerror = () => reject(new Error(options.networkError));
    if (options.timeoutError) {
      xhr.ontimeout = () => reject(new Error(options.timeoutError));
    }
    if (options.abortError) {
      xhr.onabort = () => reject(new Error(options.abortError));
    }

    xhr.send(formData);
  });
}

export async function getStorageInventory(): Promise<any> {
  const res = await apiFetch(`${API_BASE}/api/maintenance/storage`);
  return parseJsonOrThrow(res, 'Failed to fetch storage inventory.');
}

export async function triggerStorageCleanup(maxAgeHours?: number): Promise<any> {
  const url =
    maxAgeHours !== undefined
      ? `${API_BASE}/api/maintenance/cleanup?max_age_hours=${encodeURIComponent(maxAgeHours)}`
      : `${API_BASE}/api/maintenance/cleanup`;
  const res = await apiFetch(url, { method: 'POST' });
  return parseJsonOrThrow(res, 'Failed to trigger storage cleanup.');
}
