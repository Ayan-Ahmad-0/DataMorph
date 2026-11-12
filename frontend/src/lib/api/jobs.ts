import { API_BASE, ApiError, apiFetch, getTenantUserId, parseJsonOrThrow, xhrFormUpload } from './client';

export interface ExportOption {
  format_id: string;
  name: string;
  extension: string;
  mime_type: string;
  available: boolean;
  note?: string;
  is_source_format?: boolean;
}

export interface JobMetadata {
  id: string;
  filename: string;
  status: string;
  progress_pct: number;
  current_stage: string;
  file_size_bytes: number;
  row_count: number;
  privacy_mode: boolean;
  detected_format?: string;
  detected_confidence?: number;
  conversion_report?: any;
  error?: string;
  expires_at?: string;
  ttl_seconds_remaining?: number;
}

export interface JobDetailResponse {
  job: JobMetadata;
  profile?: any;
  report_markdown?: string;
  recipe?: any;
  recipe_plain_english: string[];
  open_questions: string[];
  verification?: any;
  conversion_report?: any;
  export_options?: ExportOption[];
  download_urls: Record<string, string>;
  expires_at?: string;
  ttl_seconds_remaining?: number;
}

export async function detectFormat(file: File): Promise<{
  format: string;
  confidence: number;
  filename: string;
  sheets?: string[];
  tables?: string[];
}> {
  const formData = new FormData();
  // Slicing the initial 1 MB avoids transferring multi-gigabyte payloads purely for schema sniffing
  const sample = file.slice(0, 1024 * 1024);
  formData.append('file', sample, file.name);

  const res = await apiFetch(`${API_BASE}/api/formats/detect`, {
    method: 'POST',
    body: formData,
  });
  return parseJsonOrThrow(res, 'Failed to detect format.');
}

export function uploadDataset(
  file: File,
  privacyMode: boolean,
  format?: string,
  options?: Record<string, any>,
  onProgress?: (loaded: number, total: number) => void,
): Promise<JobMetadata> {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('privacy_mode', String(privacyMode));
  if (format) formData.append('format', format);
  if (options) formData.append('options', JSON.stringify(options));

  return xhrFormUpload(`${API_BASE}/api/jobs/upload`, formData, {
    onProgress,
    defaultError: 'Failed to upload dataset.',
    statusErrorPrefix: 'Upload failed with HTTP status',
    networkError: 'Network connection failed or dropped during upload. Please check your network.',
    timeoutError: 'Upload connection timed out.',
  });
}

export async function getJobDetail(jobId: string): Promise<JobDetailResponse> {
  const res = await apiFetch(`${API_BASE}/api/jobs/${jobId}`);
  if (!res.ok) {
    const errorData = await res.json().catch(() => ({}));
    throw new ApiError(errorData.detail || 'Failed to fetch job detail.', res.status);
  }
  return res.json();
}

export async function getJobStatus(jobId: string): Promise<JobMetadata> {
  const res = await apiFetch(`${API_BASE}/api/jobs/${jobId}/status`);
  if (!res.ok) {
    const errorData = await res.json().catch(() => ({}));
    throw new ApiError(errorData.detail || 'Failed to fetch job status.', res.status);
  }
  return res.json();
}

export async function approveAndClean(jobId: string, recipe?: any): Promise<void> {
  const res = await apiFetch(`${API_BASE}/api/jobs/${jobId}/clean`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: recipe ? JSON.stringify(recipe) : undefined,
  });

  if (!res.ok) {
    throw new Error('Failed to initiate cleaning.');
  }
}

export function getDownloadUrl(jobId: string, format: string): string {
  const uid = getTenantUserId();
  return `${API_BASE}/api/jobs/${jobId}/download/${format}?user_id=${encodeURIComponent(uid)}`;
}
