import { API_BASE, apiFetch, parseJsonOrThrow, triggerBlobDownload, xhrFormUpload } from './client';

export interface ExploreColumnInfo {
  name: string;
  type: string;
  sample_values?: any[];
}

export interface ExploreSchemaResponse {
  table: string;
  row_count: number;
  columns: ExploreColumnInfo[];
  tables_available: string[];
}

export interface ExploreQueryResult {
  columns: { name: string; type: string }[];
  rows: any[][];
  row_count_returned: number;
  truncated: boolean;
  elapsed_seconds: number;
  warnings: string[];
}

export interface AnalystAskResult {
  status: 'ok' | 'needs_clarification' | 'cannot_answer' | 'error';
  question: string;
  sql?: string | null;
  explanation?: string | null;
  assumptions: string[];
  clarification_question?: string | null;
  result?: ExploreQueryResult | null;
  retried: boolean;
  message?: string | null;
  elapsed_seconds: number;
}

export interface QueryUploadResult {
  query_id: string;
  filename: string;
  row_count: number;
  columns: string[];
  file_size_bytes: number;
  detected_format?: string;
}

export async function getExploreSchema(jobId: string, table?: string): Promise<ExploreSchemaResponse> {
  const url = table
    ? `${API_BASE}/api/explore/${jobId}/schema?table=${encodeURIComponent(table)}`
    : `${API_BASE}/api/explore/${jobId}/schema`;
  const res = await apiFetch(url);
  return parseJsonOrThrow(res, 'Failed to load dataset schema.');
}

export async function executeExploreQuery(
  jobId: string,
  sql: string,
  rowLimit?: number,
): Promise<ExploreQueryResult> {
  const res = await apiFetch(`${API_BASE}/api/explore/${jobId}/query`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sql, row_limit: rowLimit }),
  });
  return parseJsonOrThrow(res, 'Query execution failed.');
}

export async function exportExploreQuery(jobId: string, sql: string): Promise<void> {
  const res = await apiFetch(`${API_BASE}/api/explore/${jobId}/export`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sql }),
  });
  return triggerBlobDownload(res, `query_export_${jobId.slice(0, 8)}.csv`, 'Export failed.');
}

export async function askAiAnalyst(
  jobId: string,
  question: string,
  userId: string = 'anonymous',
  signal?: AbortSignal,
): Promise<AnalystAskResult> {
  const res = await apiFetch(`${API_BASE}/api/explore/${jobId}/ask`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, user_id: userId }),
    signal,
  });
  return parseJsonOrThrow(res, 'Ask AI request failed.');
}

export function uploadForQuery(
  file: File,
  options?: Record<string, any>,
  onProgress?: (loaded: number, total: number) => void,
): Promise<QueryUploadResult> {
  const formData = new FormData();
  formData.append('file', file);
  if (options) {
    formData.append('options', JSON.stringify(options));
  }

  return xhrFormUpload(`${API_BASE}/api/query/upload`, formData, {
    onProgress,
    defaultError: 'Failed to upload dataset for querying.',
    statusErrorPrefix: 'Upload failed with HTTP status',
    networkError: 'Network error during upload. Ensure the backend server is running.',
    abortError: 'Upload was aborted.',
  });
}
