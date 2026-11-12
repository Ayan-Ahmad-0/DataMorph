import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react';
import { Navbar } from './components/Navbar';
import { UploadPage } from './pages/UploadPage';
import {
  ApiError,
  approveAndClean,
  getJobDetail,
  getJobStatus,
  type JobDetailResponse,
  type QueryUploadResult,
  type QueryHandoffResult,
} from './lib/api';

const ReportPage = lazy(() => import('./pages/ReportPage').then((m) => ({ default: m.ReportPage })));
const ApprovalPage = lazy(() => import('./pages/ApprovalPage').then((m) => ({ default: m.ApprovalPage })));
const ProgressPage = lazy(() => import('./pages/ProgressPage').then((m) => ({ default: m.ProgressPage })));
const ResultsPage = lazy(() => import('./pages/ResultsPage').then((m) => ({ default: m.ResultsPage })));
const ConvertPage = lazy(() => import('./pages/ConvertPage').then((m) => ({ default: m.ConvertPage })));
const QueryPage = lazy(() => import('./pages/QueryPage').then((m) => ({ default: m.QueryPage })));
const VisualizePage = lazy(() => import('./pages/VisualizePage').then((m) => ({ default: m.VisualizePage })));
const ModelPage = lazy(() => import('./pages/ModelPage').then((m) => ({ default: m.ModelPage })));
import { formatBytes, formatNumber } from './lib/format';
import {
  buildHashState,
  clearPersistedSession,
  loadPersistedSession,
  parseHashState,
  savePersistedSession,
  type AppMode,
} from './lib/sessionState';

function resolveInitialState() {
  const hasExplicitHash = typeof window !== 'undefined' && window.location.hash.length > 1;
  const parsed = parseHashState();
  const stored = loadPersistedSession();

  if (hasExplicitHash) {
    const cleanJobId =
      parsed.mode === 'clean'
        ? (parsed.jobId ?? (stored?.appMode === 'clean' ? stored.jobId : null))
        : (stored?.jobId ?? null);

    const safeStep =
      parsed.mode === 'clean' && parsed.step > 1 && !cleanJobId ? 1 : parsed.step;

    let visTarget = stored?.visualizeTarget ?? null;
    if (parsed.mode === 'visualize' && parsed.jobId) {
      if (!visTarget || visTarget.query_id !== parsed.jobId) {
        visTarget = {
          query_id: parsed.jobId,
          filename: parsed.targetName || 'Dataset',
          row_count: 0,
          file_size_bytes: 0,
          columns: [],
        };
      }
    }

    let qTarget = stored?.queryTarget ?? null;
    if (parsed.mode === 'query' && parsed.jobId) {
      if (!qTarget || qTarget.query_id !== parsed.jobId) {
        qTarget = {
          query_id: parsed.jobId,
          filename: parsed.targetName || 'Dataset',
          row_count: 0,
          file_size_bytes: 0,
          columns: [],
          detected_format: 'PARQUET',
        };
      }
    }

    return {
      appMode: parsed.mode,
      currentStep: safeStep,
      jobId: cleanJobId,
      fileMeta:
        cleanJobId && stored?.fileMeta
          ? stored.fileMeta
          : { name: '', sizeFormatted: '', rowCountFormatted: '' },
      visualizeTarget: visTarget,
      visualizeReturnMode: stored?.visualizeReturnMode ?? null,
      queryTarget: qTarget,
      queryInitialSql: stored?.queryInitialSql,
      queryInitialMode: parsed.queryMode ?? stored?.queryInitialMode ?? ('ask_ai' as const),
    };
  }

  if (stored) {
    const safeStep =
      stored.appMode === 'clean' && stored.currentStep > 1 && !stored.jobId
        ? 1
        : stored.currentStep;
    return {
      appMode: stored.appMode,
      currentStep: safeStep,
      jobId: stored.jobId,
      fileMeta: stored.fileMeta || { name: '', sizeFormatted: '', rowCountFormatted: '' },
      visualizeTarget: stored.visualizeTarget ?? null,
      visualizeReturnMode: stored.visualizeReturnMode ?? null,
      queryTarget: stored.queryTarget ?? null,
      queryInitialSql: stored.queryInitialSql,
      queryInitialMode: stored.queryInitialMode || ('ask_ai' as const),
    };
  }

  return {
    appMode: 'clean' as AppMode,
    currentStep: 1,
    jobId: null,
    fileMeta: { name: '', sizeFormatted: '', rowCountFormatted: '' },
    visualizeTarget: null,
    visualizeReturnMode: null,
    queryTarget: null,
    queryInitialSql: undefined,
    queryInitialMode: 'ask_ai' as const,
  };
}

