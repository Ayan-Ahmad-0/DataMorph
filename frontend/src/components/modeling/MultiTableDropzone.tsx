import React, { useRef, useState } from 'react';
import { UploadCloud, Loader2 } from 'lucide-react';
import { SupportedFileFormats } from '../ui/SupportedFileFormats';

interface MultiTableDropzoneProps {
  onUploadFiles: (files: File[]) => Promise<void>;
  isUploading?: boolean;
}

export const MultiTableDropzone: React.FC<MultiTableDropzoneProps> = ({
  onUploadFiles,
  isUploading,
}) => {
  const [isDragOver, setIsDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleDrop = async (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(false);
    if (isUploading) return;

    const files = Array.from(e.dataTransfer.files);
    if (files.length > 0) {
      await onUploadFiles(files);
    }
  };

  const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      const files = Array.from(e.target.files);
      await onUploadFiles(files);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        setIsDragOver(true);
      }}
      onDragLeave={() => setIsDragOver(false)}
      onDrop={handleDrop}
      onClick={() => !isUploading && fileInputRef.current?.click()}
      className={`border-2 border-dashed rounded-xl p-5 text-center transition-all cursor-pointer select-none ${
        isDragOver
          ? 'border-[#FF4B3E] bg-[#FF4B3E]/5'
          : 'border-[var(--border-hairline)] hover:border-[var(--text-muted)] bg-[var(--bg-surface)]'
      }`}
    >
      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept=".csv,.tsv,.parquet,.json,.ndjson,.xlsx,.db,.sqlite,.sqlite3"
        onChange={handleFileSelect}
        className="hidden"
      />

      <div className="flex flex-col items-center gap-2">
        <div className="w-10 h-10 rounded-xl bg-[var(--bg-subtle)] border border-[var(--border-hairline)] text-[#FF4B3E] flex items-center justify-center">
          {isUploading ? (
            <Loader2 className="w-5 h-5 animate-spin" />
          ) : (
            <UploadCloud className="w-5 h-5" />
          )}
        </div>

        <div className="space-y-2">
          <span className="text-xs font-semibold text-[var(--text-primary)] block">
            {isUploading ? 'Staging & profiling tables...' : 'Drop multiple datasets here'}
          </span>
          <SupportedFileFormats
            sizeLimit="Up to 5 GB per file"
            formats={['CSV', 'TSV', 'Parquet', 'JSON', 'NDJSON', 'Excel (.xlsx)', 'SQLite (.db / .sqlite3)']}
          />
        </div>
      </div>
    </div>
  );
};
