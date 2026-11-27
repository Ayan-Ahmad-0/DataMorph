import React from 'react';

const DEFAULT_FORMATS = ['CSV', 'TSV', 'Parquet', 'JSON', 'NDJSON', 'Excel (.xlsx)', 'SQLite (.db)'];

interface SupportedFileFormatsProps {
  sizeLimit?: string;
  formats?: string[];
}

export const SupportedFileFormats: React.FC<SupportedFileFormatsProps> = ({
  sizeLimit = 'Files up to 5 GB',
  formats = DEFAULT_FORMATS,
}) => (
  <div className="space-y-2">
    <div className="flex flex-wrap justify-center gap-2" aria-label="Supported file formats">
      {formats.map((format) => (
        <span className="format-pill" key={format}>
          {format}
        </span>
      ))}
    </div>
    <span className="text-xs text-[var(--text-muted)] block">
      {sizeLimit}
    </span>
  </div>
);
