/**
 * Utilities for Standalone Interactive HTML Dashboard Generation,
 * PDF Executive Briefing Printing, and Portable JSON Configuration Snapshots.
 */

import type { ChartSpec, PageDataOptions, QueryUploadResult } from '../../lib/api';
import type { ChartCardItem } from './ChartCard';

export interface DashboardSnapshot {
  version: '1.0';
  title: string;
  dataset_name: string;
  exported_at: string;
  page_options?: PageDataOptions;
  cards: Array<{
    id: string;
    title?: string;
    question_answered?: string;
    why?: string;
    spec: ChartSpec;
    source?: string;
  }>;
}

/**
 * Generates a self-contained, standalone HTML document containing the entire dashboard
 * with embedded Plotly charts, dark/light toggle, and print stylesheet.
 */
export function generateDashboardHtml(
  title: string,
  datasetInfo: QueryUploadResult,
  cards: ChartCardItem[],
  pageOptions?: PageDataOptions
): string {
  const safeTitle = (title || 'Executive Data Intelligence Briefing').trim();
  const escapedTitle = safeTitle.replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const escapedFilename = datasetInfo.filename.replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const timestamp = new Date().toUTCString();
  const rowCountStr = datasetInfo.row_count ? datasetInfo.row_count.toLocaleString() : 'N/A';

  const cardsPayload = cards.map((c) => ({
    id: c.id,
    title: c.spec.title || `${c.spec.chart_type.toUpperCase()} Chart`,
    spec: c.spec,
    questionAnswered: c.questionAnswered,
    data: {
      columns: c.data?.columns || [],
      rows: c.data?.rows || [],
    },
  }));

  const payloadJson = JSON.stringify({
    title: safeTitle,
    dataset_name: datasetInfo.filename,
    metadata: {
      row_count: datasetInfo.row_count,
      file_size_bytes: datasetInfo.file_size_bytes,
      exported_at: timestamp,
      page_options: pageOptions,
    },
    cards: cardsPayload,
  })
    .replace(/</g, '\\u003c')
    .replace(/>/g, '\\u003e')
    .replace(/&/g, '\\u0026');

  return `<!DOCTYPE html>
<html lang="en" class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>${escapedTitle} — Data Cleaner Report</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    :root {
      --bg-page: #09090b;
      --bg-surface: #121215;
      --bg-subtle: #18181b;
      --border-hairline: rgba(255, 255, 255, 0.08);
      --border-strong: rgba(255, 255, 255, 0.16);
      --text-primary: #f4f4f5;
      --text-secondary: #a1a1aa;
      --text-muted: #71717a;
      --accent: #FF4B3E;
      --accent-subtle: rgba(255, 75, 62, 0.12);
      --card-shadow: 0 4px 20px -2px rgba(0, 0, 0, 0.5);
    }

    html.light {
      --bg-page: #f8fafc;
      --bg-surface: #ffffff;
      --bg-subtle: #f1f5f9;
      --border-hairline: rgba(0, 0, 0, 0.08);
      --border-strong: rgba(0, 0, 0, 0.16);
      --text-primary: #0f172a;
      --text-secondary: #475569;
      --text-muted: #64748b;
      --accent: #FF4B3E;
      --accent-subtle: rgba(255, 75, 62, 0.08);
      --card-shadow: 0 4px 16px -2px rgba(0, 0, 0, 0.06);
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'IBM Plex Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background-color: var(--bg-page);
      color: var(--text-primary);
      line-height: 1.5;
      padding: 2rem 1.5rem 4rem 1.5rem;
      min-height: 100vh;
      transition: background-color 0.2s ease, color 0.2s ease;
    }

    .container {
      max-width: 1400px;
      margin: 0 auto;
    }

    .header-bar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 1.5rem;
      padding-bottom: 1.5rem;
      border-bottom: 1px solid var(--border-hairline);
      margin-bottom: 2rem;
    }

    .header-titles h1 {
      font-size: 1.75rem;
      font-weight: 700;
      letter-spacing: -0.025em;
      margin-bottom: 0.35rem;
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }

    .badge-confidential {
      font-size: 0.65rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      padding: 0.2rem 0.5rem;
      border-radius: 9999px;
      background-color: var(--accent-subtle);
      color: var(--accent);
      border: 1px solid rgba(255, 75, 62, 0.25);
    }

    .meta-tags {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.75rem;
      font-size: 0.8125rem;
      color: var(--text-secondary);
    }

    .meta-tag {
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      padding: 0.15rem 0.6rem;
      border-radius: 0.375rem;
      background-color: var(--bg-subtle);
      border: 1px solid var(--border-hairline);
      font-family: 'IBM Plex Mono', monospace;
      font-size: 0.75rem;
    }

    .header-actions {
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }

    .btn {
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      font-size: 0.8125rem;
      font-weight: 600;
      padding: 0.45rem 0.9rem;
      border-radius: 0.5rem;
      border: 1px solid var(--border-hairline);
      background-color: var(--bg-surface);
      color: var(--text-primary);
      cursor: pointer;
      transition: all 0.15s ease;
      text-decoration: none;
    }

    .btn:hover {
      border-color: var(--border-strong);
      background-color: var(--bg-subtle);
    }

    .btn-primary {
      background-color: var(--accent);
      border-color: var(--accent);
      color: #ffffff;
    }

    .btn-primary:hover {
      background-color: #e03b2f;
      border-color: #e03b2f;
    }

    .dashboard-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 1.25rem;
      width: 100%;
    }

    @media (max-width: 1024px) {
      .dashboard-grid {
        grid-template-columns: 1fr;
      }
    }

    .chart-card {
      background-color: var(--bg-surface);
      border: 1px solid var(--border-hairline);
      border-radius: 0.75rem;
      padding: 1.25rem;
      box-shadow: var(--card-shadow);
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      min-width: 0;
      max-width: 100%;
      overflow: hidden;
      position: relative;
      break-inside: avoid;
      page-break-inside: avoid;
      transition: border-color 0.15s ease;
    }

    .chart-card-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      margin-bottom: 0.75rem;
      padding-bottom: 0.65rem;
      border-bottom: 1px solid var(--border-hairline);
    }

    .chart-card-title {
      font-size: 0.9375rem;
      font-weight: 600;
      color: var(--text-primary);
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }

    .chart-pill {
      font-size: 0.6875rem;
      font-weight: 600;
      text-transform: uppercase;
      padding: 0.15rem 0.5rem;
      border-radius: 9999px;
      background-color: var(--bg-subtle);
      border: 1px solid var(--border-hairline);
      color: var(--text-muted);
    }

    .chart-pill-formula {
      background-color: rgba(99, 102, 241, 0.1);
      color: #818cf8;
      border-color: rgba(99, 102, 241, 0.25);
      font-family: 'IBM Plex Mono', monospace;
    }

    .chart-pill-trend {
      background-color: rgba(245, 158, 11, 0.1);
      color: #fbbf24;
      border-color: rgba(245, 158, 11, 0.25);
      font-family: 'IBM Plex Mono', monospace;
    }

    .chart-body {
      width: 100%;
      min-width: 0;
      max-width: 100%;
      min-height: 360px;
      overflow: hidden;
      position: relative;
    }

    .kpi-container {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      min-height: 320px;
      text-align: center;
    }

    .kpi-value {
      font-size: 3.5rem;
      font-weight: 700;
      font-family: 'IBM Plex Mono', monospace;
      color: var(--accent);
      line-height: 1.1;
      margin-bottom: 0.5rem;
    }

    .kpi-label {
      font-size: 0.875rem;
      color: var(--text-secondary);
      max-width: 380px;
    }

    @media print {
      @page {
        size: letter landscape;
        margin: 0.6cm 0.6cm;
      }
      *, *::before, *::after {
        -webkit-print-color-adjust: exact !important;
        print-color-adjust: exact !important;
      }
      body {
        background-color: #09090b !important;
        color: #f4f4f5 !important;
        padding: 0 !important;
        margin: 0 !important;
      }
      .no-print {
        display: none !important;
      }
      .header-bar {
        padding-bottom: 0.75rem !important;
        margin-bottom: 1rem !important;
        border-bottom: 1px solid rgba(255, 255, 255, 0.2) !important;
      }
      .dashboard-grid {
        display: flex !important;
        flex-direction: row !important;
        flex-wrap: wrap !important;
        justify-content: space-between !important;
        align-items: stretch !important;
        gap: 0.5rem 0 !important;
        width: 100% !important;
      }
      .chart-card {
        display: flex !important;
        flex-direction: column !important;
        justify-content: flex-start !important;
        width: 49.2% !important;
        max-width: 49.2% !important;
        flex: 0 0 49.2% !important;
        height: 275px !important;
        min-height: 275px !important;
        max-height: 275px !important;
        box-sizing: border-box !important;
        background-color: #121215 !important;
        border: 1px solid rgba(255, 255, 255, 0.14) !important;
        border-radius: 0.5rem !important;
        box-shadow: none !important;
        margin-bottom: 0.5rem !important;
        padding: 0.5rem 0.75rem !important;
        break-inside: avoid !important;
        page-break-inside: avoid !important;
        break-inside: avoid-page !important;
        overflow: hidden !important;
      }
      .chart-card-header {
        height: 24px !important;
        min-height: 24px !important;
        max-height: 24px !important;
        margin-bottom: 2px !important;
        padding-bottom: 0 !important;
        border-bottom: 1px solid rgba(255, 255, 255, 0.1) !important;
      }
      .chart-card-title {
        color: #f4f4f5 !important;
        font-size: 0.8125rem !important;
      }
      .chart-pill {
        background-color: #18181b !important;
        color: #a1a1aa !important;
        border-color: rgba(255, 255, 255, 0.1) !important;
        font-size: 0.625rem !important;
        padding: 0.1rem 0.4rem !important;
      }
      .chart-body {
        min-height: 0 !important;
        height: 215px !important;
        max-height: 215px !important;
        flex: 0 0 215px !important;
      }
      .js-plotly-plot,
      .plotly,
      .plot-container {
        height: 215px !important;
        max-height: 215px !important;
      }
      .js-plotly-plot svg.main-svg {
        height: 215px !important;
        max-height: 215px !important;
      }
      .kpi-container {
        min-height: 180px !important;
      }
      .kpi-value {
        font-size: 2.5rem !important;
      }
    }
  </style>
</head>
<body>
  <div class="container">
    <header class="header-bar">
      <div class="header-titles">
        <h1>
          <span>${escapedTitle}</span>
          <span class="badge-confidential">Executive Briefing</span>
        </h1>
        <div class="meta-tags">
          <span class="meta-tag"><strong>File:</strong> ${escapedFilename}</span>
          <span class="meta-tag"><strong>Rows:</strong> ${rowCountStr}</span>
          <span class="meta-tag"><strong>Generated:</strong> ${timestamp}</span>
        </div>
      </div>

      <div class="header-actions no-print">
        <button id="theme-toggle-btn" class="btn" title="Toggle dark/light mode">
          <span id="theme-icon">☀️</span>
          <span id="theme-text">Light Mode</span>
        </button>
        <button onclick="window.print()" class="btn btn-primary" title="Print or save as PDF executive report">
          <span>🖨️ Print / Save PDF</span>
        </button>
      </div>
    </header>

    <main class="dashboard-grid" id="charts-container"></main>
  </div>

  <script id="dashboard-data" type="application/json">
${payloadJson}
  </script>

  <script>
    (function() {
      const dataPayload = JSON.parse(document.getElementById('dashboard-data').textContent);
      const container = document.getElementById('charts-container');
      const themeToggleBtn = document.getElementById('theme-toggle-btn');
      const themeIcon = document.getElementById('theme-icon');
      const themeText = document.getElementById('theme-text');

      let isDark = document.documentElement.classList.contains('dark');

      function updateThemeButtons() {
        if (isDark) {
          themeIcon.textContent = '☀️';
          themeText.textContent = 'Light Mode';
        } else {
          themeIcon.textContent = '🌙';
          themeText.textContent = 'Dark Mode';
        }
      }

      themeToggleBtn.addEventListener('click', () => {
        isDark = !isDark;
        if (isDark) {
          document.documentElement.classList.add('dark');
          document.documentElement.classList.remove('light');
        } else {
          document.documentElement.classList.remove('dark');
          document.documentElement.classList.add('light');
        }
        updateThemeButtons();
        renderAllCharts();
      });

      function calculateSMA(x, y, windowSize) {
        const w = Math.max(2, Math.floor(windowSize));
        if (!y || y.length < w) return null;
        const resX = [], resY = [];
        let sum = 0;
        for (let i = 0; i < w; i++) sum += y[i];
        resX.push(x[w - 1]);
        resY.push(Number((sum / w).toFixed(2)));
        for (let i = w; i < y.length; i++) {
          sum += y[i] - y[i - w];
          resX.push(x[i]);
          resY.push(Number((sum / w).toFixed(2)));
        }
        return { x: resX, y: resY };
      }

      const BRAND_PALETTE = [
        '#FF4B3E', '#3B82F6', '#10B981', '#F59E0B', '#8B5CF6',
        '#EC4899', '#06B6D4', '#84CC16', '#F97316', '#6366F1'
      ];

      function renderAllCharts() {
        container.innerHTML = '';
        const textColor = isDark ? '#e2e8f0' : '#1e293b';
        const tickColor = isDark ? '#94a3b8' : '#64748b';
        const gridColor = isDark ? 'rgba(255, 255, 255, 0.05)' : 'rgba(0, 0, 0, 0.05)';

        const chartTasks = [];

        // 1. Build and append all card containers to DOM first so CSS grid computes exact column widths
        dataPayload.cards.forEach((card, idx) => {
          const spec = card.spec || {};
          const data = card.data || { columns: [], rows: [] };
          const cardEl = document.createElement('div');
          cardEl.className = 'chart-card';

          const cardHeader = document.createElement('div');
          cardHeader.className = 'chart-card-header';

          const titleEl = document.createElement('div');
          titleEl.className = 'chart-card-title';
          titleEl.textContent = card.title || spec.title || \`\${spec.chart_type.toUpperCase()} Chart\`;
          cardHeader.appendChild(titleEl);

          const pillsEl = document.createElement('div');
          pillsEl.style.display = 'flex';
          pillsEl.style.gap = '0.35rem';

          const typePill = document.createElement('span');
          typePill.className = 'chart-pill';
          typePill.textContent = spec.chart_type;
          pillsEl.appendChild(typePill);

          if (spec.y && spec.y.formula) {
            const fxPill = document.createElement('span');
            fxPill.className = 'chart-pill chart-pill-formula';
            fxPill.textContent = \`fx \${spec.y.column}\`;
            pillsEl.appendChild(fxPill);
          }

          if (spec.trendline && spec.trendline.type !== 'none') {
            const trPill = document.createElement('span');
            trPill.className = 'chart-pill chart-pill-trend';
            trPill.textContent = spec.trendline.type.toUpperCase();
            pillsEl.appendChild(trPill);
          }

          cardHeader.appendChild(pillsEl);
          cardEl.appendChild(cardHeader);

          const chartDiv = document.createElement('div');
          chartDiv.className = 'chart-body';
          chartDiv.id = \`chart-elem-\${idx}\`;
          cardEl.appendChild(chartDiv);
          container.appendChild(cardEl);

          if (spec.chart_type === 'big_number') {
            const val = data.rows && data.rows.length > 0 && data.rows[0].length > 0 ? data.rows[0][0] : 0;
            const formatted = typeof val === 'number' ? Number(val).toLocaleString() : String(val);
            const kpiBox = document.createElement('div');
            kpiBox.className = 'kpi-container';
            const kpiVal = document.createElement('div');
            kpiVal.className = 'kpi-value';
            kpiVal.textContent = formatted;
            const kpiLbl = document.createElement('div');
            kpiLbl.className = 'kpi-label';
            kpiLbl.textContent = spec.title || 'Summary Total';
            kpiBox.appendChild(kpiVal);
            kpiBox.appendChild(kpiLbl);
            chartDiv.appendChild(kpiBox);
            return;
          }

          chartTasks.push({ chartDiv, spec, data });
        });

        // 2. Plot all Plotly instances in the next animation frame when widths are computed
        requestAnimationFrame(() => {
          chartTasks.forEach(({ chartDiv, spec, data }) => {
            const primaryColor = spec.custom_color || (spec.chart_type === 'bar' || spec.chart_type === 'histogram' ? '#8F0926' : '#FF4B3E');
            const isPie = spec.chart_type === 'pie' || spec.chart_type === 'donut';
            const isHbar = spec.chart_type === 'hbar';
            const layout = {
              autosize: true,
              height: 360,
              paper_bgcolor: 'transparent',
              plot_bgcolor: 'transparent',
              font: { family: 'IBM Plex Sans, sans-serif', color: textColor, size: 11 },
              margin: isPie ? { l: 20, r: 20, t: 15, b: 35 } : isHbar ? { l: 95, r: 20, t: 15, b: 40 } : { l: 45, r: 20, t: 15, b: 40 },
              showlegend: !!spec.color || isPie || (!!spec.trendline && spec.trendline.type !== 'none'),
              legend: { font: { color: textColor, size: 10 }, orientation: 'h', y: -0.15 }
            };

            if (!isPie) {
              layout.xaxis = { gridcolor: gridColor, tickfont: { color: tickColor, size: 10 }, automargin: true };
              layout.yaxis = { gridcolor: gridColor, tickfont: { color: tickColor, size: 10 }, automargin: true };
            }

            const rows = data.rows || [];
            const traces = [];

            if (spec.chart_type === 'bar') {
              traces.push({
                type: 'bar',
                x: rows.map(r => r[0]),
                y: rows.map(r => Number(r[1]) || 0),
                marker: { color: primaryColor }
              });
            } else if (spec.chart_type === 'hbar') {
              traces.push({
                type: 'bar',
                orientation: 'h',
                y: rows.map(r => String(r[0])).reverse(),
                x: rows.map(r => Number(r[1]) || 0).reverse(),
                marker: { color: primaryColor }
              });
            } else if (spec.chart_type === 'line' || spec.chart_type === 'area') {
              const xVals = rows.map(r => r[0]);
              const yVals = rows.map(r => Number(r[1]) || 0);
              traces.push({
                type: 'scatter',
                mode: 'lines+markers',
                name: 'Series',
                x: xVals,
                y: yVals,
                fill: spec.chart_type === 'area' ? 'tozeroy' : undefined,
                line: { color: primaryColor, width: 2.5 },
                marker: { color: primaryColor, size: 5 }
              });

              if (spec.trendline && spec.trendline.type === 'sma') {
                const sma = calculateSMA(xVals, yVals, spec.trendline.window || 7);
                if (sma) {
                  traces.push({
                    type: 'scatter',
                    mode: 'lines',
                    name: \`SMA (\${spec.trendline.window || 7})\`,
                    x: sma.x,
                    y: sma.y,
                    line: { color: spec.trendline.color || '#f59e0b', width: 2, dash: 'dash' }
                  });
                }
              }
            } else if (spec.chart_type === 'pie' || spec.chart_type === 'donut') {
              traces.push({
                type: 'pie',
                labels: rows.map(r => String(r[0])),
                values: rows.map(r => Number(r[1]) || 0),
                hole: spec.chart_type === 'donut' ? 0.55 : 0,
                domain: { x: [0, 1], y: [0, 1] },
                textposition: 'inside',
                textinfo: 'percent',
                hoverinfo: 'label+value+percent',
                marker: {
                  colors: BRAND_PALETTE,
                  line: { color: isDark ? '#121215' : '#ffffff', width: 2 }
                }
              });
            } else if (spec.chart_type === 'scatter') {
              traces.push({
                type: 'scatter',
                mode: 'markers',
                x: rows.map(r => Number(r[0])),
                y: rows.map(r => Number(r[1])),
                marker: { color: primaryColor, size: 7 }
              });
            } else if (spec.chart_type === 'heatmap') {
              const xSet = Array.from(new Set(rows.map(r => String(r[0]))));
              const ySet = Array.from(new Set(rows.map(r => String(r[1]))));
              const zMatrix = ySet.map(() => xSet.map(() => 0));

              rows.forEach(r => {
                const xIdx = xSet.indexOf(String(r[0]));
                const yIdx = ySet.indexOf(String(r[1]));
                if (xIdx !== -1 && yIdx !== -1) {
                  zMatrix[yIdx][xIdx] = Number(r[2]) || 0;
                }
              });

              const textMatrix = zMatrix.map(row =>
                row.map(val => (val == null || val === 0 ? '0' : Number(val).toLocaleString()))
              );
              const cellFontSize = xSet.length > 25 || ySet.length > 25 ? 0 : xSet.length > 15 ? 9 : 11;

              const colorscale = isDark
                ? [[0, '#09090b'], [0.25, '#1e3a8a'], [0.55, '#2563eb'], [0.8, '#60a5fa'], [1.0, '#bfdbfe']]
                : [[0, '#eff6ff'], [0.3, '#93c5fd'], [0.65, '#2563eb'], [1.0, '#1e3a8a']];

              traces.push({
                type: 'heatmap',
                x: xSet,
                y: ySet,
                z: zMatrix,
                ...(cellFontSize > 0 ? {
                  text: textMatrix,
                  texttemplate: '<b>%{text}</b>',
                  textfont: { size: cellFontSize, color: textColor }
                } : {}),
                xgap: 2,
                ygap: 2,
                colorscale: colorscale,
                colorbar: {
                  thickness: 12,
                  len: 0.85,
                  outlinewidth: 0,
                  tickfont: { color: tickColor, size: 10 }
                },
                hovertemplate: '<b>%{y}</b> • <b>%{x}</b><br>Value: <b>%{z:,}</b><extra></extra>'
              });

              layout.margin.l = 85;
              layout.margin.b = 55;
              layout.xaxis.type = 'category';
              layout.xaxis.showgrid = false;
              layout.yaxis.type = 'category';
              layout.yaxis.autorange = 'reversed';
              layout.yaxis.showgrid = false;
            } else if (spec.chart_type === 'histogram') {
              const xLabels = rows.map(r => \`\${r[0]} - \${r[1]}\`);
              const yValues = rows.map(r => Number(r[2]) || 0);
              traces.push({
                type: 'bar',
                x: xLabels,
                y: yValues,
                marker: { color: primaryColor }
              });
              layout.xaxis.type = 'category';
            } else if (spec.chart_type === 'box') {
              rows.forEach((r, bIdx) => {
                const color = BRAND_PALETTE[bIdx % BRAND_PALETTE.length];
                traces.push({
                  type: 'box',
                  name: String(r[0]),
                  lowerfence: [r[2]],
                  q1: [r[3]],
                  median: [r[4]],
                  q3: [r[5]],
                  upperfence: [r[6]],
                  marker: { color: color },
                  line: { color: color, width: 1.5 }
                });
              });
              layout.xaxis.type = 'category';
            } else {
              traces.push({
                type: 'bar',
                x: rows.map(r => r[0]),
                y: rows.map(r => Number(r[1]) || 0),
                marker: { color: primaryColor }
              });
            }

            Plotly.newPlot(chartDiv.id, traces, layout, { responsive: true, displayModeBar: false })
              .then(() => {
                if (window.Plotly) Plotly.Plots.resize(chartDiv);
              });
          });
        });
      }

      renderAllCharts();

      const resizeCharts = (printHeight) => {
        dataPayload.cards.forEach((_, idx) => {
          const elem = document.getElementById(\`chart-elem-\${idx}\`);
          if (elem && window.Plotly) {
            if (printHeight) {
              Plotly.relayout(elem, { height: printHeight, autosize: true });
            } else {
              Plotly.Plots.resize(elem);
            }
          }
        });
      };

      window.addEventListener('resize', () => resizeCharts());
      window.addEventListener('beforeprint', () => resizeCharts(215));
      window.addEventListener('afterprint', () => resizeCharts(360));
      if (window.ResizeObserver) {
        const ro = new ResizeObserver(() => resizeCharts());
        ro.observe(container);
      }
    })();
  </script>
</body>
</html>`;
}

