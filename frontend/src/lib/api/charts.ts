import { API_BASE, apiFetch, parseJsonOrThrow, triggerBlobDownload } from './client';

export interface ChartColumnStats {
  name: string;
  type: string;
  null_pct: number;
  distinct_approx: number | null;
  min: string | null;
  max: string | null;
  kind: 'numeric' | 'categorical' | 'datetime' | 'boolean' | 'text_long' | 'id_like';
  parse_hints: {
    as_number_success_pct?: number;
    as_date_success_pct?: number;
  };
}

export interface ChartFieldRef {
  column: string;
  cast_as?: 'number' | 'date' | 'timestamp' | 'text' | null;
  time_bucket?: 'hour' | 'day' | 'week' | 'month' | 'quarter' | 'year' | 'hour_of_day' | 'day_of_week' | null;
  bins?: number | null;
  formula?: string | null;
}

export interface ChartFilter {
  column: string;
  op: '=' | '!=' | '<' | '<=' | '>' | '>=' | 'in' | 'between' | 'is_null' | 'not_null';
  value?: any;
}

export type FieldRef = ChartFieldRef;
export type Filter = ChartFilter;

export type TrendlineType = 'none' | 'linear' | 'sma' | 'ema' | 'polynomial';
export type TrendlineLineStyle = 'dash' | 'dot' | 'solid' | 'dashdot';

export interface TrendlineConfig {
  type: TrendlineType;
  window?: number;
  show_equation?: boolean;
  color?: string | null;
  line_style?: TrendlineLineStyle;
}

export interface ChartSpec {
  chart_type: 'bar' | 'hbar' | 'line' | 'area' | 'histogram' | 'scatter' | 'pie' | 'donut' | 'box' | 'heatmap' | 'big_number';
  x?: ChartFieldRef | null;
  y?: ChartFieldRef | null;
  color?: ChartFieldRef | null;
  aggregation?: 'count' | 'count_distinct' | 'sum' | 'avg' | 'min' | 'max' | 'median' | 'none';
  filters?: ChartFilter[];
  sort?: 'x' | 'y_desc' | 'y_asc';
  top_n?: number | null;
  log_y?: boolean;
  title?: string | null;
  exclude_nulls?: boolean;
  hide_outliers?: boolean;
  custom_color?: string | null;
  trendline?: TrendlineConfig | null;
}

export interface ChartDataResponse {
  spec: ChartSpec;
  columns: string[];
  rows: any[][];
  row_count: number;
  notes: string[];
  warnings: string[];
  elapsed_seconds: number;
}

export interface PageDataOptions {
  cast_overrides: Record<string, string>;
  exclude_missing: boolean;
  hide_outliers: boolean;
  cross_filters?: Record<string, any>;
  calculated_columns?: Record<string, string>;
}

export interface TweakColumnOption {
  name: string;
  type: string;
  kind: string;
  null_pct: number;
  distinct_approx?: number | null;
}

export interface TweakOptionsResponse {
  allowed_chart_types: string[];
  disabled_chart_types: Record<string, string>;
  allowed_aggregations: string[];
  disabled_aggregations: Record<string, string>;
  columns: TweakColumnOption[];
  disabled_reasons: Record<string, string>;
}

export interface ChartRecommendation {
  id: string;
  spec: ChartSpec;
  title: string;
  question_answered: string;
  why: string;
  caveats: string[];
  priority: number;
  source: 'ai' | 'standard';
}

export interface DataPrepTip {
  column?: string | null;
  tip: string;
  action: 'cast_as_date' | 'cast_as_number' | 'exclude_missing' | 'hide_outliers' | 'none';
}

export interface VisualizationPlan {
  summary: string;
  recommendations: ChartRecommendation[];
  prep_tips: DataPrepTip[];
  avoid: string[];
  used_fallback: boolean;
  elapsed_seconds: number;
}

export interface ChartFromTextResult {
  status: 'ok' | 'needs_clarification' | 'cannot_chart' | 'error';
  request: string;
  spec?: ChartSpec | null;
  title?: string | null;
  explanation?: string | null;
  assumptions: string[];
  clarification_question?: string | null;
  message?: string | null;
  data?: ChartDataResponse | null;
  retried: boolean;
  elapsed_seconds: number;
}

