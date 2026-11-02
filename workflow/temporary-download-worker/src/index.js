const TTL_MS = 60 * 60 * 1000;
const MAX_APK_BYTES = 500 * 1024 * 1024;
const TOKEN_RE = /^[0-9a-f]{64}$/;
const SHA_RE = /^[0-9a-f]{64}$/;
const KEY_RE = /^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,180}$/;
const FILENAME_RE = /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,100}\.apk$/;
const REQUEST_ID_RE = /^[a-zA-Z0-9_-]{16,100}$/;
const COMMON_HEADERS = {
  'Cache-Control': 'private, no-store, no-cache, max-age=0',
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'X-Robots-Tag': 'noindex, nofollow',
};
const enc = new TextEncoder();

function reply(status, data, extra = {}) {
  const headers = { ...COMMON_HEADERS, ...extra };
  if (data === null) return new Response(null, { status, headers });
  return new Response(JSON.stringify(data), {
    status,
    headers: { ...headers, 'Content-Type': 'application/json; charset=utf-8' },
  });
}

function error(status, code) {
  return reply(status, { error: code });
}

function constantTimeEquals(a, b) {
  const x = enc.encode(a);
  const y = enc.encode(b);
  // Both token values are fixed-length SHA-256 hex digests.
  if (x.length !== y.length) return false;
  let difference = 0;
  for (let i = 0; i < x.length; i++) difference |= x[i] ^ y[i];
  return difference === 0;
}

async function sha256(input) {
  const bytes = typeof input === 'string' ? enc.encode(input) : input;
  return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)))
    .map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

