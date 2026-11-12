import { API_BASE, apiFetch, getTenantUserId, parseJsonOrThrow, triggerBlobDownload } from './client';

export interface ColumnMetadata {
  name: string;
  data_type: string;
  is_nullable: boolean;
  is_pk: boolean;
  is_fk: boolean;
  distinct_count?: number;
  null_count?: number;
  sample_values?: any[];
}

export interface TableMetadata {
  table_name: string;
  row_count: number;
  file_size_bytes: number;
  original_filename: string;
  columns: ColumnMetadata[];
  position_x: number;
  position_y: number;
}

export interface Relationship {
  id: string;
  from_table: string;
  from_column: string;
  to_table: string;
  to_column: string;
  cardinality: string;
  confidence: number;
  match_ratio: number;
  orphan_count: number;
  status: 'detected' | 'confirmed' | 'manual';
}

export interface ModelingSessionState {
  session_id: string;
  created_at: string;
  tables: Record<string, TableMetadata>;
  relationships: Relationship[];
}

export interface JoinPreviewResult {
  from_table: string;
  from_column: string;
  to_table: string;
  to_column: string;
  join_type: string;
  matched_rows: number;
  total_child_rows: number;
  match_ratio: number;
  orphan_count: number;
  columns: string[];
  sample_rows: Record<string, any>[];
}

export interface SchemaExportResult {
  format: string;
  content: string;
}

export interface QueryHandoffResult {
  query_id: string;
  filename: string;
  row_count: number;
  columns: string[];
  file_size_bytes: number;
  detected_format?: string;
  sql: string;
  tables: string[];
  relationships_count?: number;
}

export async function createModelingSession(): Promise<ModelingSessionState> {
  const res = await apiFetch(`${API_BASE}/api/modeling/sessions`, {
    method: 'POST',
  });
  return parseJsonOrThrow(res, 'Failed to create modeling session.');
}

export async function getModelingSession(sessionId: string): Promise<ModelingSessionState> {
  const res = await apiFetch(`${API_BASE}/api/modeling/sessions/${sessionId}`);
  return parseJsonOrThrow(res, 'Failed to load modeling session.');
}

export async function uploadModelingTable(
  sessionId: string,
  file: File,
  tableName?: string,
): Promise<ModelingSessionState> {
  const formData = new FormData();
  formData.append('file', file);
  if (tableName) {
    formData.append('table_name', tableName);
  }

  const res = await apiFetch(`${API_BASE}/api/modeling/sessions/${sessionId}/tables`, {
    method: 'POST',
    body: formData,
  });
  return parseJsonOrThrow(res, 'Failed to upload table to modeling session.');
}

export async function deleteModelingTable(
  sessionId: string,
  tableName: string,
): Promise<ModelingSessionState> {
  const res = await apiFetch(`${API_BASE}/api/modeling/sessions/${sessionId}/tables/${tableName}`, {
    method: 'DELETE',
  });
  return parseJsonOrThrow(res, 'Failed to remove table.');
}

export async function updateModelingTablePosition(
  sessionId: string,
  tableName: string,
  x: number,
  y: number,
): Promise<{ status: string }> {
  const res = await apiFetch(`${API_BASE}/api/modeling/sessions/${sessionId}/tables/${tableName}/position`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ x, y }),
  });
  if (!res.ok) {
    return { status: 'error' };
  }
  return res.json();
}

export async function detectModelingRelationships(sessionId: string): Promise<ModelingSessionState> {
  const res = await apiFetch(`${API_BASE}/api/modeling/sessions/${sessionId}/detect`, {
    method: 'POST',
  });
  return parseJsonOrThrow(res, 'Failed to detect relationships.');
}

export async function saveModelingRelationship(
  sessionId: string,
  relationship: Relationship,
): Promise<Relationship> {
  const res = await apiFetch(`${API_BASE}/api/modeling/sessions/${sessionId}/relationships`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(relationship),
  });
  return parseJsonOrThrow(res, 'Failed to save relationship.');
}

export async function deleteModelingRelationship(
  sessionId: string,
  relId: string,
): Promise<{ status: string }> {
  const res = await apiFetch(`${API_BASE}/api/modeling/sessions/${sessionId}/relationships/${relId}`, {
    method: 'DELETE',
  });
  return parseJsonOrThrow(res, 'Failed to delete relationship.');
}

export async function previewModelingJoin(
  sessionId: string,
  params: {
    from_table: string;
    from_column: string;
    to_table: string;
    to_column: string;
    join_type?: string;
  },
): Promise<JoinPreviewResult> {
  const res = await apiFetch(`${API_BASE}/api/modeling/sessions/${sessionId}/preview-join`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  });
  return parseJsonOrThrow(res, 'Failed to preview join.');
}

export async function exportModelingSchema(
  sessionId: string,
  format: 'mermaid' | 'dbml' | 'sql_ddl' = 'mermaid',
): Promise<SchemaExportResult> {
  const res = await apiFetch(`${API_BASE}/api/modeling/sessions/${sessionId}/export?format=${format}`);
  return parseJsonOrThrow(res, 'Failed to export schema.');
}

export async function handoffModelingToQuery(sessionId: string): Promise<QueryHandoffResult> {
  const res = await apiFetch(`${API_BASE}/api/modeling/sessions/${sessionId}/handoff-query`, {
    method: 'POST',
  });
  return parseJsonOrThrow(res, 'Failed to prepare query handoff.');
}

export async function downloadModelingMergedCsv(sessionId: string, relId?: string): Promise<void> {
  const uid = getTenantUserId();
  const url = relId
    ? `${API_BASE}/api/modeling/sessions/${sessionId}/export-csv?rel_id=${encodeURIComponent(relId)}&user_id=${encodeURIComponent(uid)}`
    : `${API_BASE}/api/modeling/sessions/${sessionId}/export-csv?user_id=${encodeURIComponent(uid)}`;
  const res = await apiFetch(url);
  const filename = relId ? `joined_${relId.slice(0, 16)}.csv` : `merged_${sessionId.slice(0, 12)}.csv`;
  return triggerBlobDownload(res, filename, 'Failed to download merged CSV.');
}
