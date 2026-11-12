import React, { useState } from 'react';
import {
  Key,
  Link,
  Trash2,
  GripHorizontal,
  Search,
} from 'lucide-react';
import type { TableMetadata } from '../../lib/api';

interface TableEntityCardProps {
  table: TableMetadata;
  isSelected?: boolean;
  onSelect?: () => void;
  onDelete?: () => void;
  onStartConnect?: (colName: string) => void;
  onEndConnect?: (colName: string) => void;
  connectingFromCol?: string | null;
  onMouseDownDrag?: (e: React.MouseEvent) => void;
}

export const TableEntityCard: React.FC<TableEntityCardProps> = React.memo(({
  table,
  isSelected,
  onSelect,
  onDelete,
  onStartConnect,
  onEndConnect,
  connectingFromCol,
  onMouseDownDrag,
}) => {
  const [filter, setFilter] = useState('');

  const filteredColumns = table.columns.filter((col) =>
    col.name.toLowerCase().includes(filter.toLowerCase())
  );

  const getDataTypeBadge = (type: string) => {
    const t = type.toUpperCase();
    if (t.includes('INT') || t.includes('BIGINT') || t.includes('FLOAT') || t.includes('DOUBLE') || t.includes('NUMERIC')) {
      return <span className="text-[10px] font-mono px-1.5 py-0.2 rounded bg-blue-500/10 text-blue-400 border border-blue-500/20">{t}</span>;
    }
    if (t.includes('DATE') || t.includes('TIME')) {
      return <span className="text-[10px] font-mono px-1.5 py-0.2 rounded bg-amber-500/10 text-amber-400 border border-amber-500/20">{t}</span>;
    }
    if (t.includes('BOOL')) {
      return <span className="text-[10px] font-mono px-1.5 py-0.2 rounded bg-purple-500/10 text-purple-400 border border-purple-500/20">{t}</span>;
    }
    return <span className="text-[10px] font-mono px-1.5 py-0.2 rounded bg-[var(--bg-subtle)] text-[var(--text-muted)] border border-[var(--border-hairline)]">{t}</span>;
  };

  return (
    <div
      onClick={onSelect}
      style={{
        transform: `translate3d(${table.position_x}px, ${table.position_y}px, 0)`,
      }}
      className={`absolute w-72 rounded-xl bg-[var(--bg-surface)] border shadow-md select-none transition-shadow ${
        isSelected
          ? 'border-[#FF4B3E] ring-2 ring-[#FF4B3E]/20'
          : 'border-[var(--border-hairline)] hover:border-[var(--text-muted)]'
      }`}
    >
      {/* Draggable Card Header */}
      <div
        onMouseDown={onMouseDownDrag}
        className="px-3.5 py-2.5 bg-[var(--bg-subtle)] border-b border-[var(--border-hairline)] rounded-t-xl flex items-center justify-between cursor-grab active:cursor-grabbing"
      >
        <div className="flex items-center gap-2 min-w-0">
          <GripHorizontal className="w-3.5 h-3.5 text-[var(--text-muted)] shrink-0" />
          <div className="min-w-0">
            <div className="flex items-center gap-1.5">
              <span className="font-mono text-xs font-bold text-[var(--text-primary)] truncate" title={table.table_name}>
                {table.table_name}
              </span>
            </div>
            <div className="text-[10px] text-[var(--text-muted)] flex items-center gap-1.5">
              <span>{table.row_count.toLocaleString()} rows</span>
              <span>·</span>
              <span>{table.columns.length} cols</span>
            </div>
          </div>
        </div>

        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onDelete?.();
          }}
          className="p-1 rounded text-[var(--text-muted)] hover:text-red-400 hover:bg-red-500/10 transition-colors cursor-pointer"
          title={`Remove table ${table.table_name}`}
        >
          <Trash2 className="w-3.5 h-3.5" />
        </button>
      </div>

      {/* Column Search Filter (if > 6 columns) */}
      {table.columns.length > 6 && (
        <div className="p-2 border-b border-[var(--border-hairline)] bg-[var(--bg-app)]/50">
          <div className="relative flex items-center">
            <Search className="w-3 h-3 text-[var(--text-muted)] absolute left-2" />
            <input
              type="text"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter columns..."
              className="w-full pl-6 pr-2 py-0.5 text-[11px] bg-[var(--bg-surface)] border border-[var(--border-hairline)] rounded text-[var(--text-primary)] placeholder-[var(--text-muted)] focus:outline-none focus:border-[#FF4B3E]"
            />
          </div>
        </div>
      )}

      {/* Column List */}
      <div className="max-h-64 overflow-y-auto divide-y divide-[var(--border-hairline)]/50">
        {filteredColumns.map((col) => {
          const isConnectSource = connectingFromCol === `${table.table_name}.${col.name}`;

          return (
            <div
              key={col.name}
              id={`col-${table.table_name}-${col.name}`}
              className={`group flex items-center justify-between px-3 py-1.5 text-xs hover:bg-[var(--bg-hover)] transition-colors ${
                isConnectSource ? 'bg-[#FF4B3E]/10' : ''
              }`}
            >
              <div className="flex items-center gap-1.5 min-w-0 pr-2">
                {col.is_pk ? (
                  <span title="Primary Key" className="shrink-0 text-amber-400 font-mono text-[10px] font-bold flex items-center gap-0.5">
                    <Key className="w-3 h-3" />
                  </span>
                ) : col.is_fk ? (
                  <span title="Foreign Key" className="shrink-0 text-cyan-400 font-mono text-[10px] font-bold flex items-center gap-0.5">
                    <Link className="w-3 h-3" />
                  </span>
                ) : (
                  <span className="w-3 shrink-0" />
                )}

                <span
                  className="font-mono text-xs text-[var(--text-secondary)] group-hover:text-[var(--text-primary)] truncate"
                  title={col.name}
                >
                  {col.name}
                </span>
              </div>

              <div className="flex items-center gap-1.5 shrink-0">
                {getDataTypeBadge(col.data_type)}

                {/* Connection Anchor Dot */}
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    if (connectingFromCol) {
                      onEndConnect?.(col.name);
                    } else {
                      onStartConnect?.(col.name);
                    }
                  }}
                  className={`w-4 h-4 rounded-full flex items-center justify-center transition-all ${
                    isConnectSource
                      ? 'bg-[#FF4B3E] ring-2 ring-[#FF4B3E]/40 text-white'
                      : 'bg-[var(--bg-subtle)] text-[var(--text-muted)] hover:bg-[#FF4B3E] hover:text-white border border-[var(--border-hairline)]'
                  }`}
                  title={connectingFromCol ? `Connect to ${col.name}` : `Drag relationship from ${col.name}`}
                >
                  <span className="w-1.5 h-1.5 rounded-full bg-current" />
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
});
