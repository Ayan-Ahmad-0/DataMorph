import React, { useRef, useState } from 'react';
import { Button } from '../components/ui/Button';
import { Callout } from '../components/ui/Callout';
import { Tag } from '../components/ui/Tag';
import { formatBytes, formatNumber } from '../lib/format';
import {
  detectFormat,
  uploadForQuery,
  type QueryUploadResult,
} from '../lib/api';
import {
  ArrowRight,
  Database,
  FileText,
  FileUp,
  RefreshCw,
} from 'lucide-react';
import { SqlQueryBox } from '../components/explore/SqlQueryBox';
import { ConnectorModal } from '../components/connectors/ConnectorModal';
import { MAX_FILE_SIZE_BYTES } from '../config';

interface QueryPageProps {
  initialDataset?: QueryUploadResult | null;
  initialSql?: string;
  initialMode?: 'sql' | 'ask_ai';
  onHandoffToClean: (jobId: string, filename: string) => void;
}

export const QueryPage: React.FC<QueryPageProps> = ({
  initialDataset = null,
  initialSql,
  initialMode = 'ask_ai',
  onHandoffToClean,
}) => {
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [detectedFormat, setDetectedFormat] = useState<string>('');
  const [sheetNames, setSheetNames] = useState<string[]>([]);
  const [selectedSheet, setSelectedSheet] = useState<string>('');
  const [tableNames, setTableNames] = useState<string[]>([]);
  const [selectedTable, setSelectedTable] = useState<string>('');

  const [dragActive, setDragActive] = useState<boolean>(false);
  const [isUploading, setIsUploading] = useState<boolean>(false);
  const [uploadProgress, setUploadProgress] = useState<number>(0);
  const [uploadedBytes, setUploadedBytes] = useState<number>(0);
  const [totalBytes, setTotalBytes] = useState<number>(0);
  const [uploadStage, setUploadStage] = useState<'inspecting' | 'uploading' | 'converting'>('uploading');
  const [error, setError] = useState<string | null>(null);

  const [queryResult, setQueryResult] = useState<QueryUploadResult | null>(initialDataset || null);

  React.useEffect(() => {
    if (initialDataset) {
      setQueryResult(initialDataset);
    }
  }, [initialDataset]);
  const [showConnectorModal, setShowConnectorModal] = useState<boolean>(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleDrag = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.type === 'dragenter' || e.type === 'dragover') {
      setDragActive(true);
    } else if (e.type === 'dragleave') {
      setDragActive(false);
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(false);
    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      handleFileSelected(e.dataTransfer.files[0]);
    }
  };

  const handleFileInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      handleFileSelected(e.target.files[0]);
    }
  };

  const handleFileSelected = async (file: File) => {
    setError(null);
    setSelectedFile(file);
    setSheetNames([]);
    setSelectedSheet('');
    setTableNames([]);
    setSelectedTable('');
    setTotalBytes(file.size);
    setUploadedBytes(0);
    setUploadProgress(0);

    const ext = file.name.slice(file.name.lastIndexOf('.')).toLowerCase();
    const supportedExts = ['.csv', '.tsv', '.parquet', '.json', '.ndjson', '.jsonl', '.xlsx', '.db', '.sqlite'];

    if (!supportedExts.includes(ext)) {
      setError(`Unsupported file type '${ext}'. Please upload a CSV, TSV, Parquet, JSON, NDJSON, Excel (.xlsx), or SQLite (.db) file.`);
      return;
    }

    if (file.size > MAX_FILE_SIZE_BYTES) {
      setError(`File size (${formatBytes(file.size)}) exceeds the maximum allowed limit of 5 GB.`);
      return;
    }

    // Direct staging for tabular files (CSV, TSV, Parquet, JSON, NDJSON)
    const requiresInspection = ['.xlsx', '.db', '.sqlite'].includes(ext);
    if (!requiresInspection) {
      await processAndStageFile(file);
      return;
    }

    setIsUploading(true);
    setUploadStage('inspecting');
    try {
      const detectRes = await detectFormat(file);
      setIsUploading(false);
      setDetectedFormat(detectRes.format);

      if (detectRes.sheets && detectRes.sheets.length > 1) {
        setSheetNames(detectRes.sheets);
        setSelectedSheet(detectRes.sheets[0]);
      } else if (detectRes.tables && detectRes.tables.length > 1) {
        setTableNames(detectRes.tables);
        setSelectedTable(detectRes.tables[0]);
      } else {
        await processAndStageFile(file);
      }
    } catch {
      setIsUploading(false);
      await processAndStageFile(file);
    }
  };

  const processAndStageFile = async (
    file: File,
    customOptions?: { sheet?: string; table?: string }
  ) => {
    setError(null);
    setIsUploading(true);
    setUploadStage('uploading');
    setTotalBytes(file.size);
    setUploadedBytes(0);
    setUploadProgress(0);

    try {
      const options = customOptions || (
        selectedSheet ? { sheet: selectedSheet } : selectedTable ? { table: selectedTable } : undefined
      );

      const res = await uploadForQuery(file, options, (loaded, total) => {
        setUploadedBytes(loaded);
        const actualTotal = total || file.size;
        setTotalBytes(actualTotal);
        const pct = Math.min(99, Math.round((loaded / actualTotal) * 100));
        setUploadProgress(pct);
        if (pct >= 99) {
          setUploadStage('converting');
        }
      });

      setUploadProgress(100);
      setQueryResult(res);
    } catch (err: any) {
      setError(err.message || 'Failed to stage dataset for querying.');
    } finally {
      setIsUploading(false);
    }
  };

  const handleReset = () => {
    setSelectedFile(null);
    setQueryResult(null);
    setDetectedFormat('');
    setSheetNames([]);
    setTableNames([]);
    setSelectedSheet('');
    setSelectedTable('');
    setUploadedBytes(0);
    setTotalBytes(0);
    setUploadProgress(0);
    setError(null);
    if (fileInputRef.current) {
      fileInputRef.current.value = '';
    }
  };

  return (
    <div className="container-shell py-8 sm:py-12 space-y-8">
      {/* Header section */}
      <div className="flex flex-col sm:flex-row sm:items-start justify-between gap-4">
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2.5">
            <h1 className="text-2xl sm:text-3xl font-semibold text-[var(--text-primary)] tracking-tight">
              Query Your Data
            </h1>
          </div>
          <p className="text-sm sm:text-base text-[var(--text-secondary)] leading-relaxed max-w-2xl">
            Upload any dataset to ask questions in natural English with our AI Analyst or write sandboxed SQL queries directly against your data without cleaning or converting.
          </p>
        </div>

        {queryResult && (
          <div className="flex items-center gap-3 shrink-0">
            <Button
              variant="secondary"
              size="md"
              leftIcon={<RefreshCw className="w-4 h-4" />}
              onClick={handleReset}
            >
              Query Different File
            </Button>
            <Button
              variant="secondary"
              size="md"
              rightIcon={<ArrowRight className="w-4 h-4" />}
              onClick={() => onHandoffToClean(queryResult.query_id, queryResult.filename)}
            >
              Clean this file too
            </Button>
          </div>
        )}
      </div>

      {/* Error Callout */}
      {error && (
        <Callout
          variant="danger"
          title="Upload or Staging Error"
          action={
            <Button size="sm" variant="secondary" onClick={() => setError(null)}>
              Dismiss
            </Button>
          }
        >
          <p className="text-xs">{error}</p>
        </Callout>
      )}

      {/* 1. Upload & Staging View (when no dataset is loaded yet) */}
      {!queryResult ? (
        <div className="space-y-6">
          {/* Disambiguation for multi-sheet Excel / multi-table SQLite */}
          {selectedFile && !isUploading && (sheetNames.length > 1 || tableNames.length > 1) && (
            <div className="p-5 rounded-[var(--radius-md)] bg-[var(--bg-surface)] hairline-border space-y-4">
              <div className="flex items-center gap-2 text-sm font-semibold text-[var(--text-primary)]">
                <Database className="w-4 h-4 text-[var(--accent)]" />
                <span>
                  {sheetNames.length > 1
                    ? 'Select an Excel Sheet to Query:'
                    : 'Select a SQLite Table to Query:'}
                </span>
                {detectedFormat && (
                  <Tag variant="neutral" font="mono">
                    {detectedFormat.toUpperCase()}
                  </Tag>
                )}
              </div>
              <div className="flex flex-wrap items-center gap-3">
                <select
                  value={selectedSheet || selectedTable}
                  onChange={(e) => {
                    if (sheetNames.length > 1) setSelectedSheet(e.target.value);
                    else setSelectedTable(e.target.value);
                  }}
                  className="bg-[var(--bg-subtle)] text-[var(--text-primary)] text-sm rounded-[var(--radius-sm)] px-3.5 py-2 hairline-border outline-none"
                >
                  {(sheetNames.length > 1 ? sheetNames : tableNames).map((item) => (
                    <option key={item} value={item}>{item}</option>
                  ))}
                </select>
                <Button
                  variant="primary"
                  size="md"
                  onClick={() => processAndStageFile(selectedFile)}
                >
                  Start Exploring
                </Button>
              </div>
            </div>
          )}

          {/* Upload Dropzone */}
          <div
            onDragEnter={handleDrag}
            onDragLeave={handleDrag}
            onDragOver={handleDrag}
            onDrop={handleDrop}
            className={`dropzone-glow bg-[var(--bg-surface)] rounded-[var(--radius-lg)] p-10 sm:p-14 text-center transition-all duration-200 border border-dashed cursor-pointer ${
              dragActive
                ? 'drag-active border-[var(--accent)] bg-[var(--accent-subtle)]'
                : 'border-[var(--border-hairline)] hover:border-[var(--border-strong)]'
            }`}
            style={{
              backgroundImage: dragActive
                ? undefined
                : 'radial-gradient(ellipse at center top, var(--bg-subtle) 0%, transparent 70%)',
            }}
            onClick={() => !isUploading && fileInputRef.current?.click()}
          >
            <input
              ref={fileInputRef}
              type="file"
              className="hidden"
              onChange={handleFileInputChange}
              onClick={(e) => {
                (e.target as HTMLInputElement).value = '';
              }}
              accept=".csv,.tsv,.parquet,.json,.ndjson,.jsonl,.xlsx,.db,.sqlite"
            />

            {isUploading ? (
              <div className="py-6 space-y-4 max-w-sm mx-auto" onClick={(e) => e.stopPropagation()}>
                <div className="w-12 h-12 rounded-full border-2 border-[var(--accent)] border-t-transparent animate-spin mx-auto" />
                <div className="space-y-1 text-center">
                  <span className="text-sm font-semibold text-[var(--text-primary)] block">
                    {uploadStage === 'inspecting'
                      ? 'Inspecting dataset schema...'
                      : uploadStage === 'uploading'
                      ? 'Streaming dataset to sandbox...'
                      : 'Indexing & preparing sandbox...'}
                  </span>
                  <span className="text-xs text-[var(--text-muted)] font-mono block">
                    {uploadStage === 'uploading'
                      ? `${formatBytes(uploadedBytes)} of ${formatBytes(totalBytes || selectedFile?.size || 0)} (${uploadProgress}%)`
                      : '99% completed'}
                  </span>
                </div>
                <div className="w-full bg-[var(--bg-subtle)] h-2 rounded-full overflow-hidden border border-[var(--border-hairline)] shadow-inner">
                  <div
                    className={`h-full transition-all duration-200 rounded-full ${
                      uploadStage === 'converting' ? 'bg-[var(--accent)] animate-pulse' : 'bg-[var(--accent)]'
                    }`}
                    style={{ width: `${uploadProgress}%` }}
                  />
                </div>
              </div>
            ) : (
              <div className="space-y-5">
                <div className="w-16 h-16 rounded-2xl bg-[var(--bg-subtle)] border border-[var(--border-hairline)] shadow-sm flex items-center justify-center mx-auto">
                  <FileUp className="w-8 h-8 text-[var(--accent)]" />
                </div>

                <div className="space-y-1.5">
                  <span className="text-base sm:text-lg font-semibold text-[var(--text-primary)] block">
                    Drag and drop your dataset here to query
                  </span>
                  <span className="text-sm text-[var(--text-muted)] block">
                    CSV, TSV, Parquet, JSON, NDJSON, Excel (.xlsx), or SQLite (.db) up to 5 GB
                  </span>
                </div>

                <div className="pt-2 flex flex-wrap items-center justify-center gap-3">
                  <Button
                    variant="primary"
                    size="lg"
                    onClick={(e) => {
                      e.stopPropagation();
                      fileInputRef.current?.click();
                    }}
                  >
                    Select File to Query
                  </Button>

                  <Button
                    variant="secondary"
                    size="lg"
                    onClick={(e) => {
                      e.stopPropagation();
                      setShowConnectorModal(true);
                    }}
                    leftIcon={<Database className="w-5 h-5 text-[#FF4B3E]" />}
                  >
                    Connect S3 / PostgreSQL
                  </Button>
                </div>
              </div>
            )}
          </div>
        </div>
      ) : (
        /* 2. Active Query & Exploration Workspace */
        <div className="space-y-6">
          {/* Active Dataset Stat Bar */}
          <div className="bg-[var(--bg-surface)] border border-[var(--border-hairline)] rounded-xl p-4 sm:p-5 flex flex-wrap items-center justify-between gap-4 shadow-2xs">
            <div className="flex items-center gap-3.5 min-w-0">
              <div className="w-10 h-10 rounded-xl bg-[var(--bg-subtle)] border border-[var(--border-hairline)] text-[#FF4B3E] flex items-center justify-center shrink-0">
                <FileText className="w-5 h-5" />
              </div>
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-sm sm:text-base font-semibold text-[var(--text-primary)] truncate block tracking-tight" title={queryResult.filename}>
                    {queryResult.filename}
                  </span>
                  <Tag variant="neutral" font="mono">
                    {queryResult.detected_format ? queryResult.detected_format.toUpperCase() : 'DATASET'}
                  </Tag>
                </div>
                <span className="text-xs text-[var(--text-muted)] font-mono">
                  {formatBytes(queryResult.file_size_bytes)} · {formatNumber(queryResult.row_count)} rows · {queryResult.columns.length} columns
                </span>
              </div>
            </div>

            <div className="flex items-center gap-3">
              <Button
                size="sm"
                variant="secondary"
                leftIcon={<RefreshCw className="w-3.5 h-3.5 text-[var(--text-muted)]" />}
                onClick={handleReset}
              >
                Query Another File
              </Button>
              <Button
                size="sm"
                variant="secondary"
                rightIcon={<ArrowRight className="w-3.5 h-3.5 text-[var(--text-muted)]" />}
                onClick={() => onHandoffToClean(queryResult.query_id, queryResult.filename)}
              >
                Clean this file too
              </Button>
            </div>
          </div>

          {/* Main SQL & AI Analyst Explorer Component */}
          <SqlQueryBox
            jobId={queryResult.query_id}
            datasetName={queryResult.filename}
            initialMode={initialMode}
            initialSql={initialSql}
          />
        </div>
      )}

      {/* Cloud & Database Connector Ingestion Modal */}
      <ConnectorModal
        isOpen={showConnectorModal}
        onClose={() => setShowConnectorModal(false)}
        mode="import"
        onImportSuccess={(info) => {
          setQueryResult({
            query_id: info.id,
            filename: info.name,
            file_size_bytes: 0,
            row_count: parseInt(info.rowCount.replace(/[^0-9]/g, '')) || 0,
            columns: [],
          });
        }}
      />
    </div>
  );
};
