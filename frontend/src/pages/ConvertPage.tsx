import React, { useRef, useState } from 'react';
import { Button } from '../components/ui/Button';
import { Callout } from '../components/ui/Callout';
import { SupportedFileFormats } from '../components/ui/SupportedFileFormats';
import { formatBytes, formatNumber } from '../lib/format';
import {
  detectFormat,
  executeConvertOnly,
  getConvertDownloadUrl,
  previewConvert,
  type ConvertResultResponse,
} from '../lib/api';
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Download,
  FileText,
  FileUp,
  Loader2,
  RefreshCw,
  Bot,
  Terminal,
  UploadCloud,
  BarChart3,
  Database,
} from 'lucide-react';
import { SqlQueryBox } from '../components/explore/SqlQueryBox';
import { ConnectorModal } from '../components/connectors/ConnectorModal';

interface ConvertPageProps {
  onHandoffToClean: (jobId: string, filename: string) => void;
  onHandoffToVisualize?: (convertId: string, filename: string, rowCount?: number) => void;
}

const TARGET_FORMATS = [
  { id: 'parquet', label: 'Parquet (Columnar, ZSTD)', ext: '.parquet', note: 'Recommended for big data & fastest queries' },
  { id: 'csv', label: 'CSV (Comma-Separated)', ext: '.csv', note: 'Universal compatibility with spreadsheets' },
  { id: 'tsv', label: 'TSV (Tab-Separated)', ext: '.tsv', note: 'Tab-delimited text' },
  { id: 'ndjson', label: 'NDJSON (Newline JSON)', ext: '.ndjson', note: 'Ideal for streaming & line-by-line processing' },
  { id: 'json', label: 'JSON (Array)', ext: '.json', note: 'Standard JSON array format' },
  { id: 'xlsx', label: 'Excel Workbook (.xlsx)', ext: '.xlsx', note: 'Splits automatically into parts if >1,048,575 rows' },
];

