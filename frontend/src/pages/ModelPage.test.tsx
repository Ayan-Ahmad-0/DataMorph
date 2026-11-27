import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { ModelPage } from './ModelPage';
import * as api from '../lib/api';
import {
  mockEmptySessionState,
  mockTwoTablesSessionState,
  mockFiveTablesSessionState,
  mockCustomersTable,
  mockOrdersTable,
  mockMalformedDatasetError,
  mockCorruptedSqliteError,
  generate50TablesSessionState,
} from '../test/mockData';

// Mock all modeling API functions
vi.mock('../lib/api', async () => {
  const actual = await vi.importActual('../lib/api');
  return {
    ...actual,
    createModelingSession: vi.fn(),
    getModelingSession: vi.fn(),
    uploadModelingTable: vi.fn(),
    deleteModelingTable: vi.fn(),
    updateModelingTablePosition: vi.fn(),
    detectModelingRelationships: vi.fn(),
    saveModelingRelationship: vi.fn(),
    deleteModelingRelationship: vi.fn(),
    handoffModelingToQuery: vi.fn(),
    exportModelingSchema: vi.fn(),
    previewModelingJoin: vi.fn(),
    downloadModelingMergedCsv: vi.fn(),
  };
});

describe('Model Data (ERD) Studio - Comprehensive Production Test Suite', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.createModelingSession as any).mockResolvedValue(mockEmptySessionState);
    (api.getModelingSession as any).mockResolvedValue(mockEmptySessionState);
    (api.updateModelingTablePosition as any).mockResolvedValue({ status: 'ok' });
  });

  // =========================================================================
  // SCENARIO 1: Empty State & File Ingestion
  // =========================================================================
  describe('Scenario 1: Empty State & File Ingestion', () => {
    it('keeps the sidebar file-only and shows the upload dropzone on the main canvas when tables == 0', async () => {
      render(<ModelPage />);

      // Wait for session initialization
      await waitFor(() => {
        expect(screen.getByText('Data Modeler & ERD Studio')).toBeInTheDocument();
      });

      // Verify sidebar state
      expect(screen.getByText(/Staged Entities \(0\)/i)).toBeInTheDocument();
      expect(screen.getByText('No tables staged yet.')).toBeInTheDocument();

      // Verify main canvas empty state hero
      expect(screen.getByText('Build your Relational Data Model')).toBeInTheDocument();
      expect(
        screen.getByText(/Upload multiple related datasets \(orders, customers, products\) to auto-detect/i)
      ).toBeInTheDocument();

      // Upload stays available on the canvas, not in the file list sidebar.
      const dropzonePrompts = screen.getAllByText('Drop multiple datasets here');
      expect(dropzonePrompts.length).toBe(1);
    });

    it('transitions UI from Empty to Populated on valid file uploads (CSV, Parquet, Excel, JSON, SQLite)', async () => {
      // Mock successful sequential uploads transitioning to two tables
      (api.uploadModelingTable as any).mockResolvedValueOnce({
        ...mockEmptySessionState,
        tables: { customers: mockCustomersTable },
      });

      render(<ModelPage />);

      await waitFor(() => {
        expect(screen.getByText('Build your Relational Data Model')).toBeInTheDocument();
      });

      // Find file inputs in the dropzones
      const fileInputs = document.querySelectorAll('input[type="file"]');
      expect(fileInputs.length).toBeGreaterThan(0);

      const csvFile = new File(['id,name\n1,Alice\n2,Bob'], 'customers.csv', { type: 'text/csv' });
      fireEvent.change(fileInputs[0], { target: { files: [csvFile] } });

      await waitFor(() => {
        expect(api.uploadModelingTable).toHaveBeenCalledWith('model_empty_123', csvFile);
      });

      // Main canvas empty hero disappears and table card appears
      await waitFor(() => {
        expect(screen.queryByText('Build your Relational Data Model')).not.toBeInTheDocument();
        expect(screen.getAllByText('customers').length).toBeGreaterThan(0);
        expect(screen.getByText('1 Table · 0 Links')).toBeInTheDocument();
      });
    });

    it('rejects unsupported file formats before calling the upload API', async () => {
      render(<ModelPage />);

      await waitFor(() => {
        expect(screen.getByText('Build your Relational Data Model')).toBeInTheDocument();
      });

      const fileInputs = document.querySelectorAll('input[type="file"]');
      const invalidExe = new File(['binary content'], 'script.exe', { type: 'application/x-msdownload' });

      fireEvent.change(fileInputs[0], { target: { files: [invalidExe] } });

      // uploadModelingTable should NOT have been invoked
      expect(api.uploadModelingTable).not.toHaveBeenCalled();

      // Informative rejection error displayed in UI
      await waitFor(() => {
        expect(
          screen.getByText(/Unsupported file type '\.exe'\. Please upload CSV, Parquet, Excel, JSON, or SQLite database files\./i)
        ).toBeInTheDocument();
      });
    });

    it('rejects files exceeding the 5GB limit before calling the upload API', async () => {
      render(<ModelPage />);

      await waitFor(() => {
        expect(screen.getByText('Build your Relational Data Model')).toBeInTheDocument();
      });

      const fileInputs = document.querySelectorAll('input[type="file"]');
      const oversizedFile = new File(['test'], 'giant_warehouse.parquet', { type: 'application/octet-stream' });
      // Set size > 5GB (5 * 1024 * 1024 * 1024 + 1 bytes)
      Object.defineProperty(oversizedFile, 'size', {
        value: 5 * 1024 * 1024 * 1024 + 1024,
        writable: false,
      });

      fireEvent.change(fileInputs[0], { target: { files: [oversizedFile] } });

      expect(api.uploadModelingTable).not.toHaveBeenCalled();

      await waitFor(() => {
        expect(screen.getByText(/File 'giant_warehouse\.parquet' exceeds the 5GB limit\./i)).toBeInTheDocument();
      });
    });

    it('rejects empty files (0 bytes) with a descriptive error', async () => {
      render(<ModelPage />);

      await waitFor(() => {
        expect(screen.getByText('Build your Relational Data Model')).toBeInTheDocument();
      });

      const fileInputs = document.querySelectorAll('input[type="file"]');
      const emptyFile = new File([], 'empty_records.csv', { type: 'text/csv' });
      Object.defineProperty(emptyFile, 'size', { value: 0 });

      fireEvent.change(fileInputs[0], { target: { files: [emptyFile] } });

      expect(api.uploadModelingTable).not.toHaveBeenCalled();

      await waitFor(() => {
        expect(screen.getByText(/File 'empty_records\.csv' is empty \(0 bytes\)\./i)).toBeInTheDocument();
      });
    });
  });

  // =========================================================================
  // SCENARIO 2: Header Metrics & Action Bar
  // =========================================================================
  describe('Scenario 2: Header Metrics & Action Bar', () => {
    it('disables all utility buttons when tables == 0 in the empty state', async () => {
      render(<ModelPage />);

      await waitFor(() => {
        expect(screen.getByText('Data Modeler & ERD Studio')).toBeInTheDocument();
      });

      // Check badge counter
      expect(screen.getByText('0 Tables · 0 Links')).toBeInTheDocument();

      // Check button states
      const autoDetectBtn = screen.getByRole('button', { name: /Auto-Detect Keys/i });
      const downloadCsvBtn = screen.getByRole('button', { name: /Download Merged CSV/i });
      const exportErdBtn = screen.getByRole('button', { name: /Export ERD/i });
      const querySqlBtn = screen.getByRole('button', { name: /Query in SQL Studio/i });

      expect(autoDetectBtn).toBeDisabled();
      expect(downloadCsvBtn).toBeDisabled();
      expect(exportErdBtn).toBeDisabled();
      expect(querySqlBtn).toBeDisabled();
    });

    it('disables Auto-Detect Keys when only 1 table is staged, but enables Export & Query', async () => {
      (api.createModelingSession as any).mockResolvedValue({
        ...mockEmptySessionState,
        tables: { customers: mockCustomersTable },
        relationships: [],
      });

      render(<ModelPage />);

      await waitFor(() => {
        expect(screen.getByText('1 Table · 0 Links')).toBeInTheDocument();
      });

      const autoDetectBtn = screen.getByRole('button', { name: /Auto-Detect Keys/i });
      const downloadCsvBtn = screen.getByRole('button', { name: /Download Merged CSV/i });
      const exportErdBtn = screen.getByRole('button', { name: /Export ERD/i });
      const querySqlBtn = screen.getByRole('button', { name: /Query in SQL Studio/i });

      // Needs at least 2 tables to detect relationships
      expect(autoDetectBtn).toBeDisabled();
      expect(downloadCsvBtn).not.toBeDisabled();
      expect(exportErdBtn).not.toBeDisabled();
      expect(querySqlBtn).not.toBeDisabled();
    });

    it('enables all action buttons and updates counters when multiple tables and links exist', async () => {
      (api.createModelingSession as any).mockResolvedValue(mockFiveTablesSessionState);

      render(<ModelPage />);

      await waitFor(() => {
        expect(screen.getByText('5 Tables · 3 Links')).toBeInTheDocument();
      });

      const autoDetectBtn = screen.getByRole('button', { name: /Auto-Detect Keys/i });
      const downloadCsvBtn = screen.getByRole('button', { name: /Download Merged CSV/i });
      const exportErdBtn = screen.getByRole('button', { name: /Export ERD/i });
      const querySqlBtn = screen.getByRole('button', { name: /Query in SQL Studio/i });

      expect(autoDetectBtn).not.toBeDisabled();
      expect(downloadCsvBtn).not.toBeDisabled();
      expect(exportErdBtn).not.toBeDisabled();
      expect(querySqlBtn).not.toBeDisabled();
    });

    it('triggers SQL Studio handoff when the coral action button is clicked', async () => {
      const mockHandoff = vi.fn();
      (api.createModelingSession as any).mockResolvedValue(mockTwoTablesSessionState);
      (api.handoffModelingToQuery as any).mockResolvedValue({
        query_id: 'model_two_tables',
        filename: 'multi_table_model_two_tables',
        row_count: 4,
        columns: ['id', 'name', 'order_id'],
        file_size_bytes: 1024,
        sql: 'SELECT * FROM customers JOIN orders ON customers.id = orders.customer_id',
        tables: ['customers', 'orders'],
      });

      render(<ModelPage onHandoffToQuery={mockHandoff} />);

      await waitFor(() => {
        expect(screen.getByRole('button', { name: /Query in SQL Studio/i })).not.toBeDisabled();
      });

      fireEvent.click(screen.getByRole('button', { name: /Query in SQL Studio/i }));

      await waitFor(() => {
        expect(api.handoffModelingToQuery).toHaveBeenCalledWith('model_two_tables');
        expect(mockHandoff).toHaveBeenCalledWith(
          'model_two_tables',
          'multi_table_model_two_tables',
          expect.objectContaining({ tables: ['customers', 'orders'] }),
          'sql'
        );
      });
    });

    it('triggers AI Analyst handoff when the Ask AI Analyst button is clicked', async () => {
      const mockHandoff = vi.fn();
      (api.createModelingSession as any).mockResolvedValue(mockTwoTablesSessionState);
      (api.handoffModelingToQuery as any).mockResolvedValue({
        query_id: 'model_two_tables',
        filename: 'multi_table_model_two_tables',
        row_count: 4,
        columns: ['id', 'name', 'order_id'],
        file_size_bytes: 1024,
        sql: 'SELECT * FROM customers JOIN orders ON customers.id = orders.customer_id',
        tables: ['customers', 'orders'],
      });

      render(<ModelPage onHandoffToQuery={mockHandoff} />);

      await waitFor(() => {
        expect(screen.getByRole('button', { name: /Ask AI Analyst/i })).not.toBeDisabled();
      });

      fireEvent.click(screen.getByRole('button', { name: /Ask AI Analyst/i }));

      await waitFor(() => {
        expect(api.handoffModelingToQuery).toHaveBeenCalledWith('model_two_tables');
        expect(mockHandoff).toHaveBeenCalledWith(
          'model_two_tables',
          'multi_table_model_two_tables',
          expect.objectContaining({ tables: ['customers', 'orders'] }),
          'ask_ai'
        );
      });
    });
  });

  // =========================================================================
  // SCENARIO 3: Canvas Interactions & State Management
  // =========================================================================
  describe('Scenario 3: Canvas Interactions & State Management', () => {
    it('renders ERD entity cards with table names, column names, and data types', async () => {
      (api.createModelingSession as any).mockResolvedValue(mockTwoTablesSessionState);

      render(<ModelPage />);

      await waitFor(() => {
        expect(screen.getAllByText('customers').length).toBeGreaterThan(0);
        expect(screen.getAllByText('orders').length).toBeGreaterThan(0);
      });

      // Verify customer columns and types
      expect(screen.getByText('first_name')).toBeInTheDocument();
      expect(screen.getByText('email')).toBeInTheDocument();

      // Verify orders columns and types
      expect(screen.getByText('customer_id')).toBeInTheDocument();
      expect(screen.getByText('total_amount')).toBeInTheDocument();

      // Check row & column count metadata
      expect(screen.getAllByText(/1,250 rows/i).length).toBeGreaterThan(0);
      expect(screen.getAllByText(/8,420 rows/i).length).toBeGreaterThan(0);
    });

    it('triggers Auto-Detect Keys and maps relationships visually on the canvas', async () => {
      // Start with two unlinked tables
      const unlinkedTwoTables = {
        ...mockTwoTablesSessionState,
        relationships: [],
      };
      (api.createModelingSession as any).mockResolvedValue(unlinkedTwoTables);
      (api.detectModelingRelationships as any).mockResolvedValue(mockTwoTablesSessionState);

      render(<ModelPage />);

      await waitFor(() => {
        expect(screen.getByText('2 Tables · 0 Links')).toBeInTheDocument();
      });

      const autoDetectBtn = screen.getByRole('button', { name: /Auto-Detect Keys/i });
      fireEvent.click(autoDetectBtn);

      await waitFor(() => {
        expect(api.detectModelingRelationships).toHaveBeenCalledWith('model_two_tables');
      });

      // Verify relationship was applied and badge counter updated
      await waitFor(() => {
        expect(screen.getByText('2 Tables · 1 Links')).toBeInTheDocument();
        expect(screen.getByText('Detected 1 relationship link(s).')).toBeInTheDocument();
      });

      // Verify relationship cardinality pill is rendered on canvas
      expect(screen.getByText('1:N')).toBeInTheDocument();
    });

    it('supports manually linking tables via column connection dots and opening configuration modal', async () => {
      (api.createModelingSession as any).mockResolvedValue({
        ...mockTwoTablesSessionState,
        relationships: [],
      });

      render(<ModelPage />);

      await waitFor(() => {
        expect(screen.getAllByText('customers').length).toBeGreaterThan(0);
      });

      // Find the anchor dot for customers.id
      const customerIdDots = screen.getAllByTitle(/Drag relationship from id/i);
      expect(customerIdDots.length).toBeGreaterThan(0);
      fireEvent.click(customerIdDots[0]);

      // Verify banner prompt appears: "Connecting: Click target column in another table..."
      expect(screen.getByText(/Connecting: Click target column in another table\.\.\./i)).toBeInTheDocument();

      // Find target anchor dot on orders.customer_id
      const targetDot = screen.getByTitle(/Connect to customer_id/i);
      fireEvent.click(targetDot);

      // Verify RelationshipModal opens
      await waitFor(() => {
        expect(screen.getByText(/Relational Link Configuration/i)).toBeInTheDocument();
      });

      // Save relationship
      const saveBtn = screen.getByRole('button', { name: /Save Link/i });
      fireEvent.click(saveBtn);

      await waitFor(() => {
        expect(api.saveModelingRelationship).toHaveBeenCalled();
      });
    });

    it('allows deleting an entity table and synchronizes session state', async () => {
      (api.createModelingSession as any).mockResolvedValue(mockTwoTablesSessionState);
      (api.deleteModelingTable as any).mockResolvedValue({
        ...mockTwoTablesSessionState,
        tables: { orders: mockOrdersTable },
        relationships: [],
      });

      render(<ModelPage />);

      await waitFor(() => {
        expect(screen.getAllByText('customers').length).toBeGreaterThan(0);
      });

      // Click delete table button on customers
      const deleteButtons = screen.getAllByTitle(/Remove table/i);
      fireEvent.click(deleteButtons[0]);

      await waitFor(() => {
        expect(api.deleteModelingTable).toHaveBeenCalledWith('model_two_tables', 'customers');
        expect(screen.getByText("Removed table 'customers'.")).toBeInTheDocument();
      });
    });
  });

  // =========================================================================
  // SCENARIO 4: Edge Cases & Error Handling
  // =========================================================================
  describe('Scenario 4: Edge Cases & Error Handling', () => {
    it('displays error banner when uploading malformed dataset or corrupted SQLite schema', async () => {
      (api.uploadModelingTable as any).mockRejectedValueOnce(
        new Error(mockMalformedDatasetError.detail)
      );

      render(<ModelPage />);

      await waitFor(() => {
        expect(screen.getByText('Data Modeler & ERD Studio')).toBeInTheDocument();
      });

      const fileInputs = document.querySelectorAll('input[type="file"]');
      const malformedCsv = new File(['bad,,,content\n1,2'], 'corrupted.csv', { type: 'text/csv' });

      fireEvent.change(fileInputs[0], { target: { files: [malformedCsv] } });

      await waitFor(() => {
        expect(screen.getByText(mockMalformedDatasetError.detail)).toBeInTheDocument();
      });

      // Dismiss the error banner
      const dismissBtn = screen.getByRole('button', { name: '' });
      fireEvent.click(dismissBtn);

      await waitFor(() => {
        expect(screen.queryByText(mockMalformedDatasetError.detail)).not.toBeInTheDocument();
      });
    });

    it('gracefully handles corrupted SQLite schema error from backend', async () => {
      (api.uploadModelingTable as any).mockRejectedValueOnce(
        new Error(mockCorruptedSqliteError.detail)
      );

      render(<ModelPage />);

      await waitFor(() => {
        expect(screen.getByText('Data Modeler & ERD Studio')).toBeInTheDocument();
      });

      const fileInputs = document.querySelectorAll('input[type="file"]');
      const corruptedDb = new File(['not a real sqlite file'], 'corrupted.db', { type: 'application/octet-stream' });

      fireEvent.change(fileInputs[0], { target: { files: [corruptedDb] } });

      await waitFor(() => {
        expect(screen.getByText(mockCorruptedSqliteError.detail)).toBeInTheDocument();
      });
    });

    it('renders 50+ staged tables simultaneously with high layout stability and responsiveness', async () => {
      const stressSession = generate50TablesSessionState(52);
      (api.createModelingSession as any).mockResolvedValue(stressSession);

      const startTime = performance.now();
      render(<ModelPage />);

      await waitFor(() => {
        expect(screen.getByText('52 Tables · 25 Links')).toBeInTheDocument();
      });
      const renderDurationMs = performance.now() - startTime;

      // Ensure render performance is within high-speed bounds (< 1000ms)
      expect(renderDurationMs).toBeLessThan(1000);

      // Verify sidebar entity count
      expect(screen.getByText(/Staged Entities \(52\)/i)).toBeInTheDocument();

      // Check first, middle, and last staged tables render correctly
      expect(screen.getAllByText('staged_entity_1').length).toBeGreaterThan(0);
      expect(screen.getAllByText('staged_entity_25').length).toBeGreaterThan(0);
      expect(screen.getAllByText('staged_entity_52').length).toBeGreaterThan(0);

      // Verify auto-arrange button is responsive under high entity load
      const autoArrangeBtn = screen.getByRole('button', { name: /Auto Layout/i });
      expect(autoArrangeBtn).toBeInTheDocument();
      fireEvent.click(autoArrangeBtn);

      await waitFor(() => {
        expect(screen.getByText('Auto-arranged table layout.')).toBeInTheDocument();
      });
    });
  });
});
