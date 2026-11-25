import React, { useEffect, useMemo, useState } from 'react';
import type { JobDetailResponse } from '../lib/api';
import { Button } from '../components/ui/Button';
import { Callout } from '../components/ui/Callout';
import { Disclosure } from '../components/ui/Disclosure';
import { Tag } from '../components/ui/Tag';
import { Toggle } from '../components/ui/Toggle';
import { cleanRuleSentence, formatNumber, pluralize } from '../lib/format';
import { AlertTriangle, Check, CheckCircle2, CheckSquare, Code2, Loader2, Sliders } from 'lucide-react';

interface ApprovalPageProps {
  jobData: JobDetailResponse | null;
  onStartCleaning: (customRecipe?: any) => void;
  onGoToUpload?: () => void;
}

export const ApprovalPage: React.FC<ApprovalPageProps> = ({ jobData, onStartCleaning, onGoToUpload }) => {
  const profile = jobData?.profile;
  const fileMeta = profile?.file || {};
  const duplicates = profile?.duplicates || {};
  const columnsProfile = profile?.columns || {};
  const initialRecipe = jobData?.recipe;
  const initialOpenQuestions = jobData?.open_questions || [];

  // Track editable recipe copy
  const [recipe, setRecipe] = useState<any>(() => {
    return initialRecipe ? JSON.parse(JSON.stringify(initialRecipe)) : null;
  });

  // Keep recipe updated when jobData finishes loading from the server
  useEffect(() => {
    if (jobData?.recipe) {
      setRecipe(JSON.parse(JSON.stringify(jobData.recipe)));
    }
  }, [jobData?.recipe]);

  // Track answers to open questions: questionIndex -> answer
  const [questionAnswers, setQuestionAnswers] = useState<Record<number, string>>(() => {
    return {};
  });

  const [cleaningTriggered, setCleaningTriggered] = useState(false);

  // Check if all questions have been addressed and recipe exists
  const allQuestionsAnswered = useMemo(() => {
    const questions = jobData?.open_questions || [];
    if (questions.length === 0) return Boolean(recipe);
    return questions.every((_, idx) => Boolean(questionAnswers[idx]));
  }, [jobData?.open_questions, questionAnswers, recipe]);

  // Projected impact computed strictly from real profile metrics
  const projectedImpact = useMemo(() => {
    const dups = profile?.duplicates || {};
    const colsProf = profile?.columns || {};
    const items: Array<{ title: string; count: number; detail: string; severity: 'ok' | 'warn' | 'danger' }> = [];

    // Duplicates
    if (recipe?.dedupe && dups.exact_rows > 0) {
      items.push({
        title: 'Duplicate Row Removal',
        count: dups.exact_rows,
        detail: `${formatNumber(dups.exact_rows)} exact duplicates will be removed`,
        severity: 'ok',
      });
    }

    // Column-level impacts
    if (recipe?.columns) {
      Object.entries(recipe.columns).forEach(([colName, colRule]: [string, any]) => {
        const colProf = colsProf[colName] || {};

        // Drop rows on null
        if (colRule.handle_nulls?.strategy === 'drop_row' && colProf.nulls > 0) {
          items.push({
            title: `Row Filter on ${colName}`,
            count: colProf.nulls,
            detail: `${formatNumber(colProf.nulls)} rows with missing ${colName} will be dropped`,
            severity: 'warn',
          });
        }

        // Fill nulls
        if (colRule.handle_nulls && colRule.handle_nulls.strategy !== 'drop_row' && colProf.nulls > 0) {
          const strat = colRule.handle_nulls.strategy.replace('fill_', '');
          items.push({
            title: `Missing Value Imputation (${colName})`,
            count: colProf.nulls,
            detail: `${formatNumber(colProf.nulls)} empty values will be filled with column ${strat}`,
            severity: 'ok',
          });
        }

        // Whitespace & Tokens
        if (colRule.trim_and_normalize && colProf.whitespace_issues > 0) {
          items.push({
            title: `Whitespace Trim (${colName})`,
            count: colProf.whitespace_issues,
            detail: `${formatNumber(colProf.whitespace_issues)} values with stray spacing will be trimmed`,
            severity: 'ok',
          });
        }

        // Outlier bounds
        if (colRule.clip_outliers && colProf.negatives > 0) {
          const action = colRule.clip_outliers.action === 'clamp' ? 'clamped to 0' : 'set to null';
          items.push({
            title: `Outlier Normalization (${colName})`,
            count: colProf.negatives,
            detail: `${formatNumber(colProf.negatives)} negative values will be ${action}`,
            severity: 'ok',
          });
        }
      });
    }

    return items;
  }, [recipe, profile]);

  // Toggle column rule
  const handleToggleColumn = (colName: string, enabled: boolean) => {
    if (!recipe) return;
    const nextRecipe = { ...recipe };
    nextRecipe.columns = { ...nextRecipe.columns };

    if (!enabled) {
      delete nextRecipe.columns[colName];
    } else if (initialRecipe?.columns?.[colName]) {
      nextRecipe.columns[colName] = JSON.parse(JSON.stringify(initialRecipe.columns[colName]));
    }
    setRecipe(nextRecipe);
  };

  // Toggle dedupe
  const handleToggleDedupe = (enabled: boolean) => {
    if (!recipe) return;
    const nextRecipe = { ...recipe };
    if (!enabled) {
      delete nextRecipe.dedupe;
    } else if (initialRecipe?.dedupe) {
      nextRecipe.dedupe = JSON.parse(JSON.stringify(initialRecipe.dedupe));
    } else {
      nextRecipe.dedupe = { keys: [Object.keys(columnsProfile)[0] || 'id'], keep: 'latest' };
    }
    setRecipe(nextRecipe);
  };

  // Change null handling strategy
  const handleNullStrategyChange = (colName: string, strategy: string) => {
    if (!recipe?.columns?.[colName]) return;
    const nextRecipe = { ...recipe };
    nextRecipe.columns[colName] = {
      ...nextRecipe.columns[colName],
      handle_nulls: {
        strategy,
        value: strategy === 'fill_value' ? 'unknown' : undefined,
      },
    };
    setRecipe(nextRecipe);
  };

  // Trigger execution
  const handleApprove = () => {
    setCleaningTriggered(true);
    onStartCleaning(recipe);
  };

  if (!jobData) {
    return (
      <div className="container-shell py-16 sm:py-24 max-w-2xl mx-auto text-center space-y-6">
        <div className="w-14 h-14 rounded-full bg-[var(--bg-subtle)] text-[var(--text-secondary)] flex items-center justify-center mx-auto hairline-border">
          <CheckSquare className="w-7 h-7" />
        </div>
        <div className="space-y-2">
          <h2 className="text-2xl sm:text-3xl font-semibold text-[var(--text-primary)]">
            No Cleaning Plan Compiled Yet
          </h2>
          <p className="text-base text-[var(--text-secondary)] leading-relaxed max-w-lg mx-auto">
            After uploading a CSV file in Step 1, the AI compiles a deterministic cleaning recipe
            for you to review, customize parameters, and approve before execution.
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

  // Active Compiling / Drafting state
  const isCompilingRecipe =
    !recipe &&
    (jobData?.job?.status === 'CONVERTING' ||
      jobData?.job?.status === 'PROFILING' ||
      jobData?.job?.status === 'DRAFTING_RECIPE');

  if (isCompilingRecipe) {
    return (
      <div className="container-shell py-16 sm:py-24 max-w-2xl mx-auto text-center space-y-6">
        <div className="w-16 h-16 rounded-full bg-[var(--accent-subtle)] text-[var(--accent)] flex items-center justify-center mx-auto">
          <Loader2 className="w-8 h-8 animate-spin" />
        </div>
        <div className="space-y-2">
          <h2 className="text-2xl sm:text-3xl font-semibold text-[var(--text-primary)] tracking-tight">
            Drafting Cleaning Plan & Human Confirmations
          </h2>
          <p className="text-base text-[var(--text-secondary)] leading-relaxed max-w-lg mx-auto">
            {jobData?.job?.current_stage ||
              'DuckDB profiler and AI engine are analyzing dataset anomalies and drafting confirmation choices...'}
          </p>
        </div>

        <div className="w-72 mx-auto bg-[var(--bg-subtle)] h-2 rounded-full overflow-hidden hairline-border">
          <div
            className="bg-[var(--accent)] h-full transition-all duration-300"
            style={{ width: `${jobData?.job?.progress_pct || 75}%` }}
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
            Review & Approve Cleaning Plan
          </h1>
          <p className="text-sm sm:text-base text-[var(--text-muted)] mt-1.5 max-w-2xl leading-relaxed">
            Review proposed transformation rules, answer confirmation questions, and tailor parameters.
            Your original CSV file is never modified or overwritten.
          </p>
        </div>

        <Button
          variant="primary"
          size="lg"
          disabled={cleaningTriggered || !allQuestionsAnswered || !recipe}
          onClick={() => {
            if (!allQuestionsAnswered) {
              const el = document.getElementById('human-confirmations-section');
              if (el) {
                el.scrollIntoView({ behavior: 'smooth', block: 'start' });
              }
              return;
            }
            handleApprove();
          }}
        >
          {cleaningTriggered
            ? 'Starting Cleaning...'
            : !allQuestionsAnswered
            ? `Answer Human Confirmations (${initialOpenQuestions.length - Object.keys(questionAnswers).length} remaining)`
            : 'Approve & Start Cleaning (Step 4)'}
        </Button>
      </div>

      {/* 1. Required Human Confirmations Table */}
      {initialOpenQuestions.length > 0 && (
        <div id="human-confirmations-section" className="scroll-mt-24 bg-[var(--bg-surface)] border border-[var(--border-hairline)] rounded-xl overflow-hidden shadow-2xs">
          <div className="p-3.5 sm:p-4 border-b border-[var(--border-hairline)] bg-[var(--bg-subtle)] flex items-center justify-between">
            <div className="flex items-center gap-2.5">
              <span className="text-xs font-semibold uppercase tracking-wider text-[var(--text-muted)] font-mono">
                Required Human Confirmations
              </span>
              <span className="text-xs font-mono text-[var(--text-muted)]">
                {allQuestionsAnswered ? 'All confirmations answered' : `${initialOpenQuestions.length - Object.keys(questionAnswers).length} pending decisions`}
              </span>
            </div>
            <div className="inline-flex items-center gap-1.5 text-xs font-medium">
              {allQuestionsAnswered ? (
                <>
                  <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400 shrink-0" />
                  <span className="text-emerald-400 font-mono">All Confirmed</span>
                </>
              ) : (
                <>
                  <AlertTriangle className="w-3.5 h-3.5 text-amber-400 shrink-0" />
                  <span className="text-amber-400 font-mono">Action Required</span>
                </>
              )}
            </div>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse text-xs sm:text-sm">
              <thead>
                <tr className="border-b border-[var(--border-hairline)] text-[var(--text-muted)] font-medium bg-[var(--bg-surface)]">
                  <th className="py-2.5 px-4 font-semibold text-xs uppercase tracking-wider w-[44%] font-mono">
                    Ambiguity / Question
                  </th>
                  <th className="py-2.5 px-4 font-semibold text-xs uppercase tracking-wider font-mono">
                    Resolution Choice
                  </th>
                  <th className="py-2.5 px-4 font-semibold text-xs uppercase tracking-wider text-right w-[18%] font-mono">
                    Status
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--border-hairline)]">
                {initialOpenQuestions.map((q, idx) => {
                  const isAnswered = Boolean(questionAnswers[idx]);
                  const choice = questionAnswers[idx];

                  return (
                    <tr
                      key={idx}
                      className="transition-colors hover:bg-[var(--bg-hover)]"
                    >
                      <td className="py-3.5 px-4 align-top">
                        <span className="font-semibold text-xs text-[var(--text-muted)] uppercase tracking-wider block font-mono mb-1">
                          Confirmation #{idx + 1}
                        </span>
                        <p className="text-xs sm:text-sm text-[var(--text-primary)] leading-relaxed font-medium">
                          {cleanRuleSentence(q)}
                        </p>
                      </td>

                      <td className="py-4 px-4 align-middle">
                        <div className="flex flex-wrap items-center gap-2.5 sm:gap-3">
                          <button
                            type="button"
                            onClick={() => setQuestionAnswers((prev) => ({ ...prev, [idx]: 'standard' }))}
                            className={`inline-flex items-center gap-2 px-4 py-2 sm:px-5 sm:py-2.5 rounded-lg border text-sm font-sans transition-all cursor-pointer select-none ${
                              choice === 'standard'
                                ? 'bg-[#FF4B3E] text-white border-transparent font-semibold shadow-xs'
                                : 'bg-[var(--bg-subtle)] text-[var(--text-secondary)] border-[var(--border-hairline)] hover:border-[var(--border-strong)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-hover)] font-medium'
                            }`}
                          >
                            <Check className={`w-4 h-4 shrink-0 ${choice === 'standard' ? 'text-white' : 'text-[var(--text-muted)]'}`} />
                            <span>Apply Recommended Fix</span>
                          </button>

                          <button
                            type="button"
                            onClick={() => setQuestionAnswers((prev) => ({ ...prev, [idx]: 'preserve' }))}
                            className={`inline-flex items-center gap-2 px-4 py-2 sm:px-5 sm:py-2.5 rounded-lg border text-sm font-sans transition-all cursor-pointer select-none ${
                              choice === 'preserve'
                                ? 'bg-[#FF4B3E] text-white border-transparent font-semibold shadow-xs'
                                : 'bg-[var(--bg-subtle)] text-[var(--text-secondary)] border-[var(--border-hairline)] hover:border-[var(--border-strong)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-hover)] font-medium'
                            }`}
                          >
                            <span>Keep Original Value</span>
                          </button>
                        </div>
                      </td>

                      <td className="py-3.5 px-4 align-middle text-right whitespace-nowrap">
                        <div className="inline-flex items-center gap-1.5 font-medium text-xs sm:text-sm">
                          {isAnswered ? (
                            <>
                              <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400 shrink-0" />
                              <span className="text-emerald-400 font-mono">Confirmed</span>
                            </>
                          ) : (
                            <>
                              <AlertTriangle className="w-3.5 h-3.5 text-amber-400 shrink-0" />
                              <span className="text-amber-400 font-mono">Pending</span>
                            </>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Main Two-Column Layout */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-8 items-start">
        {/* Left Column: Interactive Rules List (7 cols) */}
        <div className="lg:col-span-7 space-y-4">
          <div className="flex items-center justify-between pb-2.5 border-b border-[var(--border-hairline)]">
            <div>
              <h2 className="text-base sm:text-lg font-semibold text-[var(--text-primary)] tracking-tight">
                Proposed Cleaning Rules
              </h2>
              <p className="text-sm text-[var(--text-muted)] mt-0.5">
                Toggle rules on or off, or customize how nulls and formats are treated.
              </p>
            </div>
            <span className="text-sm font-mono text-[var(--text-muted)]">
              {recipe ? (Object.keys(recipe.columns || {}).length + (recipe.dedupe ? 1 : 0)) : 0} active
            </span>
          </div>

          <div className="bg-[var(--bg-surface)] border border-[var(--border-hairline)] rounded-xl overflow-hidden divide-y divide-[var(--border-hairline)] shadow-2xs">
            <div className="p-3.5 sm:p-4 border-b border-[var(--border-hairline)] bg-[var(--bg-subtle)] flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wider text-[var(--text-muted)] font-mono">
                Active Transformation Pipeline Rules
              </span>
              <span className="text-xs text-[var(--text-muted)] font-mono">
                {recipe ? (Object.keys(recipe.columns || {}).length + (recipe.dedupe ? 1 : 0)) : 0} configured
              </span>
            </div>
            {/* Rule 1: Deduplication Rule */}
            {initialRecipe?.dedupe && (
              <div
                className={`p-4 sm:p-5 transition-colors ${
                  recipe?.dedupe ? 'hover:bg-[var(--bg-hover)]' : 'opacity-60 bg-[var(--bg-subtle)]'
                }`}
              >
                <div className="flex items-start justify-between gap-4">
                  <div className="flex items-start gap-3.5 flex-1 min-w-0">
                    <span className="w-6 h-6 rounded-full bg-[var(--bg-subtle)] border border-[var(--border-hairline)] flex items-center justify-center text-xs font-mono font-medium text-[var(--text-secondary)] shrink-0 mt-0.5">
                      1
                    </span>
                    <div className="space-y-1.5 flex-1">
                      <div className="flex items-center gap-2.5">
                        <span className="font-semibold text-sm sm:text-base text-[var(--text-primary)]">
                          Deduplicate Records
                        </span>
                        <Tag variant="neutral" font="mono">
                          {recipe?.dedupe?.keys.join(', ')}
                        </Tag>
                      </div>
                      <p className="text-sm text-[var(--text-secondary)] leading-relaxed">
                        Remove exact and duplicate records partitioned by primary key{' '}
                        <code className="font-mono text-xs sm:text-sm font-medium text-[var(--text-primary)] bg-[var(--bg-subtle)] px-1 rounded">
                          {recipe?.dedupe?.keys.join(', ')}
                        </code>
                        {recipe?.dedupe?.order_by ? `, keeping the latest record ordered by ${recipe.dedupe.order_by}` : ''}.
                      </p>
                      <span className="text-xs sm:text-sm text-[var(--text-muted)] block">
                        Why: Ingested dataset contains {formatNumber(duplicates.exact_rows || 0)} duplicate records ({duplicates.exact_pct || 0}%).
                      </span>
                    </div>
                  </div>

                  <Toggle
                    checked={Boolean(recipe?.dedupe)}
                    ariaLabel="Toggle record deduplication rule"
                    onChange={(checked) => handleToggleDedupe(checked)}
                  />
                </div>
              </div>
            )}

            {/* Column Transformation Rules */}
            {initialRecipe?.columns &&
              Object.entries(initialRecipe.columns).map(([colName, originalRule]: [string, any], idx) => {
                const isActive = Boolean(recipe?.columns?.[colName]);
                const activeRule = recipe?.columns?.[colName] || originalRule;
                const colProfile = columnsProfile[colName] || {};
                const ruleNumber = idx + (initialRecipe.dedupe ? 2 : 1);

                return (
                  <div
                    key={colName}
                    className={`p-4 sm:p-5 transition-colors ${
                      isActive ? 'hover:bg-[var(--bg-hover)]' : 'opacity-60 bg-[var(--bg-subtle)]'
                    }`}
                  >
                    <div className="flex items-start justify-between gap-4">
                      <div className="flex items-start gap-3.5 flex-1 min-w-0">
                        <span className="w-6 h-6 rounded-full bg-[var(--bg-subtle)] border border-[var(--border-hairline)] flex items-center justify-center text-xs font-mono font-medium text-[var(--text-secondary)] shrink-0 mt-0.5">
                          {ruleNumber}
                        </span>

                        <div className="space-y-2 flex-1 min-w-0">
                          {/* Column header & type */}
                          <div className="flex items-center gap-2.5">
                            <span className="font-semibold text-sm sm:text-base text-[var(--text-primary)]">
                              {colName}
                            </span>
                            <Tag variant="neutral" font="mono">
                              {colProfile.inferred_type || 'string'}
                            </Tag>
                          </div>

                          {/* Rule description */}
                          <div className="space-y-1.5 text-sm text-[var(--text-secondary)]">
                            {activeRule.trim_and_normalize && (
                              <p>
                                Trim whitespace and normalize empty placeholder tokens ('N/A', 'null', '-') to null.
                              </p>
                            )}
                            {activeRule.parse_timestamp && (
                              <p>
                                Parse date/time using verified patterns, standardized in{' '}
                                <code className="font-mono text-xs sm:text-sm text-[var(--text-primary)] bg-[var(--bg-subtle)] px-1 py-0.5 rounded border border-[var(--border-hairline)]">{activeRule.parse_timestamp.source_tz || 'UTC'}</code> timezone.
                              </p>
                            )}
                            {activeRule.clip_outliers && (
                              <p>
                                Restrict values outside bounds [{activeRule.clip_outliers.min ?? '—'}, {activeRule.clip_outliers.max ?? '—'}] ({activeRule.clip_outliers.action}).
                              </p>
                            )}
                            {activeRule.handle_nulls && (
                              <div className="flex items-center gap-2.5 pt-1">
                                <span className="text-[var(--text-muted)] text-sm">Null strategy:</span>
                                <select
                                  disabled={!isActive}
                                  value={activeRule.handle_nulls.strategy}
                                  aria-label={`Null handling strategy for ${colName}`}
                                  onChange={(e) => handleNullStrategyChange(colName, e.target.value)}
                                  className="text-sm font-sans px-3 py-1.5 rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-surface)] text-[var(--text-primary)] cursor-pointer focus:outline-none focus:border-[#FF4B3E]"
                                >
                                  <option value="drop_row">Drop row if null</option>
                                  <option value="fill_median">Fill with median</option>
                                  <option value="fill_mode">Fill with mode</option>
                                  <option value="fill_value">Fill with default value</option>
                                </select>
                              </div>
                            )}
                          </div>

                          {/* Diagnostic Reason */}
                          <div className="text-xs text-[var(--text-muted)] pt-1">
                            Why: Column contains {pluralize(colProfile.nulls || 0, 'null', 'nulls')}
                            {colProfile.whitespace_issues ? `, ${formatNumber(colProfile.whitespace_issues)} whitespace anomalies` : ''}
                            {colProfile.negatives ? `, ${formatNumber(colProfile.negatives)} negative values` : ''}.
                          </div>
                        </div>
                      </div>

                      <Toggle
                        checked={isActive}
                        ariaLabel={`Toggle ${colName} cleaning rule`}
                        onChange={(checked) => handleToggleColumn(colName, checked)}
                      />
                    </div>
                  </div>
                );
              })}
          </div>
        </div>

        {/* Right Column: Projected Impact & Reassurance (5 cols) */}
        <div className="lg:col-span-5 space-y-6">
          {/* Projected Impact Table */}
          <div className="bg-[var(--bg-surface)] border border-[var(--border-hairline)] rounded-xl overflow-hidden shadow-2xs">
            <div className="p-3.5 sm:p-4 border-b border-[var(--border-hairline)] bg-[var(--bg-subtle)] flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Sliders className="w-4 h-4 text-[#FF4B3E]" />
                <h3 className="text-sm sm:text-base font-semibold text-[var(--text-primary)] tracking-tight">
                  Projected Cleaning Impact
                </h3>
              </div>
              <span className="text-xs font-mono text-[var(--text-muted)]">
                Derived from Profile
              </span>
            </div>

            {projectedImpact.length === 0 ? (
              <div className="p-4 text-sm text-[var(--text-muted)] italic text-center">
                No active rules currently alter record counts or cell values.
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left border-collapse text-xs sm:text-sm">
                  <thead>
                    <tr className="border-b border-[var(--border-hairline)] text-[var(--text-muted)] font-medium bg-[var(--bg-surface)]">
                      <th className="py-2.5 px-4 font-semibold text-xs uppercase tracking-wider w-[40%] font-mono">
                        Rule / Target
                      </th>
                      <th className="py-2.5 px-4 font-semibold text-xs uppercase tracking-wider font-mono">
                        Projected Action
                      </th>
                      <th className="py-2.5 px-4 font-semibold text-xs uppercase tracking-wider text-right w-[20%] font-mono">
                        Status
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--border-hairline)]">
                    {projectedImpact.map((item, idx) => (
                      <tr key={idx} className="hover:bg-[var(--bg-hover)] transition-colors">
                        <td className="py-3 px-4 align-top">
                          <span className="font-semibold text-xs sm:text-sm text-[var(--text-primary)] block">
                            {item.title}
                          </span>
                        </td>
                        <td className="py-3 px-4 align-top text-xs sm:text-sm text-[var(--text-secondary)] leading-relaxed">
                          {item.detail}
                        </td>
                        <td className="py-3 px-4 align-top text-right whitespace-nowrap">
                          <div className="inline-flex items-center gap-1.5 font-medium text-xs sm:text-sm">
                            {item.severity === 'warn' ? (
                              <>
                                <AlertTriangle className="w-3.5 h-3.5 text-amber-400 shrink-0" />
                                <span className="text-amber-400 font-mono">Row Drop</span>
                              </>
                            ) : (
                              <>
                                <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400 shrink-0" />
                                <span className="text-emerald-400 font-mono">Active</span>
                              </>
                            )}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {/* Safety Guarantee Callout */}
          <Callout variant="ok" title="Original Data Preserved">
            Your original CSV file is never altered or replaced. DuckDB builds the cleaned analytical
            dataset in a dedicated destination path. You can safely inspect and export without risk of data loss.
          </Callout>

          {/* Export as Code availability notice */}
          <div className="flex items-start gap-3 rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-4 py-3 text-sm">
            <Code2 className="mt-0.5 h-4 w-4 shrink-0 text-[var(--accent-primary)]" />
            <div>
              <span className="font-semibold text-[var(--text-primary)]">Export as Code available after cleaning </span>
              <span className="text-[var(--text-muted)]">
                — Once cleaning finishes (Step 5), use the{' '}
                <span className="font-medium text-[var(--text-secondary)]">Export as Code</span> button on the results
                screen to download this recipe as SQL, dbt, pandas, or Polars.
              </span>
            </div>
          </div>

          {/* Advanced Technical Details (Collapsed by default) */}
          <Disclosure
            title="Advanced Execution Parameters"
            subtitle="DuckDB in-process memory & threads"
            defaultOpen={false}
          >
            <div className="pt-2 overflow-x-auto">
              <table className="w-full text-left border-collapse text-xs font-mono">
                <thead>
                  <tr className="border-b border-[var(--border-hairline)] text-[var(--text-muted)] font-medium">
                    <th className="py-1.5 px-3 uppercase tracking-wider">Parameter</th>
                    <th className="py-1.5 px-3 uppercase tracking-wider">Configured Value</th>
                    <th className="py-1.5 px-3 uppercase tracking-wider text-right">Engine Mode</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--border-hairline)] text-[var(--text-secondary)]">
                  <tr>
                    <td className="py-2 px-3 text-[var(--text-muted)]">Memory Allocation</td>
                    <td className="py-2 px-3 text-[var(--text-primary)] font-medium">4.0 GB Max</td>
                    <td className="py-2 px-3 text-right">Spill-to-Disk</td>
                  </tr>
                  <tr>
                    <td className="py-2 px-3 text-[var(--text-muted)]">Worker Threads</td>
                    <td className="py-2 px-3 text-[var(--text-primary)] font-medium">4 Cores</td>
                    <td className="py-2 px-3 text-right">SIMD Vectorized</td>
                  </tr>
                  <tr>
                    <td className="py-2 px-3 text-[var(--text-muted)]">Target Dataset Size</td>
                    <td className="py-2 px-3 text-[var(--text-primary)] font-medium">
                      {fileMeta.rows ? `${formatNumber(fileMeta.rows)} records` : '—'}
                    </td>
                    <td className="py-2 px-3 text-right">Streaming</td>
                  </tr>
                  <tr>
                    <td className="py-2 px-3 text-[var(--text-muted)]">Pipeline Isolation</td>
                    <td className="py-2 px-3 text-[var(--text-primary)] font-medium">Single CTE Chain</td>
                    <td className="py-2 px-3 text-right">Zero-Copy</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </Disclosure>
        </div>
      </div>

      {/* Bottom Approval Action Bar */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 bg-[var(--bg-surface)] p-6 rounded-xl border border-[var(--border-hairline)] shadow-2xs">
        <div>
          <span className="font-semibold text-[var(--text-primary)] block text-base sm:text-lg tracking-tight">
            Ready to Clean Dataset?
          </span>
          <span className="text-xs sm:text-sm text-[var(--text-muted)]">
            {!allQuestionsAnswered
              ? `Please answer the ${initialOpenQuestions.length - Object.keys(questionAnswers).length} confirmation decisions above before proceeding to execution.`
              : 'All confirmations resolved. Execute deterministic cleaning pipeline in Step 4.'}
          </span>
        </div>

        <Button
          variant="primary"
          size="lg"
          disabled={cleaningTriggered || !allQuestionsAnswered || !recipe}
          onClick={() => {
            if (!allQuestionsAnswered) {
              const el = document.getElementById('human-confirmations-section');
              if (el) {
                el.scrollIntoView({ behavior: 'smooth', block: 'start' });
              }
              return;
            }
            handleApprove();
          }}
        >
          {cleaningTriggered
            ? 'Starting Cleaning...'
            : !allQuestionsAnswered
            ? `Answer Confirmations (${initialOpenQuestions.length - Object.keys(questionAnswers).length} remaining)`
            : 'Approve & Start Cleaning (Step 4)'}
        </Button>
      </div>
    </div>
  );
};