export const ConvertPage: React.FC<ConvertPageProps> = ({ onHandoffToClean, onHandoffToVisualize }) => {
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const [detectedFormat, setDetectedFormat] = useState<string>('');
  const [overrideFormat, setOverrideFormat] = useState<string>('');
  const [detectedConfidence, setDetectedConfidence] = useState<number>(0);

  // Source options
  const [sheetNames, setSheetNames] = useState<string[]>([]);
  const [selectedSheet, setSelectedSheet] = useState<string>('');
  const [tableNames, setTableNames] = useState<string[]>([]);
  const [selectedTable, setSelectedTable] = useState<string>('');

  // Preview & Schema
  const [previewRows, setPreviewRows] = useState<any[]>([]);
  const [previewReport, setPreviewReport] = useState<any | null>(null);

  // Target format & Conversion
  const [targetFormat, setTargetFormat] = useState<string>('parquet');
  const [converting, setConverting] = useState(false);
  const [convertProgressPct, setConvertProgressPct] = useState<number>(0);
  const [uploadedBytes, setUploadedBytes] = useState<number>(0);
  const [totalBytes, setTotalBytes] = useState<number>(0);
  const [isProcessing, setIsProcessing] = useState<boolean>(false);
  const [uploading, setUploading] = useState(false);
  const [uploadProgressPct, setUploadProgressPct] = useState<number>(0);
  const [convertResult, setConvertResult] = useState<ConvertResultResponse | null>(null);
  const [showExplore, setShowExplore] = useState<boolean>(false);
  const [showConnectorModal, setShowConnectorModal] = useState<boolean>(false);
  const [exploreInitialMode, setExploreInitialMode] = useState<'sql' | 'ask_ai'>('ask_ai');
  const [error, setError] = useState<{ message: string; recovery: string } | null>(null);

  const handleOpenExplore = (mode: 'sql' | 'ask_ai') => {
    if (showExplore && exploreInitialMode === mode) {
      setShowExplore(false);
    } else {
      setExploreInitialMode(mode);
      setShowExplore(true);
    }
  };

  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleFile = async (file: File) => {
    setSelectedFile(file);
    setError(null);
    setConvertResult(null);
    setUploading(true);
    setUploadProgressPct(35);

    try {
      const det = await detectFormat(file);
      setDetectedFormat(det.format);
      setOverrideFormat(det.format);
      setDetectedConfidence(det.confidence);
      if (det.sheets && det.sheets.length > 0) {
        setSheetNames(det.sheets);
        setSelectedSheet(det.sheets[0]);
      }
      if (det.tables && det.tables.length > 0) {
        setTableNames(det.tables);
        setSelectedTable(det.tables[0]);
      }

      setUploadProgressPct(70);
      // Load preview
      await fetchPreview(file, det.format, {
        sheet: det.sheets?.[0],
        table: det.tables?.[0],
      });
      setUploadProgressPct(100);
    } catch (err: any) {
      setError({
        message: err.message || 'Format detection failed',
        recovery: 'You can choose the format manually below.',
      });
    } finally {
      setUploading(false);
    }
  };

  const fetchPreview = async (file: File, fmt: string, opts?: Record<string, any>) => {
    try {
      const res = await previewConvert(file, fmt, opts);
      setPreviewRows(res.preview || []);
      setPreviewReport(res.report || null);
    } catch (err: any) {
      console.warn('Preview error:', err);
    }
  };

  const handleExecuteConvert = async () => {
    if (!selectedFile) return;
    setConverting(true);
    setConvertProgressPct(0);
    setUploadedBytes(0);
    setTotalBytes(selectedFile.size);
    setIsProcessing(false);
    setError(null);

    const srcOpts: Record<string, any> = {};
    if (selectedSheet) srcOpts.sheet = selectedSheet;
    if (selectedTable) srcOpts.table = selectedTable;

    try {
      const res = await executeConvertOnly(
        selectedFile,
        targetFormat,
        overrideFormat || detectedFormat,
        Object.keys(srcOpts).length > 0 ? srcOpts : undefined,
        undefined,
        (loaded, total) => {
          const actualTotal = total || selectedFile.size;
          setUploadedBytes(loaded);
          setTotalBytes(actualTotal);
          const pct = Math.min(100, Math.round((loaded / actualTotal) * 100));
          setConvertProgressPct(pct);
          if (loaded >= actualTotal) {
            setIsProcessing(true);
          }
        },
      );
      setConvertProgressPct(100);
      setIsProcessing(false);
      setConvertResult(res);
    } catch (err: any) {
      const isNetworkErr = err?.message?.toLowerCase().includes('failed to fetch') || err?.name === 'TypeError';
      setError({
        message: isNetworkErr ? 'Server connection error (Failed to fetch)' : (err.message || 'Conversion failed'),
        recovery: isNetworkErr
          ? 'Check your network connection and ensure the server is accessible.'
          : 'Verify input dataset validity or pick another target format like Parquet or CSV.',
      });
    } finally {
      setConverting(false);
      setIsProcessing(false);
    }
  };

  const handleReset = () => {
    setSelectedFile(null);
    setConvertResult(null);
    setPreviewRows([]);
    setPreviewReport(null);
    setError(null);
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  return (
    <div className="container-shell pt-6 sm:pt-8 pb-12 sm:pb-16 space-y-8">
      {/* Title & Mode Switch Note */}
      <div className="space-y-1.5">
        <div className="flex items-center gap-2.5">
          <h1 className="text-2xl sm:text-3xl font-semibold text-[var(--text-primary)] tracking-tight">
            Convert Data Format
          </h1>
        </div>
        <p className="text-sm sm:text-base text-[var(--text-secondary)] leading-relaxed">
          High-performance, in-process format conversion.
          Converts large datasets between CSV, TSV, Parquet, JSON, NDJSON, Excel (.xlsx), and SQLite without running profiling or altering values.
        </p>
      </div>

      {/* Error Callout */}
      {error && (
        <Callout
          variant="danger"
          title={error.message}
          action={
            <Button size="sm" variant="secondary" onClick={() => setError(null)}>
              Dismiss
            </Button>
          }
        >
          {error.recovery}
        </Callout>
      )}

      {/* 1. File Upload / Dropzone */}
      {!selectedFile ? (
        <div
          onDragEnter={(e) => { e.preventDefault(); setDragActive(true); }}
          onDragLeave={(e) => { e.preventDefault(); setDragActive(false); }}
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => {
            e.preventDefault();
            setDragActive(false);
            if (e.dataTransfer.files && e.dataTransfer.files[0]) {
              handleFile(e.dataTransfer.files[0]);
            }
          }}
          className={`bg-[var(--bg-surface)] hairline-border rounded-[var(--radius-md)] p-8 sm:p-10 text-center transition-all border-dashed ${
            dragActive
              ? 'border-[var(--accent)] bg-[var(--accent-subtle)]'
              : 'border-[var(--border-hairline)] hover:border-[var(--border-strong)]'
          }`}
        >
          <input
            ref={fileInputRef}
            type="file"
            accept=".csv,.tsv,.parquet,.json,.ndjson,.jsonl,.xlsx,.db,.sqlite"
            onChange={(e) => {
              if (e.target.files && e.target.files[0]) {
                handleFile(e.target.files[0]);
              }
            }}
            className="hidden"
            id="convert-file-input"
          />

          {uploading ? (
            <div className="space-y-5 py-4">
              <div className="w-14 h-14 rounded-full bg-[var(--accent-subtle)] text-[var(--accent)] flex items-center justify-center mx-auto">
                <UploadCloud className="w-7 h-7 animate-pulse" />
              </div>

              <div className="space-y-1.5">
                <span className="text-base sm:text-lg font-semibold text-[var(--text-primary)] block">
                  Reading dataset and detecting schema...
                </span>
                <span className="text-sm text-[var(--text-muted)] font-mono block">
                  {uploadProgressPct}% analyzed
                </span>
              </div>

              <div className="w-64 mx-auto bg-[var(--bg-subtle)] h-2 rounded-full overflow-hidden hairline-border">
                <div
                  className="bg-[var(--accent)] h-full transition-all duration-200"
                  style={{ width: `${uploadProgressPct}%` }}
                />
              </div>
            </div>
          ) : (
            <div className="space-y-5">
              <div className="w-16 h-16 rounded-2xl bg-[var(--bg-subtle)] border border-[var(--border-hairline)] shadow-sm flex items-center justify-center mx-auto">
                <FileUp className="w-8 h-8 text-[var(--accent)]" />
              </div>

              <div className="space-y-2">
                <span className="text-base sm:text-lg font-semibold text-[var(--text-primary)] block">
                  Drag and drop your dataset here to convert
                </span>
                <SupportedFileFormats />
              </div>

              <div className="pt-2 flex flex-wrap items-center justify-center gap-3">
                <Button
                  variant="primary"
                  size="lg"
                  onClick={() => fileInputRef.current?.click()}
                >
                  Select File to Convert
                </Button>

                <Button
                  variant="secondary"
                  size="lg"
                  onClick={() => setShowConnectorModal(true)}
                  leftIcon={<Database className="w-5 h-5 text-[#FF4B3E]" />}
                >
                  Connect S3 / PostgreSQL
                </Button>
              </div>
            </div>
          )}
        </div>
      ) : (
        /* 2. File Selected, Preview, Options & Target Picker */
        <div className="space-y-6">
          {/* File Header Bar */}
          <div className="bg-[var(--bg-surface)] hairline-border rounded-[var(--radius-md)] p-4 sm:p-5 flex flex-wrap items-center justify-between gap-4">
            <div className="flex items-center gap-3 min-w-0">
              <div className="w-10 h-10 rounded-[var(--radius-sm)] bg-[var(--bg-subtle)] text-[var(--text-secondary)] flex items-center justify-center shrink-0">
                <FileText className="w-5 h-5" />
              </div>
              <div className="min-w-0">
                <span className="text-sm sm:text-base font-semibold text-[var(--text-primary)] truncate block" title={selectedFile.name}>
                  {selectedFile.name}
                </span>
                <span className="text-xs text-[var(--text-muted)] font-mono">
                  {formatBytes(selectedFile.size)} · Detected: <strong className="text-[var(--text-primary)] uppercase">{detectedFormat}</strong> ({Math.round(detectedConfidence * 100)}% confidence)
                </span>
              </div>
            </div>

            <div className="flex items-center gap-2">
              <Button size="sm" variant="secondary" onClick={handleReset} leftIcon={<RefreshCw className="w-3.5 h-3.5" />}>
                Choose Different File
              </Button>
            </div>
          </div>

          {/* Options (Sheet / Table selector if applicable) */}
          {sheetNames.length > 1 && (
            <div className="bg-[var(--bg-surface)] hairline-border rounded-[var(--radius-md)] p-4 flex items-center justify-between gap-4">
              <span className="text-sm font-medium text-[var(--text-primary)]">
                Select Excel Sheet to Convert:
              </span>
              <select
                value={selectedSheet}
                onChange={(e) => {
                  setSelectedSheet(e.target.value);
                  fetchPreview(selectedFile, overrideFormat || detectedFormat, { sheet: e.target.value });
                }}
                className="bg-[var(--bg-subtle)] text-[var(--text-primary)] text-sm rounded-[var(--radius-sm)] px-3 py-1.5 hairline-border outline-none"
              >
                {sheetNames.map((s) => (
                  <option key={s} value={s}>{s}</option>
                ))}
              </select>
            </div>
          )}

          {tableNames.length > 1 && (
            <div className="bg-[var(--bg-surface)] hairline-border rounded-[var(--radius-md)] p-4 flex items-center justify-between gap-4">
              <span className="text-sm font-medium text-[var(--text-primary)]">
                Select SQLite Table to Convert:
              </span>
              <select
                value={selectedTable}
                onChange={(e) => {
                  setSelectedTable(e.target.value);
                  fetchPreview(selectedFile, overrideFormat || detectedFormat, { table: e.target.value });
                }}
                className="bg-[var(--bg-subtle)] text-[var(--text-primary)] text-sm rounded-[var(--radius-sm)] px-3 py-1.5 hairline-border outline-none"
              >
                {tableNames.map((t) => (
                  <option key={t} value={t}>{t}</option>
                ))}
              </select>
            </div>
          )}

          {/* Target Format Selection Table */}
          {!convertResult && (
            <div className="bg-[var(--bg-surface)] hairline-border rounded-[var(--radius-md)] overflow-hidden">
              <div className="p-3.5 sm:p-4 hairline-b bg-[var(--bg-subtle)] flex items-center justify-between">
                <div>
                  <span className="text-xs font-semibold uppercase tracking-wider text-[var(--text-muted)] block">
                    Target Format Selection
                  </span>
                  <span className="text-xs text-[var(--text-secondary)] mt-0.5">
                    Select the target serialization format for conversion
                  </span>
                </div>
                <span className="text-xs font-mono text-[var(--text-muted)]">
                  Active: <strong className="text-[var(--text-primary)] uppercase">{targetFormat}</strong>
                </span>
              </div>

              <div className="overflow-x-auto">
                <table className="w-full text-left border-collapse text-xs sm:text-sm">
                  <thead>
                    <tr className="hairline-b text-[var(--text-muted)] font-medium bg-[var(--bg-surface)]">
                      <th className="py-2.5 px-4 font-semibold text-xs uppercase tracking-wider w-[32%] sm:w-[28%]">
                        Target Format
                      </th>
                      <th className="py-2.5 px-4 font-semibold text-xs uppercase tracking-wider">
                        Capabilities & Output Specification
                      </th>
                      <th className="py-2.5 px-4 font-semibold text-xs uppercase tracking-wider text-right w-[20%] sm:w-[16%]">
                        Selection
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--border-hairline)]">
                    {TARGET_FORMATS.map((tgt) => {
                      const isSelected = targetFormat === tgt.id;
                      return (
                        <tr
                          key={tgt.id}
                          onClick={() => setTargetFormat(tgt.id)}
                          className={`cursor-pointer transition-colors ${
                            isSelected
                              ? 'bg-[var(--accent-subtle)]/40 hover:bg-[var(--accent-subtle)]/60'
                              : 'hover:bg-[var(--bg-hover)]'
                          }`}
                        >
                          <td className="py-3 px-4 align-middle">
                            <div className="flex flex-col gap-0.5">
                              <span className={`font-semibold text-xs sm:text-sm ${isSelected ? 'text-[var(--accent)]' : 'text-[var(--text-primary)]'}`}>
                                {tgt.label}
                              </span>
                              <span className="font-mono text-xs text-[var(--text-muted)]">
                                format: <span className="text-[var(--text-secondary)] font-medium">{tgt.id}</span>
                              </span>
                            </div>
                          </td>

                          <td className="py-3 px-4 align-middle text-xs sm:text-sm text-[var(--text-secondary)]">
                            {tgt.note}
                          </td>

                          <td className="py-3 px-4 align-middle text-right whitespace-nowrap">
                            <div className="inline-flex items-center gap-1.5 font-medium text-xs sm:text-sm">
                              {isSelected ? (
                                <>
                                  <CheckCircle2 className="w-4 h-4 text-[var(--status-ok)] shrink-0" />
                                  <span className="text-[var(--status-ok)] font-semibold">Selected</span>
                                </>
                              ) : (
                                <span className="text-[var(--text-muted)] hover:text-[var(--text-primary)]">Select</span>
                              )}
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>

              {/* Active Conversion Progress Loading Bar */}
              {converting && (
                <div className="p-4 sm:p-5 hairline-t bg-[var(--bg-subtle)] space-y-3">
                  <div className="flex items-center justify-between text-xs sm:text-sm">
                    <div className="flex items-center gap-2.5">
                      <Loader2 className="w-4 h-4 text-[var(--accent)] animate-spin shrink-0" />
                      <span className="font-semibold text-[var(--text-primary)]">
                        {isProcessing
                          ? `Streaming & converting ${selectedFile.name} to ${targetFormat.toUpperCase()}...`
                          : `Uploading ${selectedFile.name}...`}
                      </span>
                    </div>
                    <span className="font-mono text-xs font-semibold text-[var(--accent)]">
                      {Math.round(convertProgressPct)}%
                    </span>
                  </div>

                  <div className="w-full bg-[var(--bg-surface)] h-2.5 rounded-full overflow-hidden hairline-border">
                    <div
                      className={`bg-[var(--accent)] h-full transition-all duration-200 ease-out ${
                        isProcessing ? 'animate-pulse' : ''
                      }`}
                      style={{ width: `${Math.max(convertProgressPct, 6)}%` }}
                    />
                  </div>

                  <div className="flex items-center justify-between text-xs text-[var(--text-muted)] font-mono pt-0.5">
                    <span>
                      {isProcessing
                        ? 'Converting via streaming engine · Zero-copy'
                        : `${formatBytes(uploadedBytes)} of ${formatBytes(totalBytes || selectedFile.size)}`}
                    </span>
                    <span>
                      {isProcessing ? 'Writing output...' : 'Transferring payload'}
                    </span>
                  </div>
                </div>
              )}

              {/* Action Button Footer */}
              <div className="p-4 hairline-t bg-[var(--bg-subtle)] flex items-center justify-between">
                <span className="text-xs text-[var(--text-muted)] font-mono">
                  Engine: In-process streaming
                </span>
                <Button
                  variant="primary"
                  size="lg"
                  disabled={converting}
                  onClick={handleExecuteConvert}
                  leftIcon={converting ? <Loader2 className="w-5 h-5 animate-spin" /> : <ArrowRight className="w-5 h-5" />}
                >
                  {converting ? 'Converting Dataset...' : `Convert to ${targetFormat.toUpperCase()}`}
                </Button>
              </div>
            </div>
          )}

          {/* 3. Conversion Completed Screen */}
          {convertResult && (
            <div className="bg-[var(--bg-surface)] border border-[var(--border-hairline)] rounded-xl p-6 sm:p-8 space-y-6 shadow-2xs">
              <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-5 border-b border-[var(--border-hairline)]">
                <div className="flex items-center gap-3">
                  <div className="w-11 h-11 rounded-xl bg-[var(--bg-subtle)] border border-[var(--border-hairline)] text-emerald-400 flex items-center justify-center shrink-0">
                    <CheckCircle2 className="w-6 h-6" />
                  </div>
                  <div>
                    <h2 className="text-xl sm:text-2xl font-semibold text-[var(--text-primary)] tracking-tight">
                      Conversion Completed
                    </h2>
                    <p className="text-xs sm:text-sm text-[var(--text-muted)] font-mono">
                      Dataset staged and converted via columnar streaming
                    </p>
                  </div>
                </div>

                <div className="flex flex-wrap items-center gap-3">
                  <a
                    href={getConvertDownloadUrl(convertResult.convert_id)}
                    download
                    className="inline-flex items-center gap-2 px-5 py-2.5 rounded-lg text-sm font-semibold bg-[#FF4B3E] hover:bg-[#E03E32] text-white shadow-xs transition-all cursor-pointer"
                  >
                    <Download className="w-4 h-4" />
                    <span>Download {convertResult.result.target_format.toUpperCase()}</span>
                  </a>

                  <Button
                    variant="secondary"
                    size="md"
                    onClick={() => onHandoffToClean(convertResult.convert_id, selectedFile.name)}
                    rightIcon={<ArrowRight className="w-4 h-4 text-[var(--text-muted)]" />}
                  >
                    Clean this file too
                  </Button>
                </div>
              </div>

              {/* Horizontal Metrics Grid */}
              <div className="grid grid-cols-2 lg:grid-cols-4 gap-3.5 sm:gap-4">
                <div className="bg-[var(--bg-subtle)] border border-[var(--border-hairline)] rounded-xl p-4 sm:p-5 flex flex-col justify-between">
                  <span className="text-xs font-medium uppercase tracking-wider text-[var(--text-muted)] block font-sans">
                    Converted Rows
                  </span>
                  <span className="text-2xl sm:text-3xl font-semibold font-mono block text-[var(--text-primary)] tracking-tight mt-1.5">
                    {formatNumber(convertResult.result.rows)}
                  </span>
                  <span className="text-xs text-[var(--text-muted)] mt-2 block font-sans">
                    100% records preserved
                  </span>
                </div>

                <div className="bg-[var(--bg-subtle)] border border-[var(--border-hairline)] rounded-xl p-4 sm:p-5 flex flex-col justify-between">
                  <span className="text-xs font-medium uppercase tracking-wider text-[var(--text-muted)] block font-sans">
                    Elapsed Duration
                  </span>
                  <span className="text-2xl sm:text-3xl font-semibold font-mono block text-[var(--text-primary)] tracking-tight mt-1.5">
                    {convertResult.result.elapsed_seconds}s
                  </span>
                  <span className="text-xs text-[var(--text-muted)] mt-2 block font-sans">
                    Vectorized streaming throughput
                  </span>
                </div>

                <div className="bg-[var(--bg-subtle)] border border-[var(--border-hairline)] rounded-xl p-4 sm:p-5 flex flex-col justify-between">
                  <span className="text-xs font-medium uppercase tracking-wider text-[var(--text-muted)] block font-sans">
                    Target Format
                  </span>
                  <span className="text-2xl sm:text-3xl font-semibold font-mono block text-[var(--text-primary)] tracking-tight mt-1.5">
                    {convertResult.result.target_format.toUpperCase()}
                  </span>
                  <span className="text-xs text-[var(--text-muted)] mt-2 block font-sans">
                    Zero-copy adapter output
                  </span>
                </div>

                <div className="bg-[var(--bg-subtle)] border border-[var(--border-hairline)] rounded-xl p-4 sm:p-5 flex flex-col justify-between">
                  <span className="text-xs font-medium uppercase tracking-wider text-[var(--text-muted)] block font-sans">
                    Columns Preserved
                  </span>
                  <span className="text-2xl sm:text-3xl font-semibold font-mono block text-[var(--text-primary)] tracking-tight mt-1.5">
                    {formatNumber(convertResult.result.columns.length)}
                  </span>
                  <span className="text-xs text-[var(--text-muted)] mt-2 block font-sans">
                    100% schema preserved
                  </span>
                </div>
              </div>

              {/* Fidelity & Schema Validation Notices */}
              {convertResult.result.warnings.length > 0 && (
                <div className="bg-[var(--bg-surface)] border border-[var(--border-hairline)] rounded-xl overflow-hidden shadow-2xs">
                  <div className="p-3.5 sm:p-4 border-b border-[var(--border-hairline)] bg-[var(--bg-subtle)] flex items-center justify-between">
                    <span className="text-xs font-semibold uppercase tracking-wider text-[var(--text-muted)] font-mono">
                      Fidelity & Schema Notices
                    </span>
                    <span className="text-xs text-[var(--text-muted)] font-mono">
                      {convertResult.result.warnings.length} notices
                    </span>
                  </div>
                  <div className="overflow-x-auto">
                    <table className="w-full text-left border-collapse text-xs sm:text-sm">
                      <thead>
                        <tr className="border-b border-[var(--border-hairline)] text-[var(--text-muted)] font-medium bg-[var(--bg-surface)]">
                          <th className="py-2.5 px-4 font-semibold text-xs uppercase tracking-wider w-[36%] sm:w-[32%] font-mono">
                            Notice / Target
                          </th>
                          <th className="py-2.5 px-4 font-semibold text-xs uppercase tracking-wider font-mono">
                            Details & Action Taken
                          </th>
                          <th className="py-2.5 px-4 font-semibold text-xs uppercase tracking-wider text-right w-[20%] sm:w-[18%] font-mono">
                            Status
                          </th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-[var(--border-hairline)]">
                        {convertResult.result.warnings.map((w, idx) => (
                          <tr key={idx} className="hover:bg-[var(--bg-hover)] transition-colors">
                            <td className="py-3 px-4 align-top">
                              <span className="font-semibold text-xs sm:text-sm text-[var(--text-primary)] block">
                                Notice #{idx + 1}
                              </span>
                              <span className="font-mono text-xs text-[var(--text-muted)]">
                                adapter: <span className="text-[var(--text-secondary)] font-medium">{convertResult.result.target_format}</span>
                              </span>
                            </td>
                            <td className="py-3 px-4 align-top text-xs sm:text-sm text-[var(--text-secondary)] leading-relaxed">
                              {w}
                            </td>
                            <td className="py-3 px-4 align-top text-right whitespace-nowrap">
                              <div className="inline-flex items-center gap-1.5 font-medium text-xs sm:text-sm font-mono text-amber-400">
                                <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
                                <span>Notice</span>
                              </div>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              {/* Prominent Feature Block: Query & Inspect Converted Dataset */}
              {!showExplore && (
                <div className="p-6 rounded-xl bg-[var(--bg-surface)] border border-[var(--border-hairline)] flex flex-col md:flex-row md:items-center justify-between gap-6 mt-4 shadow-2xs">
                  <div className="flex items-start gap-4">
                    <div className="w-11 h-11 rounded-xl bg-[var(--bg-subtle)] border border-[var(--border-hairline)] text-[#FF4B3E] flex items-center justify-center shrink-0 mt-0.5">
                      <Bot className="w-5 h-5" />
                    </div>
                    <div className="space-y-1">
                      <h3 className="text-base sm:text-lg font-semibold text-[var(--text-primary)] tracking-tight">
                        Inspect, Query, or Visualize your Data
                      </h3>
                      <p className="text-sm text-[var(--text-secondary)] max-w-xl leading-relaxed">
                        Ask questions in natural English with our AI Analyst, write custom SQL queries, or generate instant interactive charts from your converted data.
                      </p>
                    </div>
                  </div>

                  <div className="flex flex-wrap items-center gap-3 shrink-0">
                    <button
                      type="button"
                      onClick={() => handleOpenExplore('ask_ai')}
                      className="inline-flex items-center gap-2.5 px-5 py-2.5 rounded-lg text-sm font-semibold bg-[#FF4B3E] hover:bg-[#E03E32] text-white shadow-xs transition-all cursor-pointer"
                    >
                      <Bot className="w-4 h-4" />
                      <span>Ask AI Analyst</span>
                    </button>

                    <button
                      type="button"
                      onClick={() => handleOpenExplore('sql')}
                      className="inline-flex items-center gap-2.5 px-5 py-2.5 rounded-lg text-sm font-semibold bg-[var(--bg-subtle)] hover:bg-[var(--bg-hover)] text-[var(--text-secondary)] hover:text-[var(--text-primary)] border border-[var(--border-hairline)] transition-all cursor-pointer"
                    >
                      <Terminal className="w-4 h-4 text-[var(--text-muted)]" />
                      <span>SQL Query Box</span>
                    </button>

                    <button
                      type="button"
                      onClick={() => onHandoffToVisualize?.(convertResult.convert_id, selectedFile?.name || 'converted dataset', convertResult.result.rows)}
                      className="inline-flex items-center gap-2.5 px-5 py-2.5 rounded-lg text-sm font-semibold bg-[var(--bg-subtle)] hover:bg-[var(--bg-hover)] text-[var(--text-secondary)] hover:text-[var(--text-primary)] border border-[var(--border-hairline)] transition-all cursor-pointer"
                    >
                      <BarChart3 className="w-4 h-4 text-[#FF4B3E]" />
                      <span>Visualize Data</span>
                    </button>
                  </div>
                </div>
              )}

              {/* Conditional SQL / AI Query Box for converted dataset */}
              {showExplore && (
                <div className="pt-4 hairline-t">
                  <SqlQueryBox
                    jobId={convertResult.convert_id}
                    datasetName={selectedFile?.name || 'converted dataset'}
                    initialMode={exploreInitialMode}
                    onClose={() => setShowExplore(false)}
                  />
                </div>
              )}

              {/* Privacy Line */}
              <p className="text-xs text-[var(--text-muted)] text-center pt-2">
                Your converted files are kept isolated in temporary workspace storage and deleted automatically within 24 hours.
              </p>
            </div>
          )}

          {/* 4. Preview Table (first 100 rows) */}
          {previewRows.length > 0 && (
            <div className="bg-[var(--bg-surface)] hairline-border rounded-[var(--radius-md)] overflow-hidden">
              <div className="p-4 hairline-b bg-[var(--bg-subtle)] flex items-center justify-between">
                <span className="text-sm font-semibold text-[var(--text-primary)]">
                  Dataset Sample Preview (First 100 Rows)
                </span>
                {previewReport && (
                  <span className="text-xs text-[var(--text-muted)] font-mono">
                    Total: {formatNumber(previewReport.rows)} rows · {previewReport.columns.length} columns
                  </span>
                )}
              </div>

              <div className="overflow-x-auto max-h-[360px]">
                <table className="w-full text-left border-collapse text-xs">
                  <thead className="sticky top-0 bg-[var(--bg-subtle)] hairline-b text-[var(--text-muted)] font-medium">
                    <tr>
                      {Object.keys(previewRows[0] || {}).map((col) => (
                        <th key={col} className="py-2.5 px-3 font-semibold uppercase tracking-wider">
                          {col}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--border-hairline)] font-mono">
                    {previewRows.slice(0, 10).map((row, rIdx) => (
                      <tr key={rIdx} className="hover:bg-[var(--bg-hover)]">
                        {Object.values(row).map((val: any, cIdx) => (
                          <td key={cIdx} className="py-2 px-3 text-[var(--text-secondary)] truncate max-w-[200px]">
                            {val !== null && val !== undefined ? String(val) : <span className="text-[var(--text-placeholder)]">null</span>}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Cloud & Database Connector Ingestion Modal */}
      <ConnectorModal
        isOpen={showConnectorModal}
        onClose={() => setShowConnectorModal(false)}
        mode="import"
        onImportSuccess={(info) => {
          onHandoffToClean(info.id, info.name);
        }}
      />
    </div>
  );
};
