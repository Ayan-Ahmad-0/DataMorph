import React, { useMemo, useState } from 'react';
import type { JobDetailResponse } from '../lib/api';
import { Button } from '../components/ui/Button';
import { Disclosure } from '../components/ui/Disclosure';
import { MarkdownView } from '../components/ui/MarkdownView';
import { SummaryStrip, type SummaryMetric } from '../components/ui/SummaryStrip';
import { Tag } from '../components/ui/Tag';
import { deriveColumnStatus, formatNumber, formatPercent, pluralize } from '../lib/format';
import {
  AlertCircle,
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Filter,
  Loader2,
  Search,
} from 'lucide-react';

interface ReportPageProps {
  jobData: JobDetailResponse | null;
  onProceedToApproval: () => void;
  onGoToUpload?: () => void;
}

export const ReportPage: React.FC<ReportPageProps> = ({ jobData, onProceedToApproval, onGoToUpload }) => {
  const profile = jobData?.profile;

  // Table state for 40+ columns
  const [filterIssuesOnly, setFilterIssuesOnly] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [expandedColumns, setExpandedColumns] = useState<Record<string, boolean>>({});

  const toggleExpand = (colName: string) => {
    setExpandedColumns((prev) => ({ ...prev, [colName]: !prev[colName] }));
  };

  // Derive findings across all columns
  const columnAnalysis = useMemo(() => {
    const cols: Record<string, any> = profile?.columns || {};
    const list = Object.entries(cols).map(([name, meta]) => {
      const status = deriveColumnStatus(meta);
      return { name, meta, status };
    });

    const issuesCount = list.filter((c) => !c.status.isClean).length;
    return { list, issuesCount };
  }, [profile]);

  // Filtered column list
  const filteredColumns = useMemo(() => {
    return columnAnalysis.list.filter(({ name, status }) => {
      if (filterIssuesOnly && status.isClean) return false;
      if (searchQuery.trim()) {
        return name.toLowerCase().includes(searchQuery.toLowerCase().trim());
      }
      return true;
    });
  }, [columnAnalysis.list, filterIssuesOnly, searchQuery]);

  // Editorial Verdict headline
  const verdictText = useMemo(() => {
    const issues = columnAnalysis.issuesCount;
    const dups = profile?.duplicates?.exact_rows || 0;

    if (issues === 0 && dups === 0) {
      return 'All columns appear clean and conform to expected formats.';
    }
    if (issues > 0 && dups > 0) {
      return `${pluralize(issues, 'column requires', 'columns require')} attention and duplicate records were detected.`;
    }
    if (issues > 0) {
      return `${pluralize(issues, 'column requires', 'columns require')} attention before this data is safe to use.`;
    }
    return 'Duplicate rows were detected; column formats appear consistent.';
  }, [columnAnalysis.issuesCount, profile]);

  // Summary strip metrics (neutral and semantic colors strictly for actual status)
  const summaryMetrics: SummaryMetric[] = useMemo(() => {
    const file = profile?.file || {};
    const dupsObj = profile?.duplicates || {};
    const dups = dupsObj.exact_rows || 0;
    const dupPct = dupsObj.exact_pct || 0;
    const cols = profile?.columns || {};

    return [
      {
        label: 'Total Rows',
        value: file.rows ? formatNumber(file.rows) : '—',
        subtext: '100% profiled',
        status: 'normal',
      },
      {
        label: 'Exact Duplicates',
        value: formatNumber(dups),
        subtext: dups > 0 ? `${formatPercent(dupPct)} of records` : 'Zero duplicates',
        status: dups > 0 ? 'warn' : 'normal',
      },
      {
        label: 'Columns Profiled',
        value: formatNumber(file.columns || Object.keys(cols).length),
        subtext: `${columnAnalysis.issuesCount} with issues`,
        status: columnAnalysis.issuesCount > 0 ? 'warn' : 'normal',
      },
      {
        label: 'Profiling Duration',
        value: profile?.profile_duration_seconds ? `${profile.profile_duration_seconds.toFixed(2)}s` : '—',
        subtext: 'Single-pass scan',
        status: 'normal',
      },
    ];
  }, [profile, columnAnalysis.issuesCount]);

  if (!jobData) {
    return (
      <div className="container-shell py-16 sm:py-24 max-w-2xl mx-auto text-center space-y-6">
        <div className="w-14 h-14 rounded-full bg-[var(--bg-subtle)] text-[var(--text-secondary)] flex items-center justify-center mx-auto hairline-border">
          <FileBarChart className="w-7 h-7" />
        </div>
        <div className="space-y-2">
          <h2 className="text-2xl sm:text-3xl font-semibold text-[var(--text-primary)]">
            No Dataset Profiled Yet
          </h2>
          <p className="text-base text-[var(--text-secondary)] leading-relaxed max-w-lg mx-auto">
            Upload a file in Step 1 to generate an automated data quality profile,
            statistical distributions, and anomaly analysis.
          </p>
        </div>
        {onGoToUpload && (
          <Button size="lg" variant="primary" onClick={onGoToUpload}>
            Go to Upload (Step 1)
          </Button>
        )}
      </div>
    );
  }

  // Job failed state
  if (jobData?.job?.status === 'FAILED') {
    return (
      <div className="container-shell py-16 sm:py-24 max-w-2xl mx-auto text-center space-y-6">
        <div className="w-16 h-16 rounded-full bg-[var(--status-danger)]/10 text-[var(--status-danger)] flex items-center justify-center mx-auto border border-[var(--status-danger)]/30">
          <AlertCircle className="w-8 h-8" />
        </div>
        <div className="space-y-2">
          <h2 className="text-2xl sm:text-3xl font-semibold text-[var(--text-primary)] tracking-tight">
            Profiling Failed
          </h2>
          <p className="text-base text-[var(--status-danger)] leading-relaxed max-w-lg mx-auto font-mono text-sm bg-[var(--bg-subtle)] p-3 rounded-lg border border-[var(--border-hairline)]">
            {jobData.job.error || jobData.job.current_stage || 'An unexpected error occurred during profiling.'}
          </p>
        </div>
        {onGoToUpload && (
          <Button size="lg" variant="primary" onClick={onGoToUpload}>
            Return to Upload (Step 1)
          </Button>
        )}
      </div>
    );
  }

  // Active Profiling & AI Drafting state while DuckDB scans the dataset
  if (!profile) {
    return (
      <div className="container-shell py-16 sm:py-24 max-w-2xl mx-auto text-center space-y-6">
        <div className="w-16 h-16 rounded-full bg-[var(--accent-subtle)] text-[var(--accent)] flex items-center justify-center mx-auto">
          <Loader2 className="w-8 h-8 animate-spin" />
        </div>
        <div className="space-y-2">
          <h2 className="text-2xl sm:text-3xl font-semibold text-[var(--text-primary)] tracking-tight">
            Reviewing your data
          </h2>
          <p className="text-base text-[var(--text-secondary)] leading-relaxed max-w-lg mx-auto">
            {jobData?.job?.current_stage || 'Checking columns, missing values, and duplicates...'}
          </p>
        </div>

        <div className="w-72 mx-auto bg-[var(--bg-subtle)] h-2 rounded-full overflow-hidden hairline-border">
          <div
            className="bg-[var(--accent)] h-full transition-all duration-300"
            style={{ width: `${jobData?.job?.progress_pct || 40}%` }}
          />
        </div>

        <div className="flex flex-wrap items-center justify-center gap-2 pt-2 text-xs sm:text-sm text-[var(--text-muted)] font-mono">
          <Tag variant="neutral">Status: {jobData?.job?.status || 'PROFILING'}</Tag>
          {jobData?.job?.filename && (
            <>
              <span>·</span>
              <Tag variant="neutral">{jobData.job.filename}</Tag>
            </>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="container-shell py-8 sm:py-12 space-y-8">
      {/* Editorial Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-6 border-b border-[var(--border-hairline)]">
        <div>
          <h1 className="text-2xl sm:text-3xl font-semibold text-[var(--text-primary)] tracking-tight">
            Data Quality Assessment
          </h1>
          <p className="text-sm sm:text-base text-[var(--text-muted)] mt-1.5 max-w-2xl leading-relaxed">
            {verdictText}
          </p>
        </div>

        <Button
          variant="primary"
          size="lg"
          onClick={onProceedToApproval}
          rightIcon={
            !jobData?.recipe ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <ArrowRight className="w-5 h-5" />
            )
          }
        >
          {!jobData?.recipe ? 'Drafting Recipe & Confirmations...' : 'Review Cleaning Plan (Step 3)'}
        </Button>
      </div>

      {/* Summary Strip (Horizontal Metrics Grid) */}
      <SummaryStrip metrics={summaryMetrics} />

      {/* Formatted AI Quality Report */}
      {jobData?.report_markdown && (
        <div className="bg-[var(--bg-surface)] border border-[var(--border-hairline)] rounded-xl p-6 sm:p-8 space-y-4 shadow-2xs">
          <div className="flex items-center justify-between pb-3.5 border-b border-[var(--border-hairline)]">
            <h2 className="text-base sm:text-lg font-semibold text-[var(--text-primary)] tracking-tight">
              AI Data Quality Report
            </h2>
            <Tag variant="neutral" font="mono">Generated from Statistical Profile</Tag>
          </div>

          <MarkdownView content={jobData.report_markdown} />
        </div>
      )}

      {/* Column Inspector Table */}
      <div className="bg-[var(--bg-surface)] border border-[var(--border-hairline)] rounded-xl overflow-hidden shadow-2xs">
        {/* Table Controls (Search & Filter) */}
        <div className="p-4 sm:p-5 border-b border-[var(--border-hairline)] flex flex-col sm:flex-row sm:items-center justify-between gap-3.5 bg-[var(--bg-subtle)]">
          <div className="flex items-center gap-2.5">
            <h2 className="text-base sm:text-lg font-semibold text-[var(--text-primary)] tracking-tight">
              Column Profiler Breakdown
            </h2>
            <Tag variant="neutral" font="mono">
              {filteredColumns.length} of {columnAnalysis.list.length} columns
            </Tag>
          </div>

          <div className="flex items-center gap-3">
            {/* Search filter */}
            <div className="relative">
              <Search className="w-4 h-4 text-[var(--text-muted)] absolute left-3 top-2.5" />
              <input
                type="text"
                placeholder="Search column..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="pl-9 pr-3.5 py-1.5 text-sm font-sans rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-surface)] text-[var(--text-primary)] placeholder-[var(--text-muted)] focus:outline-none focus:border-[#FF4B3E]"
              />
            </div>

            {/* Issues only toggle */}
            <Button
              size="sm"
              variant={filterIssuesOnly ? 'primary' : 'secondary'}
              onClick={() => setFilterIssuesOnly(!filterIssuesOnly)}
              leftIcon={<Filter className="w-3.5 h-3.5" />}
            >
              {filterIssuesOnly ? 'Showing Issues Only' : 'Issues Only'}
            </Button>
          </div>
        </div>

        {/* Table Content */}
        <div
          tabIndex={0}
          role="region"
          aria-label="Column profiler table"
          className="overflow-x-auto max-h-[620px] focus:outline-none focus:ring-1 focus:ring-[var(--accent)]"
        >
          <table className="w-full text-left border-collapse text-sm">
            <thead className="sticky top-0 bg-[var(--bg-subtle)] hairline-b text-[var(--text-muted)] font-medium z-10 select-none">
              <tr>
                <th className="py-3 px-4 font-semibold text-xs uppercase tracking-wider w-8"></th>
                <th className="py-3 px-4 font-semibold text-xs uppercase tracking-wider">Column</th>
                <th className="py-3 px-4 font-semibold text-xs uppercase tracking-wider">Inferred Type</th>
                <th className="py-3 px-4 font-semibold text-xs uppercase tracking-wider">Missing / Nulls</th>
                <th className="py-3 px-4 font-semibold text-xs uppercase tracking-wider">Cardinality</th>
                <th className="py-3 px-4 font-semibold text-xs uppercase tracking-wider">Status & Anomalies</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border-hairline)]">
              {filteredColumns.length === 0 ? (
                <tr>
                  <td colSpan={6} className="py-8 text-center text-sm text-[var(--text-muted)]">
                    No columns match the current filter.
                  </td>
                </tr>
              ) : (
                filteredColumns.map(({ name, meta, status }) => {
                  const isExpanded = Boolean(expandedColumns[name]);
                  const nullPct = meta.null_pct || 0;
                  const tokenCount = Object.values(meta.null_tokens || {}).reduce(
                    (a: number, b: any) => a + Number(b),
                    0
                  );

                  return (
                    <React.Fragment key={name}>
                      <tr
                        onClick={() => toggleExpand(name)}
                        className="hover:bg-[var(--bg-hover)] transition-colors cursor-pointer"
                      >
                        <td className="py-3 px-4 text-[var(--text-muted)]">
                          {isExpanded ? (
                            <ChevronDown className="w-4 h-4" />
                          ) : (
                            <ChevronRight className="w-4 h-4" />
                          )}
                        </td>
                        <td className="py-3 px-4 font-semibold text-[var(--text-primary)] font-mono text-sm">
                          {name}
                        </td>
                        <td className="py-3 px-4">
                          <Tag variant="neutral" font="mono">
                            {meta.inferred_type || 'string'}
                          </Tag>
                        </td>
                        <td className="py-3 px-4 font-mono text-sm">
                          <div className="flex items-center gap-2.5">
                            <span>
                              {formatNumber(meta.nulls)} ({formatPercent(nullPct)})
                            </span>
                            {nullPct > 0 && (
                              <div className="w-14 h-1.5 bg-[var(--border-hairline)] rounded-full overflow-hidden shrink-0">
                                <div
                                  className="h-full bg-[var(--status-warn)]"
                                  style={{ width: `${Math.min(nullPct, 100)}%` }}
                                />
                              </div>
                            )}
                          </div>
                        </td>
                        <td className="py-3 px-4 font-mono text-sm">
                          <span>{formatNumber(meta.distinct)}</span>
                          {meta.candidate_key && (
                            <Tag variant="ok" className="ml-2" font="sans">
                              Primary Key
                            </Tag>
                          )}
                        </td>
                        <td className="py-2.5 px-4">
                          <div className="inline-flex items-center gap-1.5 text-xs font-medium select-none">
                            {status.isClean ? (
                              <>
                                <CheckCircle2 className="w-3.5 h-3.5 text-[var(--status-ok)] shrink-0" />
                                <span className="text-[var(--status-ok)]">Clean</span>
                              </>
                            ) : status.severity === 'danger' ? (
                              <>
                                <AlertCircle className="w-3.5 h-3.5 text-[var(--status-danger)] shrink-0" />
                                <span className="text-[var(--status-danger)] font-semibold">{status.label}</span>
                              </>
                            ) : (
                              <>
                                <AlertTriangle className="w-3.5 h-3.5 text-[var(--status-warn)] shrink-0" />
                                <span className="text-[var(--status-warn)] font-medium">{status.label}</span>
                              </>
                            )}
                          </div>
                        </td>
                      </tr>

                      {/* Expandable Inspection Details */}
                      {isExpanded && (
                        <tr className="bg-[var(--bg-subtle)]">
                          <td colSpan={6} className="px-10 py-3.5 space-y-2 text-xs">
                            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                              {/* Diagnostic Issues */}
                              <div>
                                <span className="font-semibold text-[11px] text-[var(--text-muted)] uppercase tracking-wider block mb-1">
                                  Detected Issues
                                </span>
                                {status.issues.length === 0 ? (
                                  <span className="text-[var(--status-ok-text)]">No issues detected in column.</span>
                                ) : (
                                  <ul className="list-disc pl-4 space-y-0.5 text-[var(--text-secondary)]">
                                    {status.issues.map((issue: string, idx: number) => (
                                      <li key={idx}>{issue}</li>
                                    ))}
                                  </ul>
                                )}
                              </div>

                              {/* Null Tokens Breakdown */}
                              <div>
                                <span className="font-semibold text-[11px] text-[var(--text-muted)] uppercase tracking-wider block mb-1">
                                  Placeholder Tokens ({tokenCount})
                                </span>
                                {tokenCount === 0 ? (
                                  <span className="text-[var(--text-muted)]">No dirty string tokens found.</span>
                                ) : (
                                  <div className="flex flex-wrap gap-1 font-mono text-[11px]">
                                    {Object.entries(meta.null_tokens || {}).map(([tok, cnt]) => (
                                      <span
                                        key={tok}
                                        className="bg-[var(--bg-surface)] px-1.5 py-0.5 rounded-[var(--radius-xs)] hairline-border"
                                      >
                                        "{tok}": {formatNumber(cnt as number)}
                                      </span>
                                    ))}
                                  </div>
                                )}
                              </div>

                              {/* Sample Values */}
                              <div>
                                <span className="font-semibold text-[11px] text-[var(--text-muted)] uppercase tracking-wider block mb-1">
                                  Sample Values
                                </span>
                                {meta.sample_values && meta.sample_values.length > 0 ? (
                                  <div className="flex flex-wrap gap-1 font-mono text-[11px] text-[var(--text-secondary)]">
                                    {meta.sample_values.slice(0, 5).map((val: string, idx: number) => (
                                      <span
                                        key={idx}
                                        className="bg-[var(--bg-surface)] px-1.5 py-0.5 rounded-[var(--radius-xs)] hairline-border truncate max-w-[120px]"
                                        title={val}
                                      >
                                        {val === '' ? '<empty>' : val}
                                      </span>
                                    ))}
                                  </div>
                                ) : (
                                  <span className="text-[var(--text-muted)]">Sample values withheld or empty.</span>
                                )}
                              </div>
                            </div>
                          </td>
                        </tr>
                      )}
                    </React.Fragment>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Engine Details Disclosure (Collapsed at bottom) */}
      <Disclosure
        title="Technical Engine Details"
        subtitle="DuckDB Parquet staging & profiler architecture"
        defaultOpen={false}
      >
        <div className="pt-2 overflow-x-auto">
          <table className="w-full text-left border-collapse text-xs font-mono">
            <thead>
              <tr className="hairline-b text-[var(--text-muted)] font-medium">
                <th className="py-1.5 px-3 uppercase tracking-wider">Engine Component</th>
                <th className="py-1.5 px-3 uppercase tracking-wider">Architecture Detail</th>
                <th className="py-1.5 px-3 uppercase tracking-wider text-right">Verification</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border-hairline)] text-[var(--text-secondary)]">
              <tr>
                <td className="py-2 px-3 text-[var(--text-muted)]">Core Profiler</td>
                <td className="py-2 px-3 text-[var(--text-primary)] font-medium">DuckDB Vectorized C++ Engine</td>
                <td className="py-2 px-3 text-right text-[var(--status-ok)]">Single Pass</td>
              </tr>
              <tr>
                <td className="py-2 px-3 text-[var(--text-muted)]">Staging Buffer</td>
                <td className="py-2 px-3 text-[var(--text-primary)] font-medium">Text-only columnar Parquet (Zstandard)</td>
                <td className="py-2 px-3 text-right text-[var(--status-ok)]">Lossless</td>
              </tr>
              <tr>
                <td className="py-2 px-3 text-[var(--text-muted)]">Inference Strategy</td>
                <td className="py-2 px-3 text-[var(--text-primary)] font-medium">Exact token frequency & null sentinel analysis</td>
                <td className="py-2 px-3 text-right text-[var(--status-ok)]">Deterministic</td>
              </tr>
            </tbody>
          </table>
        </div>
      </Disclosure>

      {/* Bottom Step Progression Banner */}
      <div className="pt-6 border-t border-[var(--border-hairline)] flex flex-col sm:flex-row sm:items-center justify-between gap-4 bg-[var(--bg-surface)] p-6 rounded-xl border border-[var(--border-hairline)] shadow-2xs">
        <div>
          <span className="font-semibold text-[var(--text-primary)] block text-base sm:text-lg tracking-tight">
            Finished Reviewing Assessment?
          </span>
          <span className="text-xs sm:text-sm text-[var(--text-muted)]">
            Proceed to Step 3 to review transformation rules, resolve ambiguities, and approve execution.
          </span>
        </div>

        <Button
          variant="primary"
          size="lg"
          onClick={onProceedToApproval}
          rightIcon={
            !jobData?.recipe ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <ArrowRight className="w-5 h-5" />
            )
          }
        >
          {!jobData?.recipe ? 'Drafting Recipe & Confirmations...' : 'Review Cleaning Plan (Step 3)'}
        </Button>
      </div>
    </div>
  );
};
