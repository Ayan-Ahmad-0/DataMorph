import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Check,
  ChevronDown,
  Code2,
  Copy,
  Download,
  FileCode2,
  Loader2,
  RefreshCw,
  X,
} from 'lucide-react';
import {
  downloadExportedCodeZip,
  exportJobAsCode,
  type ExportCodeRequest,
  type ExportCodeResponse,
  type ExportedFile,
  type ExportTarget,
  type SqlDialect,
} from '../../lib/api';
import { Button } from '../ui/Button';
import { Callout } from '../ui/Callout';
import { Tag } from '../ui/Tag';
import { Toggle } from '../ui/Toggle';

export interface ExportCodeModalProps {
  isOpen: boolean;
  jobId: string;
  datasetName?: string;
  onClose: () => void;
}

const TARGET_OPTIONS: Array<{ id: ExportTarget; label: string }> = [
  { id: 'sql', label: 'SQL' },
  { id: 'dbt', label: 'dbt' },
  { id: 'pandas', label: 'pandas' },
  { id: 'polars', label: 'Polars' },
];

const DIALECT_OPTIONS: Array<{ id: SqlDialect; label: string }> = [
  { id: 'duckdb', label: 'DuckDB' },
  { id: 'snowflake', label: 'Snowflake' },
  { id: 'bigquery', label: 'BigQuery' },
  { id: 'postgres', label: 'Postgres' },
];

const SQL_KEYWORDS = new Set([
  'WITH', 'SELECT', 'FROM', 'AS', 'WHERE', 'QUALIFY', 'OVER', 'PARTITION',
  'BY', 'ORDER', 'ASC', 'DESC', 'CASE', 'WHEN', 'THEN', 'ELSE', 'END',
  'AND', 'OR', 'NOT', 'IN', 'IS', 'NULL', 'TRUE', 'FALSE', 'CAST',
  'TRY_CAST', 'TRY_STRPTIME', 'COALESCE', 'NULLIF', 'LEAST', 'GREATEST',
  'REPLACE', 'TRIM', 'LOWER', 'UPPER', 'SUBSTR', 'ROW_NUMBER', 'MEDIAN',
  'MODE', 'LIMIT', 'GROUP', 'HAVING', 'JOIN', 'ON', 'LEFT', 'INNER',
  'UNION', 'ALL', 'DISTINCT', 'DOUBLE', 'BIGINT', 'INTEGER', 'BOOLEAN',
  'VARCHAR', 'DATE', 'TIMESTAMP', 'SAFE_CAST', 'REGEXP_REPLACE',
]);

const PYTHON_KEYWORDS = new Set([
  'import', 'from', 'as', 'def', 'return', 'if', 'else', 'elif', 'for',
  'in', 'not', 'and', 'or', 'is', 'None', 'True', 'False', 'with', 'try',
  'except', 'raise', 'pass', 'lambda', 'class',
]);

interface Token {
  text: string;
  className?: string;
}