function tokenHex() {
  return Array.from(crypto.getRandomValues(new Uint8Array(32)))
    .map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

function safeObjectKey(key) {
  return typeof key === 'string' && KEY_RE.test(key) &&
    !key.split('/').some((part) => part === '.' || part === '..' || part === '');
}

function publicOrigin(raw) {
  try {
    const url = new URL(raw);
    if (url.protocol !== 'https:' || url.username || url.password ||
        url.port || url.pathname !== '/' || url.search || url.hash || !url.hostname ||
        url.hostname === 'localhost' || url.hostname.endsWith('.local') ||
        url.hostname === 'github.com' || url.hostname.endsWith('.github.com') ||
        url.hostname.endsWith('.githubusercontent.com')) return null;
    return url.origin;
  } catch {
    return null;
  }
}

async function readJson(request) {
  const body = await request.text();
  if (enc.encode(body).length > 4096) return null;
  try {
    const json = JSON.parse(body);
    return json && typeof json === 'object' && !Array.isArray(json) ? json : null;
  } catch {
    return null;
  }
}

function hasOnlyKeys(value, keys) {
  return Object.keys(value).length === keys.length &&
    keys.every((key) => Object.hasOwn(value, key));
}

async function authorized(request, env) {
  const secret = env.ADMIN_TOKEN;
  if (typeof secret !== 'string' || enc.encode(secret).length < 32) return false;
  const auth = request.headers.get('Authorization') || '';
  if (!auth.startsWith('Bearer ') || auth.length > 1024) return false;
  const actual = auth.slice(7);
  return constantTimeEquals(await sha256(actual), await sha256(secret));
}

async function register(request, env, now) {
  const json = await readJson(request);
  if (!json || !hasOnlyKeys(json, ['objectKey', 'size', 'sha256', 'filename']) ||
      !safeObjectKey(json.objectKey) || !Number.isSafeInteger(json.size) ||
      json.size < 1 || json.size > MAX_APK_BYTES ||
      typeof json.sha256 !== 'string' || !SHA_RE.test(json.sha256) ||
      typeof json.filename !== 'string' || !FILENAME_RE.test(json.filename)) {
    return error(400, 'invalid_registration');
  }
  const head = await env.APKS.head(json.objectKey);
  if (!head || head.size !== json.size || !head.etag) {
    return error(409, 'object_missing_or_size_mismatch');
  }

  // A conflicting re-registration cannot silently substitute a different APK.
  await env.DB.prepare(`INSERT INTO apk_objects
      (object_key, size, sha256, r2_etag, filename, registered_at)
      VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(object_key) DO NOTHING`)
    .bind(json.objectKey, json.size, json.sha256, head.etag, json.filename, now).run();
  const saved = await env.DB.prepare(`SELECT object_key, size, sha256, r2_etag, filename
      FROM apk_objects WHERE object_key = ?`).bind(json.objectKey).first();
  if (saved.size !== json.size || saved.sha256 !== json.sha256 ||
      saved.filename !== json.filename || saved.r2_etag !== head.etag) {
    return error(409, 'object_already_registered_differently');
  }
  return reply(200, { status: 'registered', objectKey: saved.object_key,
    size: saved.size, sha256: saved.sha256 });
}

async function issue(request, env, now) {
  const json = await readJson(request);
  if (!json || !hasOnlyKeys(json, ['objectKey', 'kind', 'requestId']) ||
      !safeObjectKey(json.objectKey) ||
      !['canary', 'recipient', 'expiry'].includes(json.kind) ||
      typeof json.requestId !== 'string' || !REQUEST_ID_RE.test(json.requestId)) {
    return error(400, 'invalid_issue_request');
  }
  const origin = publicOrigin(env.PUBLIC_BASE_URL);
  if (!origin) return error(503, 'public_origin_not_configured');
  const record = await env.DB.prepare(`SELECT object_key, size, sha256, r2_etag
      FROM apk_objects WHERE object_key = ?`).bind(json.objectKey).first();
  if (!record) return error(404, 'object_not_registered');
  const head = await env.APKS.head(json.objectKey);
  if (!head || head.size !== record.size || head.etag !== record.r2_etag) {
    return error(503, 'object_unavailable');
  }
  const token = tokenHex();
  const tokenHash = await sha256(token);
  const expiresAt = now + TTL_MS;
  const saved = await env.DB.prepare(`INSERT INTO download_tokens
      (token_hash, request_id, object_key, kind, issued_at, expires_at)
      VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING RETURNING token_hash`)
    .bind(tokenHash, json.requestId, json.objectKey, json.kind, now, expiresAt).first();
  // No replay of plaintext tokens: an ambiguous retry reuses the request ID
  // and receives 409; the original link expires without leaking.
  if (!saved) return error(409, 'issue_request_already_used');
  // This plaintext URL is returned only here. Never put it in logs or CI output.
  return reply(201, { url: `${origin}/d/${token}`, objectKey: record.object_key,
    sha256: record.sha256, size: record.size, kind: json.kind,
    issuedAt: now, expiresAt });
}

async function inspect(request, env, now) {
  const json = await readJson(request);
  if (!json || !hasOnlyKeys(json, ['token']) ||
      typeof json.token !== 'string' || !TOKEN_RE.test(json.token)) {
    return error(400, 'invalid_inspection');
  }
  const tokenHash = await sha256(json.token);
  const saved = await env.DB.prepare(`SELECT t.object_key, t.kind, t.issued_at,
      t.expires_at, t.consumed_at, t.revoked_at, o.size, o.sha256
      FROM download_tokens t JOIN apk_objects o ON o.object_key = t.object_key
      WHERE t.token_hash = ?`).bind(tokenHash).first();
  if (!saved) return error(404, 'unknown_token');
  return reply(200, { objectKey: saved.object_key, kind: saved.kind,
    issuedAt: saved.issued_at, expiresAt: saved.expires_at,
    remainingSeconds: Math.max(0, Math.ceil((saved.expires_at - now) / 1000)),
    consumed: saved.consumed_at !== null, revoked: saved.revoked_at !== null,
    expired: now >= saved.expires_at, sha256: saved.sha256, size: saved.size });
}

async function revoke(request, env, now) {
  const json = await readJson(request);
  if (!json || !hasOnlyKeys(json, ['token']) ||
      typeof json.token !== 'string' || !TOKEN_RE.test(json.token)) {
    return error(400, 'invalid_revoke_request');
  }
  const tokenHash = await sha256(json.token);
  const result = await env.DB.prepare(`UPDATE download_tokens SET revoked_at = ?
      WHERE token_hash = ? AND revoked_at IS NULL AND consumed_at IS NULL
      RETURNING token_hash`).bind(now, tokenHash).first();
  return result ? reply(200, { status: 'revoked' }) : error(404, 'not_revocable');
}

async function download(request, env, token, now) {
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    return error(405, 'method_not_allowed');
  }
  const hash = await sha256(token);
  const saved = await env.DB.prepare(`SELECT t.object_key, t.expires_at,
      t.consumed_at, t.revoked_at, o.size, o.sha256, o.r2_etag, o.filename
      FROM download_tokens t JOIN apk_objects o ON o.object_key = t.object_key
      WHERE t.token_hash = ?`).bind(hash).first();
  if (!saved) return error(404, 'unknown_token');
  if (saved.consumed_at !== null || saved.revoked_at !== null ||
      saved.expires_at <= now) return error(410, 'link_unavailable');
  const headers = {
    ...COMMON_HEADERS,
    'Content-Type': 'application/vnd.android.package-archive',
    'Content-Disposition': `attachment; filename="${saved.filename}"`,
    'Content-Length': String(saved.size),
    'X-Content-SHA256': saved.sha256,
    'Accept-Ranges': 'none',
  };
  if (request.method === 'HEAD') {
    // A download manager may probe metadata before GET. HEAD does not claim.
    const head = await env.APKS.head(saved.object_key);
    if (!head || head.size !== saved.size || head.etag !== saved.r2_etag) {
      return error(503, 'object_unavailable');
    }
    return new Response(null, { status: 200, headers });
  }
  // Always return a full 200, even if the client sent Range. Resumable partial
  // responses would violate the single-redemption contract.
  const apk = await env.APKS.get(saved.object_key);
  if (!apk || !apk.body || apk.size !== saved.size || apk.etag !== saved.r2_etag) {
    return error(503, 'object_unavailable');
  }

  // The UPDATE is one SQLite statement and the only place the link is consumed.
  // D1 evaluates it atomically even for concurrent Workers on separate edges.
  const won = await env.DB.prepare(`UPDATE download_tokens SET consumed_at = ?
      WHERE token_hash = ? AND consumed_at IS NULL AND revoked_at IS NULL
      AND expires_at > ? RETURNING token_hash`).bind(now, hash, now).first();
  if (!won) {
    try { await apk.body.cancel(); } catch { /* Best effort; token was not won. */ }
    return error(410, 'link_unavailable');
  }
  return new Response(apk.body, { status: 200, headers });
}

