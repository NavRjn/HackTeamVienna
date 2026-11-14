import { createServer } from 'node:http';
import { readFileSync } from 'node:fs';
import { parseEnv } from 'node:util';

let local = {};
try {
  local = parseEnv(readFileSync(new URL('../.env', import.meta.url), 'utf8'));
} catch (error) {
  if (error.code !== 'ENOENT') throw error;
}
const config = { ...local, ...process.env };
const port = Number(config.KRAFTSYNC_UI_PORT || 5173);
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
].map(([route, [file, type]]) => [route, [readFileSync(new URL('./public/' + file, import.meta.url)), type]]));
const reads = new Map([['/api/state', '/state'], ['/api/events', '/events']]);

createServer(async (request, response) => {
  function send(status, body, contentType = 'application/json') {
    response.writeHead(status, {
      'Content-Type': contentType + '; charset=utf-8',
      'Cache-Control': 'no-store',
      'X-Content-Type-Options': 'nosniff',
      'Content-Security-Policy': "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
    });
    response.end(body);
  }
  const error = (status, message) => send(status, JSON.stringify({ detail: message }));
  if (request.headers.host !== '127.0.0.1:' + port) return error(403, 'Use the local dashboard URL.');
  if (request.method !== 'GET') return error(405, 'Only read requests are supported.');
  const path = request.url.split('?')[0];
  if (assets.has(path)) return send(200, ...assets.get(path));
  if (!reads.has(path)) return error(404, 'Page unavailable.');
  if (!config.KRAFTSYNC_TOKEN) return error(503, 'Set KRAFTSYNC_TOKEN and restart the dashboard.');
  try {
    const result = await fetch(new URL(reads.get(path), upstream), {
      headers: { 'X-KraftSync-Token': config.KRAFTSYNC_TOKEN },
      signal: AbortSignal.timeout(4500),
      redirect: 'manual',
    });
    if (result.status === 401 || result.status === 403) return error(401, 'Backend rejected the configured token.');
    if (!result.ok) return error(502, 'KraftSync backend returned HTTP ' + result.status + '.');
    if (!result.headers.get('content-type')?.includes('application/json')) return error(502, 'Unexpected backend response.');
    try {
      send(200, JSON.stringify(await result.json()));
    } catch {
      error(502, 'Backend JSON could not be read.');
    }
  } catch {
    error(502, 'KraftSync backend is unreachable.');
  }
}).listen(port, '127.0.0.1', () => console.log('KraftSync dashboard at http://127.0.0.1:' + port));
