import React, { useEffect, useState } from 'react';
import {
  Sparkles,
  Share2,
  Terminal,
  Database,
  Layers,
  Loader2,
  CheckCircle2,
  AlertTriangle,
  X,
  Download,
  Bot,
} from 'lucide-react';
import {
  createModelingSession,
  getModelingSession,
  uploadModelingTable,
  deleteModelingTable,
  updateModelingTablePosition,
  detectModelingRelationships,
  saveModelingRelationship,
  deleteModelingRelationship,
  handoffModelingToQuery,
  downloadModelingMergedCsv,
  type ModelingSessionState,
  type Relationship,
  type QueryHandoffResult,
} from '../lib/api';
import { ErdCanvas } from '../components/modeling/ErdCanvas';
import { MultiTableDropzone } from '../components/modeling/MultiTableDropzone';
import { RelationshipModal } from '../components/modeling/RelationshipModal';
import { JoinPreviewDrawer } from '../components/modeling/JoinPreviewDrawer';
import { SchemaExportModal } from '../components/modeling/SchemaExportModal';
import { Button } from '../components/ui/Button';

import { MAX_FILE_SIZE_BYTES } from '../config';

interface ModelPageProps {
  onHandoffToQuery?: (
    jobId: string,
    filename: string,
    handoff?: QueryHandoffResult,
    initialMode?: 'sql' | 'ask_ai'
  ) => void;
}

