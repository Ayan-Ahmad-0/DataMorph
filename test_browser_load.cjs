const { chromium } = require('playwright');

async function testUrl(url) {
  console.log(`\n--- Testing ${url} ---`);
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext();
    const page = await context.newPage();

    const consoleLogs = [];
    page.on('console', msg => {
      if (msg.type() === 'error') {
        consoleLogs.push(`[CONSOLE ERROR] ${msg.text()}`);
      }
    });

    const pageErrors = [];
    page.on('pageerror', err => {
      pageErrors.push(`[PAGE ERROR] ${err.message}`);
    });

    const failedRequests = [];
    page.on('requestfailed', req => {
      failedRequests.push(`[FAILED REQ] ${req.method()} ${req.url()} (${req.failure()?.errorText})`);
    });

    const resp = await page.goto(url, { waitUntil: 'networkidle', timeout: 15000 });
    console.log(`HTTP Status: ${resp ? resp.status() : 'No response'}`);
    console.log(`Page Title: ${await page.title()}`);

    const appRoot = await page.$('#root');
    const rootHtml = appRoot ? (await appRoot.innerHTML()).slice(0, 300) : 'NO #root';
    console.log(`Root HTML snippet: ${rootHtml}`);

    if (consoleLogs.length) console.log(consoleLogs.join('\n'));
    if (pageErrors.length) console.log(pageErrors.join('\n'));
    if (failedRequests.length) console.log(failedRequests.join('\n'));

    if (!consoleLogs.length && !pageErrors.length && !failedRequests.length) {
      console.log('SUCCESS: Page rendered with zero errors!');
    }
  } catch (err) {
    console.error(`ERROR visiting ${url}:`, err.message);
  } finally {
    if (browser) await browser.close();
  }
}

(async () => {
  await testUrl('https://datamorph.duckdns.org');
  await testUrl('http://localhost:5173');
})();
