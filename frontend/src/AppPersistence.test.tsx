// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('react-plotly.js', () => ({
  default: () => null,
}));

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import App from './App';
import { ApiError } from './lib/api';
import * as api from './lib/api';

describe('App refresh and session persistence', () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
    window.location.hash = '';
    vi.restoreAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  it('restores an active cleaning job from URL hash across browser refresh', async () => {
    window.location.hash = '#clean?step=3&job=job_restore1';

    const getJobDetailSpy = vi.spyOn(api, 'getJobDetail').mockResolvedValue({
      job: {
        id: 'job_restore1',
        filename: 'customers.csv',
        status: 'AWAITING_APPROVAL',
        progress_pct: 100,
        current_stage: 'Awaiting user approval of cleaning plan',
        file_size_bytes: 2048,
        row_count: 50,
        privacy_mode: false,
      },
      profile: {
        file: { rows: 50, cols: 3 },
        columns: {},
      },
      report_markdown: '### Summary\nLooks good',
      recipe: { rules: [] },
      recipe_plain_english: ['Trim whitespace'],
      open_questions: [],
      download_urls: {},
    });

    render(<App />);

    await waitFor(() => {
      expect(getJobDetailSpy).toHaveBeenCalledWith('job_restore1');
    });

    await waitFor(() => {
      expect(screen.getAllByText('customers.csv').length).toBeGreaterThan(0);
    });
    expect(window.location.hash).toBe('#clean?step=3&job=job_restore1');
  });

  it('resets cleanly to Step 1 with a notice when restored job returns 404 expired', async () => {
    window.location.hash = '#clean?step=3&job=job_expired1';

    vi.spyOn(api, 'getJobDetail').mockRejectedValue(
      new ApiError('Job not found or expired.', 404)
    );

    render(<App />);

    await waitFor(() => {
      expect(
        screen.getByText(
          'Your previous cleaning session expired or is no longer available. Upload a file to start a new job.'
        )
      ).toBeTruthy();
      expect(window.location.hash).toBe('#clean');
    });
  });
});