/**
 * Triggers instant client-side browser download of standalone interactive HTML report.
 */
export function downloadDashboardHtml(
  title: string,
  datasetInfo: QueryUploadResult,
  cards: ChartCardItem[],
  pageOptions?: PageDataOptions
): void {
  const html = generateDashboardHtml(title, datasetInfo, cards, pageOptions);
  const blob = new Blob([html], { type: 'text/html;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  const sanitizedName = (datasetInfo.filename || 'dataset').replace(/[^a-zA-Z0-9_-]/g, '_');
  a.href = url;
  a.download = `${sanitizedName}_executive_briefing.html`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

/**
 * Triggers an instant download of the dashboard layout configuration as a shareable JSON file.
 */
export function downloadDashboardJson(
  title: string,
  datasetInfo: QueryUploadResult,
  cards: ChartCardItem[],
  pageOptions?: PageDataOptions
): void {
  const snapshot: DashboardSnapshot = {
    version: '1.0',
    title: title || 'Executive Data Intelligence Briefing',
    dataset_name: datasetInfo.filename,
    exported_at: new Date().toISOString(),
    page_options: pageOptions,
    cards: cards.map((c) => ({
      id: c.id,
      title: c.spec.title || `${c.spec.chart_type.toUpperCase()} Chart`,
      question_answered: c.questionAnswered,
      why: c.why,
      spec: c.spec,
      source: c.source,
    })),
  };

  const blob = new Blob([JSON.stringify(snapshot, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  const sanitizedName = (datasetInfo.filename || 'dataset').replace(/[^a-zA-Z0-9_-]/g, '_');
  a.href = url;
  a.download = `${sanitizedName}_dashboard.json`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

/**
 * Copies a compact JSON snapshot of the dashboard layout to clipboard.
 */
export async function copyDashboardSnapshotText(
  title: string,
  datasetInfo: QueryUploadResult,
  cards: ChartCardItem[],
  pageOptions?: PageDataOptions
): Promise<boolean> {
  try {
    const snapshot: DashboardSnapshot = {
      version: '1.0',
      title: title || 'Executive Data Intelligence Briefing',
      dataset_name: datasetInfo.filename,
      exported_at: new Date().toISOString(),
      page_options: pageOptions,
      cards: cards.map((c) => ({
        id: c.id,
        title: c.spec.title || `${c.spec.chart_type.toUpperCase()} Chart`,
        question_answered: c.questionAnswered,
        why: c.why,
        spec: c.spec,
        source: c.source,
      })),
    };
    await navigator.clipboard.writeText(JSON.stringify(snapshot, null, 2));
    return true;
  } catch (err) {
    console.error('Failed to copy dashboard snapshot:', err);
    return false;
  }
}

/**
 * Parses and validates an uploaded .dashboard.json file.
 */
export async function parseDashboardJsonFile(file: File): Promise<DashboardSnapshot> {
  const text = await file.text();
  const parsed = JSON.parse(text);
  if (!parsed || !Array.isArray(parsed.cards)) {
    throw new Error('Invalid dashboard configuration file: missing cards array.');
  }
  return parsed as DashboardSnapshot;
}

/**
 * Triggers native browser print/save-to-PDF flow.
 */
export function triggerPdfPrint(): void {
  document.documentElement.classList.add('is-printing');
  window.dispatchEvent(new Event('beforeprint'));
  window.dispatchEvent(new Event('resize'));

  // 250ms allows React and Plotly to re-render all chart canvases to print geometry
  setTimeout(() => {
    window.print();
    setTimeout(() => {
      document.documentElement.classList.remove('is-printing');
      window.dispatchEvent(new Event('afterprint'));
      window.dispatchEvent(new Event('resize'));
    }, 500);
  }, 250);
}
