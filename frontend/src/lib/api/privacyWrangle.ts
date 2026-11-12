import { API_BASE, apiFetch, parseJsonOrThrow } from './client';

export interface PiiFinding {
  column_name: string;
  entity_type: 'email' | 'credit_card' | 'ssn' | 'phone' | 'ip' | string;
  confidence: number;
  sample_count: number;
  match_count: number;
  sample_preview_masked: string;
  suggested_strategy: 'partial_mask' | 'sha256_hash' | 'redact';
  details?: Record<string, any>;
}

export interface PiiDetectResponse {
  job_id: string;
  detected_count: number;
  findings: PiiFinding[];
}

export interface WrangleColumn {
  name: string;
  type: string;
}

export interface WrangleHistoryItem {
  step: number;
  action_type: string;
  column: string;
  description: string;
  params: Record<string, any>;
  timestamp: string;
}

export interface WrangleResultResponse {
  success: boolean;
  message: string;
  row_count: number;
  columns: WrangleColumn[];
  preview_rows: any[][];
  history: WrangleHistoryItem[];
  can_undo: boolean;
}

export interface WrangleHistoryResponse {
  job_id: string;
  history: WrangleHistoryItem[];
  can_undo: boolean;
}

export async function detectJobPii(jobId: string): Promise<PiiDetectResponse> {
  const res = await apiFetch(`${API_BASE}/api/jobs/${jobId}/pii/detect`);
  return parseJsonOrThrow(res, 'Failed to detect PII.');
}

export async function applyJobPiiMasking(
  jobId: string,
  columns: Record<string, { strategy: string; entity_type?: string }>,
): Promise<{ success: boolean; message: string; row_count: number; columns: WrangleColumn[]; preview_rows: any[][] }> {
  const res = await apiFetch(`${API_BASE}/api/jobs/${jobId}/pii/apply`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ columns }),
  });
  return parseJsonOrThrow(res, 'Failed to apply PII masking.');
}

export async function applyWrangleAction(
  jobId: string,
  action: string,
  column: string,
  params?: Record<string, any>,
): Promise<WrangleResultResponse> {
  const res = await apiFetch(`${API_BASE}/api/jobs/${jobId}/wrangle`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, column, params: params || {} }),
  });
  return parseJsonOrThrow(res, 'Failed to apply wrangle action.');
}

export async function undoWrangleAction(jobId: string): Promise<WrangleResultResponse> {
  const res = await apiFetch(`${API_BASE}/api/jobs/${jobId}/wrangle/undo`, {
    method: 'POST',
  });
  return parseJsonOrThrow(res, 'Failed to undo wrangle action.');
}

export async function resetWrangleActions(jobId: string): Promise<WrangleResultResponse> {
  const res = await apiFetch(`${API_BASE}/api/jobs/${jobId}/wrangle/reset`, {
    method: 'POST',
  });
  return parseJsonOrThrow(res, 'Failed to reset wrangle actions.');
}

export async function getWrangleHistory(jobId: string): Promise<WrangleHistoryResponse> {
  const res = await apiFetch(`${API_BASE}/api/jobs/${jobId}/wrangle/history`);
  return parseJsonOrThrow(res, 'Failed to fetch wrangle history.');
}
