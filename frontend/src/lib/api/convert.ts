import { API_BASE, apiFetch, getTenantUserId, parseJsonOrThrow, xhrFormUpload } from './client';
import type { JobMetadata } from './jobs';

export interface ConvertResultResponse {
  convert_id: string;
  result: {
    output_files: string[];
    output_filenames: string[];
    source_format: string;
    target_format: string;
    rows: number;
    columns: string[];
    warnings: string[];
    intermediate_parquet?: string;
    elapsed_seconds: number;
  };
}

export async function previewConvert(
  file: File,
  format?: string,
  options?: Record<string, any>,
): Promise<{ preview: any[]; report: any }> {
  const formData = new FormData();
  // For files larger than 5 MB, send a 2 MB sample so preview returns instantaneously
  const previewSlice = file.size > 5 * 1024 * 1024 ? file.slice(0, 2 * 1024 * 1024) : file;
  formData.append('file', previewSlice, file.name);
  if (format) formData.append('format', format);
  if (options) formData.append('options', JSON.stringify(options));

  const res = await apiFetch(`${API_BASE}/api/convert`, {
    method: 'POST',
    body: formData,
  });
  return parseJsonOrThrow(res, 'Preview failed.');
}

export function executeConvertOnly(
  file: File,
  targetFormat: string,
  sourceFormat?: string,
  sourceOptions?: Record<string, any>,
  targetOptions?: Record<string, any>,
  onProgress?: (loaded: number, total: number) => void,
): Promise<ConvertResultResponse> {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('target_format', targetFormat);
  if (sourceFormat) formData.append('source_format', sourceFormat);
  if (sourceOptions) formData.append('source_options', JSON.stringify(sourceOptions));
  if (targetOptions) formData.append('target_options', JSON.stringify(targetOptions));

  return xhrFormUpload(`${API_BASE}/api/convert/execute`, formData, {
    onProgress,
    defaultError: 'Conversion failed.',
    statusErrorPrefix: 'Conversion failed with HTTP status',
    networkError: 'Network connection dropped or failed during conversion transfer.',
  });
}

export function getConvertDownloadUrl(convertId: string): string {
  const uid = getTenantUserId();
  return `${API_BASE}/api/convert/download/${convertId}?user_id=${encodeURIComponent(uid)}`;
}

export async function handoffToClean(convertId: string): Promise<JobMetadata> {
  const res = await apiFetch(`${API_BASE}/api/convert/${convertId}/handoff-to-clean`, {
    method: 'POST',
  });
  return parseJsonOrThrow(res, 'Handoff to cleaning failed.');
}
