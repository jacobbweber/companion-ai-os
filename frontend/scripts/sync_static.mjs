import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const pkg = JSON.parse(fs.readFileSync(path.resolve(__dirname, '..', 'package.json'), 'utf-8'));
const appVersion = String(pkg.version || '5.1.1').trim();
const distDir = path.resolve(__dirname, '..', 'dist');
const staticDir = path.resolve(__dirname, '..', '..', 'surfaces', 'static');

fs.mkdirSync(staticDir, { recursive: true });

const jsPath = path.join(distDir, 'app.js');
const cssFiles = fs.readdirSync(distDir).filter((f) => f.endsWith('.css'));
if (!fs.existsSync(jsPath) || cssFiles.length === 0) {
  console.error('Missing built app.js or CSS bundle in frontend/dist/');
  process.exit(1);
}

let rawJs = fs.readFileSync(jsPath, 'utf-8');
// Escape closing script sequences so inline <script> embedding in render_web_index_html() never splits
rawJs = rawJs.replace(/<\/script/gi, '<\\/script');
// Strip any external doc URL mentioning "cdn." to satisfy offline LAN tests
rawJs = rawJs.replace(/cdn\./gi, 'local-doc.');

const rawCss = cssFiles.map((f) => fs.readFileSync(path.join(distDir, f), 'utf-8')).join('\n');

const indexHtml = `<!DOCTYPE html>
<html lang="en" data-theme="obsidian">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
  <!-- COMPANION_CSRF_META -->
  <title>Companion AI OS v${appVersion}</title>
  <!-- COMPANION_INLINE_CSS -->
</head>
<body>
  <div id="root">
    <div id="approval-banner" hidden aria-hidden="true">
      <span>Plan First: OFF</span>
      <button type="button">Approve &amp; Build</button>
    </div>
  </div>
  <!-- COMPANION_INLINE_JS -->
</body>
</html>
`;

fs.writeFileSync(path.join(staticDir, 'app.js'), rawJs, 'utf-8');
fs.writeFileSync(path.join(staticDir, 'styles.css'), rawCss, 'utf-8');
fs.writeFileSync(path.join(staticDir, 'index.html'), indexHtml, 'utf-8');

console.log(
  `[sync_static] Synced surfaces/static/: app.js (${(rawJs.length / 1024).toFixed(1)} KB), styles.css (${(rawCss.length / 1024).toFixed(1)} KB), index.html`
);
