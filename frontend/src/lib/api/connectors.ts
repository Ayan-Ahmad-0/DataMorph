import { API_BASE, apiFetch, parseJsonOrThrow } from './client';

export interface ConnectorConfigField {
  name: string;
  label: string;
  type: string;
  required?: boolean;
  default?: any;
  placeholder?: string;
  options?: string[];
  secret?: boolean;
}

export interface ConnectorMetadata {
  id: string;
  display_name: string;
  description: string;
  category: string;
  supported_formats: string[];
  supports_import: boolean;
  supports_export: boolean;
  config_fields: ConnectorConfigField[];
}

export interface ConnectionTestResult {
  success: boolean;
  message: string;
  latency_ms?: number;
  details?: Record<string, any>;
}

export interface ConnectorSyncResult {
  success: boolean;
  rows_transferred: number;
  destination_uri: string;
  elapsed_seconds: number;
  message: string;
  details?: Record<string, any>;
}

export interface ConnectorImportResponse {
  job_id: string;
  filename: string;
  status: string;
  rows_imported: number;
  sync_result: ConnectorSyncResult;
}

export async function fetchConnectors(): Promise<ConnectorMetadata[]> {
  const res = await apiFetch(`${API_BASE}/api/connectors`);
  if (!res.ok) throw new Error('Failed to load connectors.');
  return res.json();
}

export async function testConnector(
  connectorType: string,
  config: Record<string, any>,
): Promise<ConnectionTestResult> {
  const res = await apiFetch(`${API_BASE}/api/connectors/test`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ connector_type: connectorType, config }),
  });
  return parseJsonOrThrow(res, 'Connection test failed.');
}

export async function importFromConnector(
  connectorType: string,
  config: Record<string, any>,
  filename?: string,
  privacyMode: boolean = false,
  options?: Record<string, any>,
): Promise<ConnectorImportResponse> {
  const res = await apiFetch(`${API_BASE}/api/connectors/import`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      connector_type: connectorType,
      config,
      filename,
      privacy_mode: privacyMode,
      options: options || {},
    }),
  });
  return parseJsonOrThrow(res, 'Connector import failed.');
}

export async function exportToConnector(
  jobId: string,
  connectorType: string,
  config: Record<string, any>,
  options?: Record<string, any>,
): Promise<ConnectorSyncResult> {
  const res = await apiFetch(`${API_BASE}/api/connectors/export/${jobId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      connector_type: connectorType,
      config,
      options: options || {},
    }),
  });
  return parseJsonOrThrow(res, 'Export to connector failed.');
}