function tokenizeLine(line: string, language: ExportedFile['language']): Token[] {
  if (!line) return [];

  if (language === 'yaml') {
    const commentIdx = line.indexOf('#');
    const codePart = commentIdx >= 0 ? line.slice(0, commentIdx) : line;
    const commentPart = commentIdx >= 0 ? line.slice(commentIdx) : '';
    const tokens: Token[] = [];

    const keyMatch = /^(\s*(?:-\s+)?)([A-Za-z0-9_.-]+)(:)(.*)$/.exec(codePart);
    if (keyMatch) {
      if (keyMatch[1]) tokens.push({ text: keyMatch[1] });
      tokens.push({ text: keyMatch[2], className: 'text-[var(--accent-text)] font-medium' });
      tokens.push({ text: keyMatch[3] });
      if (keyMatch[4]) {
        const val = keyMatch[4];
        const trimmed = val.trim();
        if (/^(true|false|null|\d+(?:\.\d+)?)$/i.test(trimmed)) {
          tokens.push({ text: val, className: 'text-[var(--status-warn-text)]' });
        } else if (/^['"].*['"]$/.test(trimmed)) {
          tokens.push({ text: val, className: 'text-[var(--status-ok-text)]' });
        } else {
          tokens.push({ text: val });
        }
      }
    } else if (codePart) {
      tokens.push({ text: codePart });
    }

    if (commentPart) {
      tokens.push({ text: commentPart, className: 'text-[var(--text-muted)] italic' });
    }
    return tokens;
  }

  const pattern =
    language === 'sql'
      ? /(--.*$|\{\{.*?\}\}|'(?:''|[^'])*'|"(?:""|[^"])*"|\b\d+(?:\.\d+)?\b|\b[A-Za-z_][A-Za-z0-9_]*\b)/g
      : /(#.*$|f?"""[\s\S]*?"""|f?'''[\s\S]*?'''|f?"(?:\\.|[^"])*"|f?'(?:\\.|[^'])*'|\b\d+(?:\.\d+)?\b|\b[A-Za-z_][A-Za-z0-9_]*\b)/g;

  const tokens: Token[] = [];
  let lastIndex = 0;

  for (const match of line.matchAll(pattern)) {
    const idx = match.index ?? 0;
    if (idx > lastIndex) {
      tokens.push({ text: line.slice(lastIndex, idx) });
    }

    const value = match[0];
    let className: string | undefined;

    if (
      (language === 'sql' && value.startsWith('--')) ||
      (language === 'python' && value.startsWith('#'))
    ) {
      className = 'text-[var(--text-muted)] italic';
    } else if (language === 'sql' && value.startsWith('{{')) {
      className = 'text-[var(--status-warn-text)] font-medium';
    } else if (
      value.startsWith("'") ||
      value.startsWith('"') ||
      value.startsWith("f'") ||
      value.startsWith('f"')
    ) {
      className = 'text-[var(--status-ok-text)]';
    } else if (/^\d/.test(value)) {
      className = 'text-[var(--status-warn-text)]';
    } else if (
      (language === 'sql' && SQL_KEYWORDS.has(value.toUpperCase())) ||
      (language === 'python' && PYTHON_KEYWORDS.has(value))
    ) {
      className = 'text-[var(--accent-text)] font-medium';
    }

    tokens.push({ text: value, className });
    lastIndex = idx + value.length;
  }

  if (lastIndex < line.length) {
    tokens.push({ text: line.slice(lastIndex) });
  }

  return tokens;
}

function renderHighlightedCode(content: string, language: ExportedFile['language']): React.ReactNode {
  const lines = content.split('\n');
  return lines.map((line, lineIdx) => {
    const tokens = tokenizeLine(line, language);
    return (
      <React.Fragment key={lineIdx}>
        {tokens.map((tok, tokIdx) =>
          tok.className ? (
            <span key={tokIdx} className={tok.className}>
              {tok.text}
            </span>
          ) : (
            <React.Fragment key={tokIdx}>{tok.text}</React.Fragment>
          ),
        )}
        {lineIdx < lines.length - 1 ? '\n' : null}
      </React.Fragment>
    );
  });
}

function triggerSingleFileDownload(file: ExportedFile): void {
  const blob = new Blob([file.content], { type: 'text/plain;charset=utf-8' });
  const url = window.URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = file.name;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.URL.revokeObjectURL(url);
}

/**
 * Modal for previewing, copying, and downloading exported cleaning code
 * across SQL, dbt, pandas, and Polars targets.
 */
export const ExportCodeModal: React.FC<ExportCodeModalProps> = ({
  isOpen,
  jobId,
  datasetName,
  onClose,
}) => {
  const [target, setTarget] = useState<ExportTarget>('sql');
  const [dialect, setDialect] = useState<SqlDialect>('duckdb');
  const [includeTests, setIncludeTests] = useState<boolean>(true);
  const [sourceName, setSourceName] = useState<string>('');
  const [debouncedSourceName, setDebouncedSourceName] = useState<string>('');

  const [response, setResponse] = useState<ExportCodeResponse | null>(null);
  const [activeFileIndex, setActiveFileIndex] = useState<number>(0);
  const [loading, setLoading] = useState<boolean>(false);
  const [downloading, setDownloading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<boolean>(false);

  const supportsDialect = target === 'sql' || target === 'dbt';
  const supportsTests = target === 'dbt';

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedSourceName(sourceName.trim());
    }, 250);
    return () => window.clearTimeout(timer);
  }, [sourceName]);

  const requestPayload = useMemo<ExportCodeRequest>(() => {
    const req: ExportCodeRequest = { target };
    if (supportsDialect) {
      req.dialect = dialect;
    }
    if (supportsTests) {
      req.include_tests = includeTests;
    }
    if (debouncedSourceName) {
      req.source_name = debouncedSourceName;
    }
    return req;
  }, [target, supportsDialect, dialect, supportsTests, includeTests, debouncedSourceName]);

  const fetchExportedCode = useCallback(async () => {
    if (!jobId) return;
    setLoading(true);
    setError(null);
    setCopied(false);
    try {
      const data = await exportJobAsCode(jobId, requestPayload);
      setResponse(data);
      setActiveFileIndex(0);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to generate exported code.';
      setError(message);
    } finally {
      setLoading(false);
    }
  }, [jobId, requestPayload]);

  useEffect(() => {
    if (!isOpen || !jobId) return;
    void fetchExportedCode();
  }, [isOpen, jobId, fetchExportedCode]);

  useEffect(() => {
    if (!isOpen) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  const files = response?.files || [];
  const activeFile = files[activeFileIndex] || files[0] || null;
  const warnings = response?.warnings || [];
  const isMultiFile = files.length > 1;

  const handleCopy = async () => {
    if (!activeFile?.content) return;
    try {
      await navigator.clipboard.writeText(activeFile.content);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setError('Unable to copy code to clipboard.');
    }
  };

  const handleDownload = async () => {
    if (!response || files.length === 0) return;
    if (isMultiFile) {
      setDownloading(true);
      setError(null);
      try {
        await downloadExportedCodeZip(
          jobId,
          requestPayload,
          `datamorph_export_${target}.zip`,
        );
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : 'Failed to download code archive.';
        setError(message);
      } finally {
        setDownloading(false);
      }
      return;
    }

    if (activeFile) {
      triggerSingleFileDownload(activeFile);
    }
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="export-code-modal-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-xs p-4"
    >
      <div className="w-full max-w-3xl bg-[var(--bg-surface)] border border-[var(--border-hairline)] rounded-xl shadow-2xl overflow-hidden flex flex-col max-h-[88vh]">
        {/* Header */}
        <div className="px-5 py-4 border-b border-[var(--border-hairline)] bg-[var(--bg-subtle)] flex items-center justify-between gap-3">
          <div className="flex items-center gap-2.5 min-w-0">
            <Code2 className="w-4 h-4 text-[#FF4B3E] shrink-0" />
            <h3
              id="export-code-modal-title"
              className="text-sm font-semibold text-[var(--text-primary)] tracking-tight"
            >
              Export as Code
            </h3>
            {datasetName && (
              <Tag variant="neutral" font="mono" className="truncate max-w-[220px]">
                {datasetName}
              </Tag>
            )}
          </div>
          <button
            type="button"
            aria-label="Close modal"
            onClick={onClose}
            className="p-1 rounded text-[var(--text-muted)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-hover)] transition-colors cursor-pointer"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Target Selector */}
        <div className="px-5 py-3 bg-[var(--bg-app)] border-b border-[var(--border-hairline)] flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium text-[var(--text-secondary)]">Target</span>
            <div
              role="group"
              aria-label="Export target"
              className="inline-flex items-center p-0.5 rounded-lg bg-[var(--bg-subtle)] border border-[var(--border-hairline)]"
            >
              {TARGET_OPTIONS.map((opt) => {
                const isSelected = target === opt.id;
                return (
                  <button
                    key={opt.id}
                    type="button"
                    aria-pressed={isSelected}
                    onClick={() => setTarget(opt.id)}
                    className={`px-3 py-1.5 rounded-md text-xs font-medium transition-colors cursor-pointer ${
                      isSelected
                        ? 'bg-[var(--bg-surface)] text-[var(--text-primary)] border border-[var(--border-strong)] shadow-2xs font-semibold'
                        : 'text-[var(--text-muted)] hover:text-[var(--text-primary)] border border-transparent'
                    }`}
                  >
                    {opt.label}
                  </button>
                );
              })}
            </div>
          </div>

          {supportsTests && (
            <Toggle
              id="export-include-tests"
              checked={includeTests}
              onChange={setIncludeTests}
              label="Include data quality tests"
            />
          )}
        </div>

        {/* Target Options (Dialect + Optional Source Table) */}
        <div className="px-5 py-3 bg-[var(--bg-surface)] border-b border-[var(--border-hairline)] grid grid-cols-1 sm:grid-cols-2 gap-3">
          {supportsDialect && (
            <div className="flex flex-col gap-1">
              <label
                htmlFor="export-dialect-select"
                className="text-xs font-medium text-[var(--text-secondary)]"
              >
                Dialect
              </label>
              <div className="relative">
                <select
                  id="export-dialect-select"
                  value={dialect}
                  onChange={(e) => setDialect(e.target.value as SqlDialect)}
                  className="w-full appearance-none bg-[var(--bg-app)] border border-[var(--border-hairline)] focus:border-[var(--accent)] rounded-lg pl-3 pr-8 py-1.5 text-xs font-medium text-[var(--text-primary)] outline-none cursor-pointer transition-colors"
                >
                  {DIALECT_OPTIONS.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.label}
                    </option>
                  ))}
                </select>
                <ChevronDown className="w-3.5 h-3.5 text-[var(--text-muted)] absolute right-2.5 top-1/2 -translate-y-1/2 pointer-events-none" />
              </div>
            </div>
          )}

          <div className={`flex flex-col gap-1 ${!supportsDialect ? 'sm:col-span-2' : ''}`}>
            <label
              htmlFor="export-source-name"
              className="text-xs font-medium text-[var(--text-secondary)]"
            >
              Source table
            </label>
            <input
              id="export-source-name"
              type="text"
              value={sourceName}
              onChange={(e) => setSourceName(e.target.value)}
              placeholder="raw.orders (optional)"
              className="w-full bg-[var(--bg-app)] border border-[var(--border-hairline)] focus:border-[var(--accent)] rounded-lg px-3 py-1.5 text-xs font-mono text-[var(--text-primary)] placeholder:text-[var(--text-placeholder)] outline-none transition-colors"
            />
          </div>
        </div>

        {/* Multi-file Tab Bar (dbt) */}
        {isMultiFile && !loading && !error && (
          <div
            role="tablist"
            aria-label="Exported files"
            className="flex items-center border-b border-[var(--border-hairline)] bg-[var(--bg-app)] px-5 pt-1.5 gap-2 text-xs font-mono overflow-x-auto"
          >
            {files.map((file, idx) => {
              const isActive = idx === activeFileIndex;
              return (
                <button
                  key={file.name}
                  type="button"
                  role="tab"
                  aria-selected={isActive}
                  onClick={() => setActiveFileIndex(idx)}
                  className={`flex items-center gap-1.5 px-3 py-2 border-b-2 font-medium transition-colors cursor-pointer whitespace-nowrap ${
                    isActive
                      ? 'border-[#FF4B3E] text-[var(--text-primary)] bg-[var(--bg-surface)]'
                      : 'border-transparent text-[var(--text-muted)] hover:text-[var(--text-secondary)]'
                  }`}
                >
                  <FileCode2 className="w-3.5 h-3.5 shrink-0" />
                  <span>{file.name}</span>
                </button>
              );
            })}
          </div>
        )}

        {/* Body: Warnings + Code Preview / Loading / Error */}
        <div className="flex-1 p-5 overflow-y-auto space-y-4 custom-scrollbar">
          {loading ? (
            <div className="py-20 flex items-center justify-center gap-2 text-xs text-[var(--text-muted)]">
              <Loader2 className="w-4 h-4 animate-spin text-[#FF4B3E]" />
              <span>Generating {target} code...</span>
            </div>
          ) : error ? (
            <Callout
              variant="danger"
              title="Failed to generate code"
              action={
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => void fetchExportedCode()}
                  leftIcon={<RefreshCw className="w-3.5 h-3.5" />}
                >
                  Retry
                </Button>
              }
            >
              <span className="font-mono text-xs">{error}</span>
            </Callout>
          ) : (
            <>
              {warnings.length > 0 && (
                <Callout variant="warn" title="Compatibility notices">
                  <ul className="list-disc pl-4 space-y-1 text-xs">
                    {warnings.map((warning, idx) => (
                      <li key={idx}>{warning}</li>
                    ))}
                  </ul>
                </Callout>
              )}

              {activeFile ? (
                <div className="space-y-2">
                  <div className="flex items-center justify-between text-xs">
                    <span className="font-mono font-medium text-[var(--text-secondary)]">
                      {activeFile.name}
                    </span>
                    <Tag variant="neutral" font="mono">
                      {activeFile.language}
                    </Tag>
                  </div>
                  <pre className="p-4 rounded-lg bg-[var(--bg-app)] border border-[var(--border-hairline)] text-xs font-mono text-[var(--text-primary)] overflow-x-auto whitespace-pre leading-relaxed custom-scrollbar">
                    <code>{renderHighlightedCode(activeFile.content, activeFile.language)}</code>
                  </pre>
                </div>
              ) : (
                <div className="py-12 text-center text-xs text-[var(--text-muted)]">
                  No generated files returned.
                </div>
              )}
            </>
          )}
        </div>

        {/* Footer */}
        <div className="px-5 py-3.5 border-t border-[var(--border-hairline)] bg-[var(--bg-subtle)] flex items-center justify-between gap-3">
          <span className="text-xs text-[var(--text-muted)] font-mono truncate">
            {isMultiFile
              ? `${files.length} files (${files.map((f) => f.name).join(', ')})`
              : activeFile?.name || 'Standalone cleaning script'}
          </span>

          <div className="flex items-center gap-2 shrink-0">
            <Button size="sm" variant="ghost" onClick={onClose}>
              Close
            </Button>
            <Button
              size="sm"
              variant="secondary"
              onClick={() => void handleCopy()}
              disabled={loading || !activeFile?.content}
              leftIcon={
                copied ? (
                  <Check className="w-3.5 h-3.5 text-emerald-500" />
                ) : (
                  <Copy className="w-3.5 h-3.5" />
                )
              }
            >
              {copied ? 'Copied' : 'Copy'}
            </Button>
            <Button
              size="sm"
              variant="primary"
              onClick={() => void handleDownload()}
              disabled={loading || downloading || files.length === 0}
              leftIcon={
                downloading ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <Download className="w-3.5 h-3.5" />
                )
              }
            >
              {isMultiFile ? 'Download (.zip)' : 'Download'}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
};
