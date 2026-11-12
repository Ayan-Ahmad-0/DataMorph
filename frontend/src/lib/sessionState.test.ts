// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from 'vitest';
import {
  buildHashState,
  clearPersistedSession,
  loadPersistedSession,
  parseHashState,
  savePersistedSession,
} from './sessionState';

describe('sessionState hash and storage helpers', () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
  });

  it('parses clean hash state including step and jobId', () => {
    expect(parseHashState('#clean?step=3&job=job_abc123')).toEqual({
      mode: 'clean',
      step: 3,
      jobId: 'job_abc123',
      targetName: null,
      queryMode: null,
    });
  });

  it('parses visualize and query hash states with dataset metadata', () => {
    expect(parseHashState('#visualize?job=job_99&name=sales.csv')).toEqual({
      mode: 'visualize',
      step: 1,
      jobId: 'job_99',
      targetName: 'sales.csv',
      queryMode: null,
    });

    expect(parseHashState('#query?job=query_42&name=events.parquet&mode=sql')).toEqual({
      mode: 'query',
      step: 1,
      jobId: 'query_42',
      targetName: 'events.parquet',
      queryMode: 'sql',
    });
  });

  it('builds canonical hash strings across modes', () => {
    expect(buildHashState({ mode: 'clean', step: 1, jobId: null })).toBe('#clean');
    expect(buildHashState({ mode: 'clean', step: 3, jobId: 'job_abc' })).toBe(
      '#clean?step=3&job=job_abc'
    );
    expect(
      buildHashState({
        mode: 'visualize',
        step: 1,
        visualizeTarget: {
          query_id: 'job_abc',
          filename: 'users.csv',
          row_count: 100,
          file_size_bytes: 2048,
          columns: [],
        },
      })
    ).toBe('#visualize?job=job_abc&name=users.csv');
  });

  it('saves, loads, and clears per-tenant sessionStorage state', () => {
    savePersistedSession({
      appMode: 'clean',
      currentStep: 5,
      jobId: 'job_saved1',
      fileMeta: {
        name: 'data.csv',
        sizeFormatted: '1.2 KB',
        rowCountFormatted: '42',
      },
      visualizeTarget: null,
      visualizeReturnMode: null,
      queryTarget: null,
      queryInitialMode: 'ask_ai',
    });

    const loaded = loadPersistedSession();
    expect(loaded?.jobId).toBe('job_saved1');
    expect(loaded?.currentStep).toBe(5);

    clearPersistedSession();
    expect(loadPersistedSession()).toBeNull();
  });
});