export interface AutoVisualizeItem {
  recommendation: ChartRecommendation;
  data?: ChartDataResponse | null;
  error?: string | null;
}

export interface ValidateFormulaResponse {
  valid: boolean;
  inferred_type?: string | null;
  sample_result?: any;
  error?: string | null;
}

export async function getColumnStats(jobId: string): Promise<ChartColumnStats[]> {
  const res = await apiFetch(`${API_BASE}/api/charts/${jobId}/stats`);
  const data = await parseJsonOrThrow<{ columns?: ChartColumnStats[] }>(res, 'Failed to fetch column stats.');
  return data.columns || [];
}

export async function getStarterSpec(jobId: string): Promise<ChartSpec> {
  const res = await apiFetch(`${API_BASE}/api/charts/${jobId}/starter-spec`);
  const data = await parseJsonOrThrow<{ spec: ChartSpec }>(res, 'Failed to fetch starter chart spec.');
  return data.spec;
}

export async function getTweakOptions(jobId: string, spec?: ChartSpec): Promise<TweakOptionsResponse> {
  const res = await apiFetch(`${API_BASE}/api/charts/${jobId}/tweak-options`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ spec }),
  });
  return parseJsonOrThrow(res, 'Failed to fetch tweak options.');
}

export async function getChartSuggestions(jobId: string): Promise<ChartSpec[]> {
  const res = await apiFetch(`${API_BASE}/api/charts/${jobId}/suggest`);
  const data = await parseJsonOrThrow<{ suggestions?: ChartSpec[] }>(res, 'Failed to fetch chart suggestions.');
  return data.suggestions || [];
}

export async function buildChart(
  jobId: string,
  spec: ChartSpec,
  pageOptions?: PageDataOptions,
  signal?: AbortSignal,
): Promise<ChartDataResponse> {
  const res = await apiFetch(`${API_BASE}/api/charts/${jobId}/build`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ spec, page_options: pageOptions }),
    signal,
  });
  return parseJsonOrThrow(res, 'Failed to build chart.');
}

export async function exportChartCsv(jobId: string, spec: ChartSpec): Promise<void> {
  const res = await apiFetch(`${API_BASE}/api/charts/${jobId}/export`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ spec }),
  });
  return triggerBlobDownload(res, `chart_data_${jobId}.csv`, 'Failed to export chart data.');
}

export async function validateFormula(
  jobId: string,
  formula: string,
  name?: string,
): Promise<ValidateFormulaResponse> {
  const res = await apiFetch(`${API_BASE}/api/charts/${jobId}/validate-formula`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ formula, name }),
  });
  return parseJsonOrThrow(res, 'Failed to validate formula.');
}

export async function getChartAiStatus(jobId: string): Promise<{ enabled: boolean; model: string }> {
  const res = await apiFetch(`${API_BASE}/api/charts/${jobId}/ai/status`);
  if (!res.ok) {
    return { enabled: false, model: '' };
  }
  return res.json();
}

export async function getVisualizationPlan(
  jobId: string,
  pageOptions?: PageDataOptions,
  forceRefresh: boolean = false,
): Promise<VisualizationPlan> {
  const res = await apiFetch(`${API_BASE}/api/charts/${jobId}/ai/plan`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ page_options: pageOptions, force_refresh: forceRefresh }),
  });
  return parseJsonOrThrow(res, 'Failed to generate visualization plan.');
}

export async function autoVisualizeCharts(
  jobId: string,
  pageOptions?: PageDataOptions,
  maxCharts: number = 6,
  forceRefresh: boolean = false,
): Promise<AutoVisualizeItem[]> {
  const res = await apiFetch(`${API_BASE}/api/charts/${jobId}/ai/auto`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ page_options: pageOptions, max_charts: maxCharts, force_refresh: forceRefresh }),
  });
  const data = await parseJsonOrThrow<{ items?: AutoVisualizeItem[] }>(res, 'Failed to auto-visualize charts.');
  return data.items || [];
}

export async function askChartFromText(
  jobId: string,
  request: string,
  pageOptions?: PageDataOptions,
): Promise<ChartFromTextResult> {
  const res = await apiFetch(`${API_BASE}/api/charts/${jobId}/ai/ask`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ request, page_options: pageOptions }),
  });
  return parseJsonOrThrow(res, 'Failed to ask for chart.');
}

