import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ZoomIn,
  ZoomOut,
  RotateCcw,
  Sparkles,
} from 'lucide-react';
import type { TableMetadata, Relationship } from '../../lib/api';
import { TableEntityCard } from './TableEntityCard';

interface ErdCanvasProps {
  tables: Record<string, TableMetadata>;
  relationships: Relationship[];
  onUpdateTablePosition: (tableName: string, x: number, y: number) => void;
  onDeleteTable: (tableName: string) => void;
  onSelectRelationship: (rel: Relationship) => void;
  onConnectColumns: (fromTable: string, fromCol: string, toTable: string, toCol: string) => void;
  onAutoArrange?: () => void;
}

export const ErdCanvas: React.FC<ErdCanvasProps> = ({
  tables,
  relationships,
  onUpdateTablePosition,
  onDeleteTable,
  onSelectRelationship,
  onConnectColumns,
  onAutoArrange,
}) => {
  const containerRef = useRef<HTMLDivElement>(null);

  // Pan and Zoom State
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [isPanning, setIsPanning] = useState(false);
  const [panStart, setPanStart] = useState({ x: 0, y: 0 });

  // Dragging Table State
  const [draggingTable, setDraggingTable] = useState<string | null>(null);
  const [dragStartPos, setDragStartPos] = useState({ x: 0, y: 0 });
  const [initialTablePos, setInitialTablePos] = useState({ x: 0, y: 0 });

  // Connecting Columns State
  const [connectingSource, setConnectingSource] = useState<{ table: string; col: string } | null>(null);

  // Selected table
  const [selectedTable, setSelectedTable] = useState<string | null>(null);

  // Keep refs to volatile state so memoized card callbacks remain stable during pan/zoom/drag
  const tablesRef = useRef(tables);
  tablesRef.current = tables;
  const connectingSourceRef = useRef(connectingSource);
  connectingSourceRef.current = connectingSource;
  const onDeleteTableRef = useRef(onDeleteTable);
  onDeleteTableRef.current = onDeleteTable;
  const onConnectColumnsRef = useRef(onConnectColumns);
  onConnectColumnsRef.current = onConnectColumns;

  // Non-passive native wheel listener to prevent whole-page browser zoom on Ctrl+Wheel or trackpad pinch
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const handleNativeWheel = (e: WheelEvent) => {
      // Crucial: Cancels native browser webpage zoom
      e.preventDefault();
      e.stopPropagation();

      const rect = container.getBoundingClientRect();
      const mouseX = e.clientX - rect.left;
      const mouseY = e.clientY - rect.top;

      // Smooth zoom scaling towards cursor
      const zoomFactor = e.deltaY < 0 ? 1.08 : 0.92;

      setZoom((prevZoom) => {
        const newZoom = Math.min(Math.max(0.35, prevZoom * zoomFactor), 2.5);

        // Adjust pan coordinates so the point under the cursor stays fixed in world space
        setPan((prevPan) => {
          const worldX = (mouseX - prevPan.x) / prevZoom;
          const worldY = (mouseY - prevPan.y) / prevZoom;
          return {
            x: Math.round(mouseX - worldX * newZoom),
            y: Math.round(mouseY - worldY * newZoom),
          };
        });

        return newZoom;
      });
    };

    container.addEventListener('wheel', handleNativeWheel, { passive: false });

    return () => {
      container.removeEventListener('wheel', handleNativeWheel);
    };
  }, []);

  // Canvas Pan Handlers
  const handleMouseDownBackground = (e: React.MouseEvent) => {
    // Only pan if clicking on empty background
    if (e.target === containerRef.current || (e.target as HTMLElement).tagName === 'svg') {
      setIsPanning(true);
      setPanStart({ x: e.clientX - pan.x, y: e.clientY - pan.y });
      setConnectingSource(null);
    }
  };

  const handleMouseMove = (e: React.MouseEvent) => {
    if (isPanning) {
      setPan({ x: e.clientX - panStart.x, y: e.clientY - panStart.y });
    } else if (draggingTable && tables[draggingTable]) {
      const deltaX = (e.clientX - dragStartPos.x) / zoom;
      const deltaY = (e.clientY - dragStartPos.y) / zoom;
      const newX = Math.round(initialTablePos.x + deltaX);
      const newY = Math.round(initialTablePos.y + deltaY);
      onUpdateTablePosition(draggingTable, newX, newY);
    }
  };

  const handleMouseUp = () => {
    setIsPanning(false);
    setDraggingTable(null);
  };

  const startDraggingTable = useCallback((tableName: string, e: React.MouseEvent) => {
    e.stopPropagation();
    const tbl = tablesRef.current[tableName];
    setDraggingTable(tableName);
    setDragStartPos({ x: e.clientX, y: e.clientY });
    setInitialTablePos({
      x: tbl?.position_x || 0,
      y: tbl?.position_y || 0,
    });
  }, []);

  // Column connection logic
  const handleStartConnect = useCallback((tableName: string, colName: string) => {
    setConnectingSource((prev) => prev ?? { table: tableName, col: colName });
  }, []);

  const handleEndConnect = useCallback((tableName: string, colName: string) => {
    const source = connectingSourceRef.current;
    if (source && source.table !== tableName) {
      onConnectColumnsRef.current(source.table, source.col, tableName, colName);
    }
    setConnectingSource(null);
  }, []);

  // Precompute Bezier edge geometry only when tables or relationships change (not on pan/zoom/hover)
  const relationshipEdges = useMemo(() => {
    const columnIndicesByTable: Record<string, Map<string, number>> = {};
    for (const [tblName, tbl] of Object.entries(tables)) {
      const idxMap = new Map<string, number>();
      tbl.columns.forEach((c, idx) => idxMap.set(c.name, idx));
      columnIndicesByTable[tblName] = idxMap;
    }

    const getAnchor = (tableName: string, colName: string, isSource: boolean) => {
      const tbl = tables[tableName];
      if (!tbl) return { x: 0, y: 0 };
      const colIndex = columnIndicesByTable[tableName]?.get(colName) ?? -1;
      const headerHeight = 44;
      const searchHeight = tbl.columns.length > 6 ? 36 : 0;
      const rowHeight = 28;
      const yOffset = headerHeight + searchHeight + (colIndex >= 0 ? colIndex * rowHeight + 14 : 20);
      const cardWidth = 288; // w-72 = 18rem = 288px
      return {
        x: isSource ? tbl.position_x + cardWidth : tbl.position_x,
        y: tbl.position_y + yOffset,
      };
    };

    return relationships.flatMap((rel) => {
      const fromTable = tables[rel.from_table];
      const toTable = tables[rel.to_table];
      if (!fromTable || !toTable) return [];

      const isFromOnLeft = fromTable.position_x < toTable.position_x;
      const startAnchor = getAnchor(rel.from_table, rel.from_column, isFromOnLeft);
      const endAnchor = getAnchor(rel.to_table, rel.to_column, !isFromOnLeft);

      const x1 = startAnchor.x;
      const y1 = startAnchor.y;
      const x2 = endAnchor.x;
      const y2 = endAnchor.y;

      const dx = Math.abs(x2 - x1);
      const controlDist = Math.max(40, dx * 0.45);
      const cx1 = isFromOnLeft ? x1 + controlDist : x1 - controlDist;
      const cx2 = isFromOnLeft ? x2 - controlDist : x2 + controlDist;

      return [{
        rel,
        pathD: `M ${x1} ${y1} C ${cx1} ${y1}, ${cx2} ${y2}, ${x2} ${y2}`,
        midX: (x1 + x2) / 2,
        midY: (y1 + y2) / 2,
      }];
    });
  }, [relationships, tables]);

  const tableNamesKey = Object.keys(tables).join('\0');
  const tableList = useMemo(() => Object.values(tables), [tables]);
  const perTableHandlers = useMemo(() => {
    const map: Record<
      string,
      {
        onSelect: () => void;
        onDelete: () => void;
        onStartConnect: (col: string) => void;
        onEndConnect: (col: string) => void;
        onMouseDownDrag: (e: React.MouseEvent) => void;
      }
    > = {};
    for (const tableName of tableNamesKey ? tableNamesKey.split('\0') : []) {
      map[tableName] = {
        onSelect: () => setSelectedTable(tableName),
        onDelete: () => onDeleteTableRef.current(tableName),
        onStartConnect: (col) => handleStartConnect(tableName, col),
        onEndConnect: (col) => handleEndConnect(tableName, col),
        onMouseDownDrag: (e) => startDraggingTable(tableName, e),
      };
    }
    return map;
  }, [tableNamesKey, startDraggingTable, handleStartConnect, handleEndConnect]);

  const connectingFromCol = connectingSource
    ? `${connectingSource.table}.${connectingSource.col}`
    : null;

  const resetView = () => {
    setZoom(1);
    setPan({ x: 0, y: 0 });
  };

  return (
    <div
      ref={containerRef}
      onMouseDown={handleMouseDownBackground}
      onMouseMove={handleMouseMove}
      onMouseUp={handleMouseUp}
      className="relative w-full h-full min-h-[580px] bg-[var(--bg-app)] overflow-hidden cursor-crosshair select-none"
      style={{
        backgroundImage: `radial-gradient(var(--border-hairline) 1px, transparent 1px)`,
        backgroundSize: `${24 * zoom}px ${24 * zoom}px`,
        backgroundPosition: `${pan.x}px ${pan.y}px`,
      }}
    >
      {/* Top Floating Control Bar */}
      <div className="absolute top-4 right-4 z-20 flex items-center gap-1.5 p-1.5 bg-[var(--bg-surface)] border border-[var(--border-hairline)] rounded-xl shadow-md">
        <button
          type="button"
          onClick={() => setZoom((z) => Math.min(2.0, z + 0.15))}
          className="p-1.5 rounded-lg text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-hover)] transition-colors cursor-pointer"
          title="Zoom In"
        >
          <ZoomIn className="w-4 h-4" />
        </button>
        <button
          type="button"
          onClick={() => setZoom((z) => Math.max(0.4, z - 0.15))}
          className="p-1.5 rounded-lg text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-hover)] transition-colors cursor-pointer"
          title="Zoom Out"
        >
          <ZoomOut className="w-4 h-4" />
        </button>
        <div className="w-[1px] h-4 bg-[var(--border-hairline)]" />
        <button
          type="button"
          onClick={resetView}
          className="p-1.5 rounded-lg text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-hover)] transition-colors cursor-pointer"
          title="Reset View"
        >
          <RotateCcw className="w-4 h-4" />
        </button>
        {onAutoArrange && (
          <button
            type="button"
            onClick={onAutoArrange}
            className="flex items-center gap-1 px-2 py-1 rounded-lg text-xs font-medium text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-hover)] transition-colors cursor-pointer"
            title="Auto-arrange entity layout"
          >
            <Sparkles className="w-3.5 h-3.5 text-[#FF4B3E]" />
            <span>Auto Layout</span>
          </button>
        )}
      </div>

      {/* Connecting Active Notice */}
      {connectingSource && (
        <div className="absolute top-4 left-4 z-20 px-3 py-1.5 bg-[#FF4B3E]/10 border border-[#FF4B3E]/30 rounded-lg text-xs font-mono text-[#FF4B3E] flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-[#FF4B3E] animate-pulse" />
          <span>Connecting: Click target column in another table...</span>
          <button
            type="button"
            onClick={() => setConnectingSource(null)}
            className="underline ml-2 text-[var(--text-muted)] hover:text-[var(--text-primary)] cursor-pointer"
          >
            Cancel
          </button>
        </div>
      )}

      {/* Zoomable / Pannable World Container */}
      <div
        style={{
          transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
          transformOrigin: '0 0',
          position: 'absolute',
          top: 0,
          left: 0,
          width: '100%',
          height: '100%',
        }}
      >
        {/* SVG Bezier Relationship Curves */}
        <svg className="absolute top-0 left-0 w-[5000px] h-[5000px] pointer-events-none overflow-visible">
          <defs>
            <marker
              id="erd-arrow"
              viewBox="0 0 10 10"
              refX="6"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 1 L 8 5 L 0 9 z" fill="var(--text-muted)" />
            </marker>
          </defs>

          {relationships.map((rel) => {
            const fromTable = tables[rel.from_table];
            const toTable = tables[rel.to_table];
            if (!fromTable || !toTable) return null;

            // Determine left/right orientation
            const isFromOnLeft = fromTable.position_x < toTable.position_x;
            const startAnchor = getColumnAnchor(rel.from_table, rel.from_column, isFromOnLeft);
            const endAnchor = getColumnAnchor(rel.to_table, rel.to_column, !isFromOnLeft);

            const x1 = startAnchor.x;
            const y1 = startAnchor.y;
            const x2 = endAnchor.x;
            const y2 = endAnchor.y;

            const dx = Math.abs(x2 - x1);
            const controlDist = Math.max(40, dx * 0.45);
            const cx1 = isFromOnLeft ? x1 + controlDist : x1 - controlDist;
            const cx2 = isFromOnLeft ? x2 - controlDist : x2 + controlDist;

            const pathD = `M ${x1} ${y1} C ${cx1} ${y1}, ${cx2} ${y2}, ${x2} ${y2}`;
            const midX = (x1 + x2) / 2;
            const midY = (y1 + y2) / 2;

            return (
              <g key={rel.id} className="cursor-pointer pointer-events-auto group" onClick={() => onSelectRelationship(rel)}>
                {/* Thick invisible hover target */}
                <path d={pathD} fill="none" stroke="transparent" strokeWidth="18" />

                {/* Visible curve */}
                <path
                  d={pathD}
                  fill="none"
                  stroke={rel.status === 'confirmed' ? '#10B981' : '#3B82F6'}
                  strokeWidth="2"
                  strokeDasharray={rel.confidence < 0.85 ? '4 4' : undefined}
                  className="group-hover:stroke-[#FF4B3E] transition-colors"
                />

                {/* Small visual stem connecting pill to line */}
                <line
                  x1={midX}
                  y1={midY - 8}
                  x2={midX}
                  y2={midY}
                  stroke={rel.status === 'confirmed' ? '#10B981' : '#3B82F6'}
                  strokeWidth="1.5"
                  className="group-hover:stroke-[#FF4B3E] transition-colors"
                />

                {/* Elevated Cardinality pill floating cleanly above curve */}
                <foreignObject
                  x={midX - 48}
                  y={midY - 26}
                  width="96"
                  height="26"
                  className="overflow-visible pointer-events-auto"
                >
                  <div
                    className="flex items-center justify-center gap-1.5 px-2.5 py-1 rounded-full text-[10px] font-mono font-bold bg-[var(--bg-surface)] border border-[var(--border-hairline)] shadow-md group-hover:border-[#FF4B3E] group-hover:text-[#FF4B3E] transition-all cursor-pointer select-none"
                    title={`Click to preview join: ${rel.from_table}.${rel.from_column} → ${rel.to_table}.${rel.to_column} (${Math.round(rel.match_ratio * 100)}% match)`}
                  >
                    <span className="text-[10px] font-bold text-[var(--text-primary)]">{rel.cardinality}</span>
                    <span className="text-[9px] text-[var(--text-muted)]">·</span>
                    <span className="text-[10px] font-medium text-emerald-400">
                      {Math.round(rel.match_ratio * 100)}%
                    </span>
                  </div>
                </foreignObject>
              </g>
            );
          })}
        </svg>

        {/* Entity Cards */}
        {Object.values(tables).map((table) => (
          <TableEntityCard
            key={table.table_name}
            table={table}
            isSelected={selectedTable === table.table_name}
            onSelect={() => setSelectedTable(table.table_name)}
            onDelete={() => onDeleteTable(table.table_name)}
            onStartConnect={(col) => handleStartConnect(table.table_name, col)}
            onEndConnect={(col) => handleEndConnect(table.table_name, col)}
            connectingFromCol={
              connectingSource ? `${connectingSource.table}.${connectingSource.col}` : null
            }
            onMouseDownDrag={(e) => startDraggingTable(table.table_name, e)}
          />
        ))}
      </div>
    </div>
  );
};
