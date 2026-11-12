import { getTenantUserId, type QueryUploadResult } from './api';

export type AppMode = 'clean' | 'convert' | 'query' | 'visualize' | 'model';

export interface ParsedHashState {
  mode: AppMode;
  step: number;
  jobId: string | null;
  targetName: string | null;
  queryMode: 'sql' | 'ask_ai' | null;
}

export interface PersistedAppSession {
  appMode: AppMode;
  currentStep: number;
  jobId: string | null;
  fileMeta: {
    name: string;
    sizeFormatted: string;
    rowCountFormatted: string;
  };
  visualizeTarget: QueryUploadResult | null;
  visualizeReturnMode: 'clean' | 'convert' | null;
  queryTarget: QueryUploadResult | null;
  queryInitialSql?: string;
  queryInitialMode: 'sql' | 'ask_ai';
}

const VALID_MODES: AppMode[] = ['clean', 'convert', 'query', 'visualize', 'model'];

export function getSessionStorageKey(): string {
  return `datamorph_active_session_${getTenantUserId()}`;
}

export function parseHashState(rawHash?: string): ParsedHashState {
  const hashInput =
    typeof rawHash === 'string'
      ? rawHash
      : typeof window !== 'undefined'
        ? window.location.hash
        : '';

  const hash = hashInput.replace(/^#/, '');
  if (!hash) {
    return { mode: 'clean', step: 1, jobId: null, targetName: null, queryMode: null };
  }

  const [modePart, queryPart] = hash.split('?');
  const mode = VALID_MODES.includes(modePart as AppMode) ? (modePart as AppMode) : 'clean';

  let step = 1;
  let jobId: string | null = null;
  let targetName: string | null = null;
  let queryMode: 'sql' | 'ask_ai' | null = null;

  if (queryPart) {
    const params = new URLSearchParams(queryPart);
    const parsedStep = parseInt(params.get('step') || '1', 10);
    if (!isNaN(parsedStep) && parsedStep >= 1 && parsedStep <= 5) {
      step = parsedStep;
    }
    const rawJob = params.get('job');
    if (rawJob && rawJob.trim()) {
      jobId = rawJob.trim();
    }
    const rawName = params.get('name');
    if (rawName && rawName.trim()) {
      targetName = rawName.trim();
    }
    const rawQueryMode = params.get('mode');
    if (rawQueryMode === 'sql' || rawQueryMode === 'ask_ai') {
      queryMode = rawQueryMode;
    }
  }

  return { mode, step, jobId, targetName, queryMode };
}

export function buildHashState(options: {
  mode: AppMode;
  step: number;
  jobId?: string | null;
  visualizeTarget?: QueryUploadResult | null;
  queryTarget?: QueryUploadResult | null;
  queryInitialMode?: 'sql' | 'ask_ai';
}): string {
  const { mode, step, jobId, visualizeTarget, queryTarget, queryInitialMode } = options;
  const params = new URLSearchParams();

  if (mode === 'clean') {
    if (step > 1 || jobId) {
      params.set('step', String(step));
    }
    if (jobId) {
      params.set('job', jobId);
    }
  } else if (mode === 'visualize' && visualizeTarget?.query_id) {
    params.set('job', visualizeTarget.query_id);
    if (visualizeTarget.filename) {
      params.set('name', visualizeTarget.filename);
    }
  } else if (mode === 'query' && queryTarget?.query_id) {
    params.set('job', queryTarget.query_id);
    if (queryTarget.filename) {
      params.set('name', queryTarget.filename);
    }
    if (queryInitialMode) {
      params.set('mode', queryInitialMode);
    }
  }

  const query = params.toString();
  return `#${mode}${query ? `?${query}` : ''}`;
}

export function savePersistedSession(session: PersistedAppSession): void {
  if (typeof window === 'undefined') return;
  try {
    window.sessionStorage.setItem(getSessionStorageKey(), JSON.stringify(session));
  } catch {
    // Ignore storage quota or private-mode restrictions
  }
}

export function loadPersistedSession(): PersistedAppSession | null {
  if (typeof window === 'undefined') return null;
  try {
    const raw = window.sessionStorage.getItem(getSessionStorageKey());
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return null;
    if (!VALID_MODES.includes(parsed.appMode)) return null;
    return parsed as PersistedAppSession;
  } catch {
    return null;
  }
}

export function clearPersistedSession(): void {
  if (typeof window === 'undefined') return;
  try {
    window.sessionStorage.removeItem(getSessionStorageKey());
  } catch {
    // Ignore
  }
}

