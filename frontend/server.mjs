import { createServer } from 'node:http';
import { readFileSync } from 'node:fs';
import { Readable } from 'node:stream';
import { parseEnv } from 'node:util';

let local = {};
try {
  local = parseEnv(readFileSync(new URL('../.env', import.meta.url), 'utf8'));
} catch (error) {
  if (error.code !== 'ENOENT') throw error;
}
const config = { ...local, ...process.env };
const port = Number(config.KRAFTSYNC_UI_PORT || 5173);
const scriptPolicy = config.NODE_ENV === 'production' ? "'self'" : "'self' 'unsafe-eval'";
const upstream = new URL(config.KRAFTSYNC_API_URL || 'http://127.0.0.1:' + (config.KRAFTSYNC_PORT || 8000));
if (upstream.username || upstream.password ||
    (upstream.protocol !== 'https:' && !(upstream.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(upstream.hostname)))) {
  throw new Error('Use HTTPS or a loopback HTTP URL for KRAFTSYNC_API_URL.');
}
const assets = new Map([
  ['/', ['index.html', 'text/html']],
  ['/style.css', ['style.css', 'text/css']],
  ['/dashboard.js', ['dashboard.js', 'text/javascript']],
  ['/graph.js', ['graph.js', 'text/javascript']],
  ['/data.js', ['data.js', 'text/javascript']],
  ['/bim-viewer.js', ['../build/bim-viewer.js', 'text/javascript']],
].map(([route, [file, type]]) => [route, [readFileSync(new URL('./public/' + file, import.meta.url)), type]]));
const reads = new Map([['/api/state', '/state'], ['/api/events', '/events'], ['/api/actions', '/actions?status=all']]);

createServer(async (request, response) => {
  function send(status, body, contentType = 'application/json') {
    response.writeHead(status, {
      'Content-Type': contentType === 'application/wasm' ? contentType : contentType + '; charset=utf-8',
      'Cache-Control': 'no-store',
      'X-Content-Type-Options': 'nosniff',
      'Content-Security-Policy': `default-src 'self'; script-src ${scriptPolicy}; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; frame-src 'self' blob:; frame-ancestors 'none'`,
    });
    response.end(body);
  }
  const error = (status, message) => send(status, JSON.stringify({ detail: message }));
  if (request.headers.host !== '127.0.0.1:' + port) return error(403, 'Use the local dashboard URL.');
  const path = request.url.split('?')[0];
  if (assets.has(path)) return send(200, ...assets.get(path));
  if (path === '/ifc-wasm/web-ifc.wasm') {
    return send(200, readFileSync(new URL('./node_modules/web-ifc/web-ifc.wasm', import.meta.url)), 'application/wasm');
  }
  const contentMatch = /^\/api\/documents\/([a-f0-9]{32})\/content$/.exec(path);
  const documentRoute = path === '/api/documents' || contentMatch;
  const upload = path === '/api/documents' && request.method === 'POST';
  if ((!reads.has(path) && !documentRoute) || (upload ? request.method !== 'POST' : request.method !== 'GET')) {
    return error(request.method === 'GET' || upload ? 404 : 405, 'Page unavailable.');
  }
  if (!config.KRAFTSYNC_TOKEN) return error(503, 'Set KRAFTSYNC_TOKEN and restart the dashboard.');
  try {
    const apiPath = reads.get(path) || (contentMatch ? '/documents/' + contentMatch[1] + '/content' : '/documents');
    const target = new URL(apiPath + (path === '/api/documents' ? '' : new URL(request.url, 'http://localhost').search), upstream);
    const headers = { 'X-KraftSync-Token': config.KRAFTSYNC_TOKEN };
    if (upload && request.headers['content-type']) headers['Content-Type'] = request.headers['content-type'];
    const result = await fetch(target, {
      method: upload ? 'POST' : 'GET',
      headers,
      ...(upload ? { body: request, duplex: 'half' } : {}),
      signal: AbortSignal.timeout(upload || contentMatch ? 600000 : 4500),
      redirect: 'manual',
    });
    if (result.status === 401 || result.status === 403) return error(401, 'Backend rejected the configured token.');
    if (contentMatch) {
      response.writeHead(result.status, {
        'Content-Type': result.headers.get('content-type') || 'application/octet-stream',
        'Content-Disposition': result.headers.get('content-disposition') || 'attachment',
        'Cache-Control': 'no-store',
        'X-Content-Type-Options': 'nosniff',
      });
      if (result.body) Readable.fromWeb(result.body).pipe(response);
      else response.end();
      return;
    }
    try {
      const body = await result.text();
      send(result.status, body, result.headers.get('content-type') || 'application/json');
    } catch {
      error(502, 'Backend response could not be read.');
    }
  } catch {
    error(502, 'KraftSync backend is unreachable.');
  }
}).listen(port, '127.0.0.1', () => console.log('KraftSync dashboard at http://127.0.0.1:' + port));