export function App() {
  const initialState = useMemo(() => resolveInitialState(), []);
  const [appMode, setAppMode] = useState<AppMode>(initialState.appMode);
  const [currentStep, setCurrentStep] = useState<number>(initialState.currentStep);
  const isNavigatingFromPopstate = useRef(false);
  const hasInitializedHistory = useRef(false);

  const [jobId, setJobId] = useState<string | null>(initialState.jobId);
  const [jobData, setJobData] = useState<JobDetailResponse | null>(null);
  const [sessionNotice, setSessionNotice] = useState<string | null>(null);
  const [visualizeTarget, setVisualizeTarget] = useState<QueryUploadResult | null>(initialState.visualizeTarget);
  const [visualizeReturnMode, setVisualizeReturnMode] = useState<'clean' | 'convert' | null>(initialState.visualizeReturnMode);
  const [queryTarget, setQueryTarget] = useState<QueryUploadResult | null>(initialState.queryTarget);
  const [queryInitialSql, setQueryInitialSql] = useState<string | undefined>(initialState.queryInitialSql);
  const [queryInitialMode, setQueryInitialMode] = useState<'sql' | 'ask_ai'>(initialState.queryInitialMode);

  const [fileMeta, setFileMeta] = useState<{
    name: string;
    sizeFormatted: string;
    rowCountFormatted: string;
  }>(initialState.fileMeta);

  // Synchronize navigation & active session state with URL hash, history, and sessionStorage
  useEffect(() => {
    const nextHash = buildHashState({
      mode: appMode,
      step: currentStep,
      jobId,
      visualizeTarget,
      queryTarget,
      queryInitialMode,
    });

    if (appMode === 'clean' && currentStep === 1 && !jobId) {
      clearPersistedSession();
    } else {
      savePersistedSession({
        appMode,
        currentStep,
        jobId,
        fileMeta,
        visualizeTarget,
        visualizeReturnMode,
        queryTarget,
        queryInitialSql,
        queryInitialMode,
      });
    }

    if (!hasInitializedHistory.current) {
      hasInitializedHistory.current = true;
      window.history.replaceState({ appMode, currentStep, jobId }, '', nextHash);
      return;
    }

    if (isNavigatingFromPopstate.current) {
      isNavigatingFromPopstate.current = false;
      return;
    }

    if (window.location.hash !== nextHash) {
      window.history.pushState({ appMode, currentStep, jobId }, '', nextHash);
    }
  }, [
    appMode,
    currentStep,
    jobId,
    fileMeta,
    visualizeTarget,
    visualizeReturnMode,
    queryTarget,
    queryInitialSql,
    queryInitialMode,
  ]);

  // Handle browser back and forward button clicks (popstate)
  useEffect(() => {
    const handlePopState = (event: PopStateEvent) => {
      isNavigatingFromPopstate.current = true;
      if (event.state && event.state.appMode) {
        setAppMode(event.state.appMode);
        if (event.state.currentStep) {
          setCurrentStep(event.state.currentStep);
        }
        if (event.state.jobId !== undefined) {
          setJobId(event.state.jobId);
        }
      } else {
        const parsed = parseHashState();
        setAppMode(parsed.mode);
        setCurrentStep(parsed.mode === 'clean' && parsed.step > 1 && !parsed.jobId ? 1 : parsed.step);
        if (parsed.mode === 'clean') {
          setJobId(parsed.jobId);
        }
      }
    };

    window.addEventListener('popstate', handlePopState);
    return () => window.removeEventListener('popstate', handlePopState);
  }, []);

  // Hydrate & adaptively poll job status/detail from FastAPI backend
  useEffect(() => {
    if (!jobId) return;

    let cancelled = false;
    let timeoutId: ReturnType<typeof setTimeout> | null = null;
    let lastKnownStatus: string | undefined =
      jobData?.job?.id === jobId ? jobData.job.status : undefined;
    let awaitingChecks = 0;

    const terminalStatuses = new Set(['DONE', 'DONE_WITH_WARNINGS', 'FAILED', 'CANCELLED']);
    const maxAwaitingChecks = 6;

    const applyJobStatusRouting = (status: string) => {
      if (status === 'AWAITING_APPROVAL' && currentStep === 1) {
        setCurrentStep(2);
      } else if ((status === 'DONE' || status === 'DONE_WITH_WARNINGS') && currentStep === 4) {
        setCurrentStep(5);
      } else if (status === 'AWAITING_APPROVAL' && (currentStep === 4 || currentStep === 5)) {
        // If server restart recovered an interrupted cleaning run back to AWAITING_APPROVAL, return to Approval
        setCurrentStep(3);
      } else if (status === 'FAILED' && currentStep > 1 && currentStep !== 4) {
        setCurrentStep(4);
      }
    };

    const handlePollError = (err: unknown) => {
      if (cancelled) return;
      if (err instanceof ApiError && (err.status === 404 || err.status === 410)) {
        if (timeoutId) {
          clearTimeout(timeoutId);
          timeoutId = null;
        }
        isNavigatingFromPopstate.current = false;
        clearPersistedSession();
        setJobId(null);
        setJobData(null);
        setFileMeta({ name: '', sizeFormatted: '', rowCountFormatted: '' });
        setCurrentStep(1);
        setSessionNotice(
          'Your previous cleaning session expired or is no longer available. Upload a file to start a new job.'
        );
        return;
      }
      console.error('Error polling job status:', err);
    };

    const scheduleNext = (status: string | undefined) => {
      if (cancelled) return;
      if (status && terminalStatuses.has(status)) return;
      if (status === 'AWAITING_APPROVAL') {
        if (awaitingChecks >= maxAwaitingChecks) return;
        awaitingChecks += 1;
        timeoutId = setTimeout(pollTick, 2500);
        return;
      }
      timeoutId = setTimeout(pollTick, 750);
    };

    const fetchFullDetail = async (): Promise<string | undefined> => {
      const detail = await getJobDetail(jobId);
      if (cancelled) return undefined;

      setJobData(detail);
      setSessionNotice(null);

      setFileMeta((prev) => ({
        name: detail.job.filename || prev.name,
        sizeFormatted:
          detail.job.file_size_bytes > 0
            ? formatBytes(detail.job.file_size_bytes)
            : prev.sizeFormatted,
        rowCountFormatted: detail.profile?.file?.rows
          ? formatNumber(detail.profile.file.rows)
          : detail.job.row_count > 0
            ? formatNumber(detail.job.row_count)
            : prev.rowCountFormatted,
      }));

      lastKnownStatus = detail.job.status;
      applyJobStatusRouting(detail.job.status);
      return detail.job.status;
    };

    const pollTick = async () => {
      try {
        // Use lightweight /status during active pipeline ticks, then fetch full /jobs/{id} on transition
        if (lastKnownStatus && typeof getJobStatus === 'function' && lastKnownStatus !== 'AWAITING_APPROVAL') {
          const snap = await getJobStatus(jobId);
          if (cancelled) return;

          setJobData((prev) => (prev ? { ...prev, job: snap } : prev));
          setSessionNotice(null);

          const transitioned =
            snap.status !== lastKnownStatus ||
            terminalStatuses.has(snap.status) ||
            snap.status === 'AWAITING_APPROVAL';
          lastKnownStatus = snap.status;

          if (transitioned) {
            const fullStatus = await fetchFullDetail();
            scheduleNext(fullStatus);
            return;
          }

          applyJobStatusRouting(snap.status);
          scheduleNext(snap.status);
          return;
        }

        const status = await fetchFullDetail();
        scheduleNext(status);
      } catch (err) {
        handlePollError(err);
      }
    };

    // Hydrate immediately on mount/jobId change instead of waiting for the first timer tick
    fetchFullDetail()
      .then((status) => scheduleNext(status))
      .catch(handlePollError);

    return () => {
      cancelled = true;
      if (timeoutId) clearTimeout(timeoutId);
    };
  }, [jobId, currentStep]);

  const handleUploadComplete = (info: { id: string; name: string; size: string; rowCount: string }) => {
    setSessionNotice(null);
    setJobData(null);
    setJobId(info.id);
    setFileMeta({
      name: info.name,
      sizeFormatted: info.size,
      rowCountFormatted: info.rowCount,
    });
    // Automatically transition to Step 2 (Profile & Report) upon upload completion
    setCurrentStep(2);

    // Immediately fetch initial job details
    getJobDetail(info.id).then((detail) => {
      setJobData(detail);
    }).catch(console.error);
  };

  const handleStartCleaning = async (customRecipe?: any) => {
    if (!jobId) return;
    try {
      await approveAndClean(jobId, customRecipe);
      setCurrentStep(4);
    } catch (err) {
      console.error('Failed to trigger cleaning:', err);
    }
  };

  const handleRestart = () => {
    clearPersistedSession();
    setSessionNotice(null);
    setJobId(null);
    setJobData(null);
    setFileMeta({ name: '', sizeFormatted: '', rowCountFormatted: '' });
    setCurrentStep(1);
  };

  const handleHandoffToClean = (cleanJobId: string, filename: string) => {
    setJobId(cleanJobId);
    setFileMeta({
      name: filename,
      sizeFormatted: '',
      rowCountFormatted: '',
    });
    setAppMode('clean');
    setCurrentStep(2);
    getJobDetail(cleanJobId).then((detail) => {
      setJobData(detail);
    }).catch(console.error);
  };

  const handleHandoffToVisualize = (
    targetJobId: string,
    filename: string,
    rowCount?: number,
    fileSize?: number
  ) => {
    setVisualizeTarget({
      query_id: targetJobId,
      filename,
      row_count: rowCount || 0,
      file_size_bytes: fileSize || 0,
      columns: [],
    });
    setVisualizeReturnMode(appMode === 'convert' ? 'convert' : 'clean');
    setAppMode('visualize');
  };

  const handleHandoffToQuery = (
    targetJobId: string,
    filename: string,
    handoff?: QueryHandoffResult,
    initialMode: 'sql' | 'ask_ai' = 'sql'
  ) => {
    setQueryTarget({
      query_id: targetJobId,
      filename: filename,
      row_count: handoff?.row_count || 0,
      columns: handoff?.columns || [],
      file_size_bytes: handoff?.file_size_bytes || 0,
      detected_format: handoff?.detected_format || 'PARQUET',
    });
    setQueryInitialSql(handoff?.sql || 'SELECT * FROM data LIMIT 100;');
    setQueryInitialMode(initialMode);
    setAppMode('query');
  };

  // Determine max step the user is allowed to navigate to
  const maxAllowedStep = useMemo(() => {
    if (!jobId) return 1;
    const status = jobData?.job?.status;
    if (status === 'DONE' || status === 'DONE_WITH_WARNINGS') return 5;
    if (status === 'CLEANING' || status === 'VERIFYING' || status === 'FAILED') return 4;
    // Before cleaning is approved/started, user cannot go past Step 3 (Approval)
    return 3;
  }, [jobId, jobData?.job?.status]);

  const handleSelectStep = (step: number) => {
    if (step <= maxAllowedStep) {
      setCurrentStep(step);
    }
  };

  return (
    <div className="min-h-screen flex flex-col bg-[var(--bg-app)] text-[var(--text-primary)] font-sans antialiased">
      <Navbar
        appMode={appMode}
        onSelectMode={(mode) => setAppMode(mode)}
        currentStep={currentStep}
        maxAllowedStep={maxAllowedStep}
        onSelectStep={handleSelectStep}
        fileName={fileMeta.name}
        fileSizeFormatted={fileMeta.sizeFormatted}
        rowCountFormatted={fileMeta.rowCountFormatted}
      />

      <main className="flex-1 w-full max-w-full overflow-x-hidden flex flex-col">
        {sessionNotice && (
          <div
            role="status"
            className="mx-auto mt-3 w-full max-w-3xl px-4"
          >
            <div className="flex items-center justify-between gap-3 rounded-[var(--radius-md)] border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-4 py-2.5 text-sm text-[var(--text-secondary)] shadow-sm">
              <span>{sessionNotice}</span>
              <button
                type="button"
                onClick={() => setSessionNotice(null)}
                className="text-xs font-medium text-[var(--text-muted)] hover:text-[var(--text-primary)] cursor-pointer shrink-0"
              >
                Dismiss
              </button>
            </div>
          </div>
        )}

        {/* Centered Mode Switcher (Clean Data vs Convert Only vs Query Data vs Visualize Data) - responsive with smooth horizontal touch scroll */}
        {currentStep === 1 && (
          <div className="pt-3 sm:pt-5 pb-2 sm:pb-5 w-full max-w-full px-2 sm:px-4 flex justify-start sm:justify-center items-center overflow-x-auto scrollbar-none touch-pan-x">
            <div className="inline-flex items-center bg-[var(--bg-subtle)] p-1 rounded-[var(--radius-lg)] border border-[var(--border-hairline)] shadow-[var(--shadow-card)] shrink-0 sm:mx-auto w-max">
              {([
                { key: 'clean' as const, label: 'Clean Data' },
                { key: 'convert' as const, label: 'Convert Only' },
                { key: 'query' as const, label: 'Query Data' },
                { key: 'visualize' as const, label: 'Visualize Data' },
                { key: 'model' as const, label: 'Model Data (ERD)' },
              ]).map((tab) => (
                <button
                  key={tab.key}
                  type="button"
                  onClick={() => setAppMode(tab.key)}
                  className={`relative px-2.5 sm:px-5 py-1.5 sm:py-2 text-xs sm:text-sm font-medium rounded-[var(--radius-md)] transition-all duration-200 cursor-pointer select-none whitespace-nowrap ${
                    appMode === tab.key
                      ? 'bg-[var(--bg-surface)] text-[var(--text-primary)] shadow-sm border-t-2 border-t-[var(--accent)] border-x border-b border-x-[var(--border-hairline)] border-b-[var(--border-hairline)]'
                      : 'text-[var(--text-muted)] hover:text-[var(--text-secondary)] hover:bg-[var(--bg-hover)] border-t-2 border-transparent'
                  }`}
                >
                  {tab.label}
                </button>
              ))}
            </div>
          </div>
        )}

        <Suspense
          fallback={
            <div className="flex-1 flex items-center justify-center p-8 text-xs text-[var(--text-muted)]">
              Loading workspace...
            </div>
          }
        >
        {appMode === 'model' ? (
          <ModelPage onHandoffToQuery={handleHandoffToQuery} />
        ) : appMode === 'convert' ? (
          <ConvertPage
            onHandoffToClean={handleHandoffToClean}
            onHandoffToVisualize={handleHandoffToVisualize}
          />
        ) : appMode === 'query' ? (
          <QueryPage
            initialDataset={queryTarget}
            initialSql={queryInitialSql}
            initialMode={queryInitialMode}
            onHandoffToClean={handleHandoffToClean}
          />
        ) : appMode === 'visualize' ? (
          <VisualizePage
            initialDataset={visualizeTarget}
            onHandoffToQuery={(id, name) => handleHandoffToQuery(id, name)}
            onHandoffToClean={handleHandoffToClean}
            onBackToSource={visualizeReturnMode ? () => setAppMode(visualizeReturnMode) : undefined}
          />
        ) : (
          <>
            {currentStep === 1 && (
              <UploadPage onUploadComplete={handleUploadComplete} />
            )}
            {currentStep > 1 && jobId && !jobData ? (
              <div className="flex-1 flex items-center justify-center p-8">
                <div className="max-w-md w-full rounded-[var(--radius-lg)] border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-6 text-center shadow-[var(--shadow-card)]">
                  <div className="mx-auto mb-3 h-6 w-6 animate-spin rounded-full border-2 border-[var(--accent)] border-t-transparent" />
                  <p className="text-sm font-medium text-[var(--text-primary)]">
                    Restoring your saved session...
                  </p>
                  <p className="mt-1 text-xs text-[var(--text-muted)]">
                    Loading job {jobId}
                  </p>
                </div>
              </div>
            ) : (
              <>
                {currentStep === 2 && (
                  <ReportPage
                    jobData={jobData}
                    onProceedToApproval={() => setCurrentStep(3)}
                    onGoToUpload={() => setCurrentStep(1)}
                  />
                )}
                {currentStep === 3 && (
                  <ApprovalPage
                    jobData={jobData}
                    onStartCleaning={handleStartCleaning}
                    onGoToUpload={() => setCurrentStep(1)}
                  />
                )}
                {currentStep === 4 && (
                  <ProgressPage
                    jobData={jobData}
                    onProceedToResults={() => setCurrentStep(5)}
                    onGoToApproval={() => setCurrentStep(3)}
                    onRetry={() => handleStartCleaning(jobData?.recipe)}
                    onCancel={handleRestart}
                    onGoToUpload={() => setCurrentStep(1)}
                  />
                )}
                {currentStep === 5 && (
                  <ResultsPage
                    jobData={jobData}
                    onRestart={handleRestart}
                    onGoToUpload={() => setCurrentStep(1)}
                    onHandoffToVisualize={handleHandoffToVisualize}
                  />
                )}
              </>
            )}
          </>
        )}
        </Suspense>
      </main>
    </div>
  );
}

export default App;