export function createWorker({ clock = () => Date.now() } = {}) {
  return {
    async fetch(request, env) {
      const { pathname, origin } = new URL(request.url);
      const now = clock();
      try {
        if (pathname === '/healthz') {
          if (request.method !== 'GET') return error(405, 'method_not_allowed');
          return reply(200, { status: 'ok' });
        }
        // An alias host must not be able to steal a link scoped to another origin.
        if ((pathname.startsWith('/d/') || pathname.startsWith('/admin/')) &&
            origin !== publicOrigin(env.PUBLIC_BASE_URL)) {
          return error(421, 'origin_mismatch');
        }
        if (pathname.startsWith('/d/')) {
          const token = pathname.slice(3);
          if (!TOKEN_RE.test(token)) return error(404, 'unknown_token');
          return await download(request, env, token, now);
        }
        if (pathname.startsWith('/admin/')) {
          if (request.method !== 'POST') return error(405, 'method_not_allowed');
          if (!await authorized(request, env)) return error(401, 'unauthorized');
          if (pathname === '/admin/register') return await register(request, env, now);
          if (pathname === '/admin/issue') return await issue(request, env, now);
          if (pathname === '/admin/inspect') return await inspect(request, env, now);
          if (pathname === '/admin/revoke') return await revoke(request, env, now);
        }
        return error(404, 'not_found');
      } catch {
        // Avoid leaking token-bearing paths, authentication values or stack traces.
        return error(503, 'service_unavailable');
      }
    },
  };
}

export default createWorker();