export const ModelPage: React.FC<ModelPageProps> = ({ onHandoffToQuery }) => {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [session, setSession] = useState<ModelingSessionState | null>(null);
  const [loading, setLoading] = useState(true);
  const [isUploading, setIsUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toastMessage, setToastMessage] = useState<string | null>(null);

  // Modals & Drawers
  const [selectedRelationship, setSelectedRelationship] = useState<Relationship | null>(null);
  const [editingRelationship, setEditingRelationship] = useState<Relationship | null>(null);
  const [showExportModal, setShowExportModal] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);

  // Initialize or restore session
  useEffect(() => {
    const initSession = async () => {
      setLoading(true);
      setError(null);
      try {
        const res = await createModelingSession();
        setSessionId(res.session_id);
        setSession(res);
      } catch (err: any) {
        setError(err.message || 'Failed to initialize modeling session.');
      } finally {
        setLoading(false);
      }
    };

    initSession();
  }, []);

  const showToast = (msg: string) => {
    setToastMessage(msg);
    setTimeout(() => setToastMessage(null), 3000);
  };

  // Supported extensions
  const SUPPORTED_EXTENSIONS = ['.csv', '.tsv', '.parquet', '.json', '.ndjson', '.jsonl', '.xlsx', '.db', '.sqlite', '.sqlite3'];

  // Upload multiple files
  const handleUploadFiles = async (files: File[]) => {
    if (!sessionId) return;
    setIsUploading(true);
    setError(null);
    try {
      // Validate all files first
      for (const file of files) {
        const lastDot = file.name.lastIndexOf('.');
        const ext = lastDot !== -1 ? file.name.slice(lastDot).toLowerCase() : '';
        if (!SUPPORTED_EXTENSIONS.includes(ext)) {
          throw new Error(`Unsupported file type '${ext || file.name}'. Please upload CSV, Parquet, Excel, JSON, or SQLite database files.`);
        }
        if (file.size > MAX_FILE_SIZE_BYTES) {
          throw new Error(`File '${file.name}' exceeds the 5GB limit.`);
        }
        if (file.size === 0) {
          throw new Error(`File '${file.name}' is empty (0 bytes).`);
        }
      }

      let latestState: ModelingSessionState | null = session;
      for (const file of files) {
        latestState = await uploadModelingTable(sessionId, file);
      }
      if (latestState) {
        setSession(latestState);
        showToast(`Staged ${files.length} table(s) into modeling session.`);
      }
    } catch (err: any) {
      setError(err.message || 'Failed to upload table.');
    } finally {
      setIsUploading(false);
    }
  };

  // Delete table
  const handleDeleteTable = async (tableName: string) => {
    if (!sessionId) return;
    try {
      const updated = await deleteModelingTable(sessionId, tableName);
      setSession(updated);
      if (selectedRelationship && (selectedRelationship.from_table === tableName || selectedRelationship.to_table === tableName)) {
        setSelectedRelationship(null);
      }
      showToast(`Removed table '${tableName}'.`);
    } catch (err: any) {
      setError(err.message || 'Failed to delete table.');
    }
  };

  // Update card position on drag
  const handleUpdatePosition = async (tableName: string, x: number, y: number) => {
    if (!sessionId || !session) return;
    // Optimistic local update
    setSession({
      ...session,
      tables: {
        ...session.tables,
        [tableName]: {
          ...session.tables[tableName],
          position_x: x,
          position_y: y,
        },
      },
    });
    await updateModelingTablePosition(sessionId, tableName, x, y);
  };

  // Auto-detect keys & relationships
  const handleAutoDetect = async () => {
    if (!sessionId) return;
    setLoading(true);
    try {
      const updated = await detectModelingRelationships(sessionId);
      setSession(updated);
      showToast(`Detected ${updated.relationships.length} relationship link(s).`);
    } catch (err: any) {
      setError(err.message || 'Auto-detection failed.');
    } finally {
      setLoading(false);
    }
  };

  // Auto-arrange layout in clean grid
  const handleAutoArrange = () => {
    if (!session || !sessionId) return;
    const tableKeys = Object.keys(session.tables);
    const updatedTables = { ...session.tables };

    tableKeys.forEach((tbl, idx) => {
      const x = (idx % 3) * 340 + 40;
      const y = Math.floor(idx / 3) * 360 + 40;
      updatedTables[tbl] = { ...updatedTables[tbl], position_x: x, position_y: y };
      updateModelingTablePosition(sessionId, tbl, x, y);
    });

    setSession({ ...session, tables: updatedTables });
    showToast('Auto-arranged table layout.');
  };

  // Connect two columns via canvas anchor drag
  const handleConnectColumns = (fromTable: string, fromCol: string, toTable: string, toCol: string) => {
    const newRel: Relationship = {
      id: `rel_${fromTable}_${fromCol}_${toTable}_${toCol}`,
      from_table: fromTable,
      from_column: fromCol,
      to_table: toTable,
      to_column: toCol,
      cardinality: '1:N',
      confidence: 1.0,
      match_ratio: 1.0,
      orphan_count: 0,
      status: 'manual',
    };
    setEditingRelationship(newRel);
  };

  // Save / Update relationship
  const handleSaveRelationship = async (rel: Relationship) => {
    if (!sessionId) return;
    try {
      await saveModelingRelationship(sessionId, rel);
      const updated = await getModelingSession(sessionId);
      setSession(updated);
      showToast(`Saved relational link: ${rel.from_table}.${rel.from_column} → ${rel.to_table}.${rel.to_column}`);
    } catch (err: any) {
      setError(err.message || 'Failed to save relationship.');
    }
  };

  // Delete relationship
  const handleDeleteRelationship = async (relId: string) => {
    if (!sessionId) return;
    try {
      await deleteModelingRelationship(sessionId, relId);
      const updated = await getModelingSession(sessionId);
      setSession(updated);
      if (selectedRelationship?.id === relId) {
        setSelectedRelationship(null);
      }
      showToast('Deleted relationship link.');
    } catch (err: any) {
      setError(err.message || 'Failed to delete relationship.');
    }
  };

  // Query handoff to SQL Studio or AI Analyst
  const handleHandoff = async (mode: 'sql' | 'ask_ai' = 'sql') => {
    if (!sessionId) return;
    try {
      showToast(mode === 'ask_ai' ? 'Preparing data for AI Analyst...' : 'Preparing data for SQL Studio...');
      const handoff = await handoffModelingToQuery(sessionId);
      if (onHandoffToQuery && handoff.tables && handoff.tables.length > 0) {
        onHandoffToQuery(sessionId, handoff.filename || `multi_table_${sessionId}`, handoff, mode);
      } else {
        navigator.clipboard.writeText(handoff.sql);
        showToast('Multi-table JOIN query copied to clipboard!');
      }
    } catch (err: any) {
      setError(err.message || 'Failed to create query handoff.');
    }
  };

  // Download full merged CSV from DuckDB
  const handleDownloadMergedCsv = async () => {
    if (!sessionId) return;
    try {
      showToast('Exporting merged dataset from DuckDB...');
      await downloadModelingMergedCsv(sessionId);
      showToast('Merged CSV downloaded successfully!');
    } catch (err: any) {
      setError(err.message || 'Failed to download merged CSV.');
    }
  };

  const tablesCount = session ? Object.keys(session.tables).length : 0;
  const relationshipsCount = session ? session.relationships.length : 0;

  return (
    <div className="flex flex-col flex-1 h-full min-h-0 bg-[var(--bg-app)] border-t border-[var(--border-hairline)]">
      {/* Top Action Header */}
      <div className="px-3 sm:px-5 py-2.5 sm:py-3 bg-[var(--bg-surface)] border-b border-[var(--border-hairline)] flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2.5 shrink-0">
        <div className="flex items-center justify-between w-full sm:w-auto">
          <div className="flex items-center gap-2.5 sm:gap-3">
            <div className="w-8 h-8 sm:w-9 sm:h-9 rounded-xl bg-[var(--bg-subtle)] border border-[var(--border-hairline)] text-[#FF4B3E] flex items-center justify-center shrink-0">
              <Layers className="w-4 h-4" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h2 className="text-sm sm:text-base font-semibold text-[var(--text-primary)] tracking-tight">
                  Data Model
                </h2>
                <span className="text-[10px] sm:text-[11px] font-mono px-2 py-0.5 rounded-full bg-[var(--bg-subtle)] text-[var(--text-secondary)] border border-[var(--border-hairline)]">
                  {tablesCount} {tablesCount === 1 ? 'Table' : 'Tables'} · {relationshipsCount} Links
                </span>
              </div>
              <p className="text-xs text-[var(--text-muted)] hidden sm:block">
                Upload related files to inspect tables and find key relationships.
              </p>
            </div>
          </div>

          {/* Mobile Sidebar Toggle Button */}
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setMobileSidebarOpen((prev) => !prev)}
            className="md:hidden text-xs border border-[var(--border-hairline)] bg-[var(--bg-subtle)] hover:bg-[var(--bg-hover)] cursor-pointer shrink-0 ml-2"
            leftIcon={<Layers className="w-3.5 h-3.5 text-[#FF4B3E]" />}
          >
            Entities ({tablesCount})
          </Button>
        </div>

        {/* Action Controls - horizontally swipeable on mobile */}
        <div className="flex items-center gap-2 overflow-x-auto scrollbar-none w-full sm:w-auto pb-1 sm:pb-0 shrink-0">
          <Button
            size="sm"
            variant="ghost"
            onClick={handleAutoDetect}
            disabled={tablesCount < 2 || loading}
            leftIcon={<Sparkles className="w-3.5 h-3.5 text-[#FF4B3E]" />}
            className="text-xs border border-[var(--border-hairline)] bg-[var(--bg-subtle)] hover:bg-[var(--bg-hover)] cursor-pointer shrink-0 whitespace-nowrap"
          >
            Auto-Detect Keys
          </Button>

          <Button
            size="sm"
            variant="ghost"
            onClick={handleDownloadMergedCsv}
            disabled={tablesCount === 0}
            leftIcon={<Download className="w-3.5 h-3.5 text-emerald-400" />}
            className="text-xs border border-[var(--border-hairline)] bg-[var(--bg-subtle)] hover:bg-[var(--bg-hover)] cursor-pointer shrink-0 whitespace-nowrap"
            title="Execute relational JOIN in DuckDB and download the complete merged dataset as CSV"
          >
            Download Merged CSV
          </Button>

          <Button
            size="sm"
            variant="ghost"
            onClick={() => setShowExportModal(true)}
            disabled={tablesCount === 0}
            leftIcon={<Share2 className="w-3.5 h-3.5 text-[var(--text-muted)]" />}
            className="text-xs border border-[var(--border-hairline)] bg-[var(--bg-subtle)] hover:bg-[var(--bg-hover)] cursor-pointer shrink-0 whitespace-nowrap"
          >
            Export ERD
          </Button>

          <Button
            size="sm"
            variant="ghost"
            onClick={() => handleHandoff('ask_ai')}
            disabled={tablesCount === 0}
            leftIcon={<Bot className="w-3.5 h-3.5 text-purple-400" />}
            className="text-xs border border-[var(--border-hairline)] bg-[var(--bg-subtle)] hover:bg-[var(--bg-hover)] cursor-pointer shrink-0 whitespace-nowrap"
            title="Materialize modeled dataset and explore with natural-language AI Analyst"
          >
            Ask AI Analyst
          </Button>

          <Button
            size="sm"
            variant="ghost"
            onClick={() => handleHandoff('sql')}
            disabled={tablesCount === 0}
            leftIcon={<Terminal className="w-3.5 h-3.5 text-[#FF4B3E]" />}
            className="text-xs border border-[var(--border-hairline)] bg-[var(--bg-subtle)] hover:bg-[var(--bg-hover)] cursor-pointer shrink-0 whitespace-nowrap"
            title="Materialize modeled dataset and open DuckDB SQL query editor"
          >
            Query in SQL Studio
          </Button>
        </div>
      </div>

      {/* Toast Notification */}
      {toastMessage && (
        <div className="fixed top-16 right-6 z-50 px-4 py-2 rounded-lg bg-emerald-500/15 border border-emerald-500/30 text-emerald-400 text-xs flex items-center gap-2 shadow-lg font-mono">
          <CheckCircle2 className="w-4 h-4 shrink-0" />
          <span>{toastMessage}</span>
        </div>
      )}

      {/* Error Alert */}
      {error && (
        <div className="mx-3 sm:mx-5 mt-3 px-4 py-2.5 rounded-lg bg-red-500/10 border border-red-500/20 text-red-400 text-xs flex items-center justify-between font-mono shrink-0">
          <div className="flex items-center gap-2">
            <AlertTriangle className="w-4 h-4 shrink-0" />
            <span>{error}</span>
          </div>
          <button type="button" onClick={() => setError(null)} className="cursor-pointer">
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      )}

      {/* Main Workspace */}
      <div className="flex-1 flex overflow-hidden relative">
        {/* Mobile Backdrop for Sidebar Drawer */}
        {mobileSidebarOpen && (
          <div
            className="fixed inset-0 bg-black/50 z-30 md:hidden backdrop-blur-2xs transition-opacity"
            onClick={() => setMobileSidebarOpen(false)}
            aria-hidden="true"
          />
        )}

        {/* Left Sidebar / Tables Drawer: absolute sliding drawer on mobile, static on desktop */}
        <div
          className={`absolute md:static inset-y-0 left-0 z-40 w-72 max-w-[85vw] bg-[var(--bg-surface)] border-r border-[var(--border-hairline)] flex flex-col shrink-0 transition-transform duration-200 ease-in-out md:translate-x-0 ${
            mobileSidebarOpen ? 'translate-x-0 shadow-2xl' : '-translate-x-full'
          }`}
        >
          <div className="p-3 border-b border-[var(--border-hairline)] bg-[var(--bg-subtle)] flex items-center justify-between">
            <span className="font-mono text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider">
              Staged Entities ({tablesCount})
            </span>
            <button
              type="button"
              onClick={() => setMobileSidebarOpen(false)}
              className="md:hidden p-1 text-[var(--text-muted)] hover:text-[var(--text-primary)] rounded cursor-pointer"
              aria-label="Close entities sidebar"
            >
              <X className="w-4 h-4" />
            </button>
          </div>

          {/* Table List */}
          <div className="flex-1 overflow-y-auto divide-y divide-[var(--border-hairline)]/60">
            {tablesCount === 0 ? (
              <div className="p-5 text-center text-xs text-[var(--text-muted)] font-mono">
                No tables staged yet.
              </div>
            ) : (
              Object.values(session!.tables).map((t) => (
                <div key={t.table_name} className="p-3 hover:bg-[var(--bg-hover)] transition-colors flex items-center justify-between text-xs">
                  <div className="min-w-0 pr-2">
                    <span className="font-mono font-bold text-[var(--text-primary)] block truncate" title={t.table_name}>
                      {t.table_name}
                    </span>
                    <span className="text-[11px] text-[var(--text-muted)] block">
                      {t.row_count.toLocaleString()} rows · {t.columns.length} cols
                    </span>
                  </div>
                  <button
                    type="button"
                    onClick={() => handleDeleteTable(t.table_name)}
                    className="p-1 text-[var(--text-muted)] hover:text-red-400 transition-colors cursor-pointer"
                    title="Remove table"
                  >
                    <X className="w-3.5 h-3.5" />
                  </button>
                </div>
              ))
            )}
          </div>
        </div>

        {/* Center ERD Canvas Area */}
        <div className="flex-1 relative overflow-hidden w-full min-w-0">
          {loading && !session ? (
            <div className="w-full h-full flex items-center justify-center gap-2 text-xs text-[var(--text-muted)] font-mono">
              <Loader2 className="w-5 h-5 animate-spin text-[#FF4B3E]" />
              <span>Loading modeling session...</span>
            </div>
          ) : tablesCount === 0 ? (
            /* Empty State Hero - perfectly responsive on mobile */
            <div className="w-full h-full flex flex-col items-center justify-center p-4 sm:p-8 text-center max-w-full overflow-y-auto">
              <div className="w-14 h-14 sm:w-16 sm:h-16 rounded-2xl bg-[var(--bg-surface)] border border-[var(--border-hairline)] text-[#FF4B3E] flex items-center justify-center shadow-lg mb-3 sm:mb-4">
                <Database className="w-7 h-7 sm:w-8 sm:h-8" />
              </div>
              <h3 className="text-base sm:text-lg font-semibold text-[var(--text-primary)] tracking-tight">
                Model your tables
              </h3>
              <p className="text-xs text-[var(--text-secondary)] max-w-md mt-1 mb-5 sm:mb-6 leading-relaxed px-2">
                Upload related datasets to view their schemas and find key relationships.
              </p>
              <div className="w-full max-w-sm px-2">
                <MultiTableDropzone
                  onUploadFiles={handleUploadFiles}
                  isUploading={isUploading}
                />
              </div>
            </div>
          ) : (
            <ErdCanvas
              tables={session!.tables}
              relationships={session!.relationships}
              onUpdateTablePosition={handleUpdatePosition}
              onDeleteTable={handleDeleteTable}
              onSelectRelationship={(rel) => setSelectedRelationship(rel)}
              onConnectColumns={handleConnectColumns}
              onAutoArrange={handleAutoArrange}
            />
          )}

          {/* Bottom Sliding Join Preview Drawer */}
          {sessionId && selectedRelationship && (
            <JoinPreviewDrawer
              sessionId={sessionId}
              relationship={selectedRelationship}
              onClose={() => setSelectedRelationship(null)}
              onEditRelationship={(rel) => setEditingRelationship(rel)}
            />
          )}
        </div>
      </div>

      {/* Relationship Configuration Modal */}
      {editingRelationship && session && (
        <RelationshipModal
          isOpen={true}
          relationship={editingRelationship}
          tables={session.tables}
          onClose={() => setEditingRelationship(null)}
          onSave={handleSaveRelationship}
          onDelete={handleDeleteRelationship}
        />
      )}

      {/* Schema Export Modal (Mermaid / DBML / SQL DDL) */}
      {sessionId && showExportModal && (
        <SchemaExportModal
          isOpen={showExportModal}
          sessionId={sessionId}
          onClose={() => setShowExportModal(false)}
        />
      )}
    </div>
  );
};
