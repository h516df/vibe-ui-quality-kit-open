import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createWorker } from '../src/index.js';

const ADMIN = 'do-not-use-in-production-' + 'x'.repeat(50);
const SHA = 'a'.repeat(64);
const KEY = 'jiyixia/v0.23/jiyixia.apk';
const BYTES = new TextEncoder().encode('pretend-signed-apk-test-only');
const BASE = 'https://apk-once-test.example.workers.dev';

class MockD1 {
  objects = new Map();
  tokens = new Map();
  requestIds = new Set();

  prepare(sql) {
    return {
      bind: (...args) => ({
        first: async () => this.execute(sql, args),
        run: async () => this.execute(sql, args),
      }),
    };
  }

  execute(sql, args) {
    if (sql.includes('INSERT INTO apk_objects')) {
      const [key, size, sha256, r2_etag, filename, at] = args;
      if (!this.objects.has(key)) this.objects.set(key, {
        object_key: key, size, sha256, r2_etag, filename, registered_at: at,
      });
      return {};
    }
    if (sql.includes('FROM apk_objects WHERE object_key = ?')) {
      return this.objects.get(args[0]) || null;
    }
    if (sql.includes('INSERT INTO download_tokens')) {
      const [token_hash, request_id, object_key, kind, issued_at, expires_at] = args;
      if (this.requestIds.has(request_id) || this.tokens.has(token_hash)) return null;
      this.requestIds.add(request_id);
      this.tokens.set(token_hash, { token_hash, request_id, object_key,
        kind, issued_at, expires_at, consumed_at: null, revoked_at: null });
      return { token_hash };
    }
    if (sql.includes('JOIN apk_objects o ON o.object_key = t.object_key')) {
      const token = this.tokens.get(args[0]);
      const obj = token && this.objects.get(token.object_key);
      return token && obj ? { ...token, ...obj } : null;
    }
    if (sql.includes('UPDATE download_tokens SET consumed_at')) {
      const [at, hash, now] = args;
      const token = this.tokens.get(hash);
      if (!token || token.consumed_at !== null || token.revoked_at !== null ||
          token.expires_at <= now) return null;
      token.consumed_at = at;
      return { token_hash: hash };
    }
    if (sql.includes('UPDATE download_tokens SET revoked_at')) {
      const [at, hash] = args;
      const token = this.tokens.get(hash);
      if (!token || token.consumed_at !== null || token.revoked_at !== null) return null;
      token.revoked_at = at;
      return { token_hash: hash };
    }
    throw new Error(`Unrecognized mock D1 SQL: ${sql}`);
  }
}

function setup() {
  let now = 1_800_000_000_000;
  const data = new Map([[KEY, { bytes: BYTES, etag: 'etag-original' }]]);
  const env = {
    ADMIN_TOKEN: ADMIN, PUBLIC_BASE_URL: BASE, DB: new MockD1(),
    APKS: {
      async head(key) {
        return data.has(key) ? { size: data.get(key).bytes.byteLength,
          etag: data.get(key).etag } : null;
      },
      async get(key) {
        return data.has(key) ? {
          size: data.get(key).bytes.byteLength,
          etag: data.get(key).etag,
          body: new Response(data.get(key).bytes).body,
        } : null;
      },
    },
  };
  const worker = createWorker({ clock: () => now });
  async function post(path, json, bearer = ADMIN) {
    const headers = { 'Content-Type': 'application/json' };
    if (bearer) headers.Authorization = `Bearer ${bearer}`;
    return worker.fetch(new Request(`${BASE}${path}`, {
      method: 'POST', headers, body: JSON.stringify(json),
    }), env);
  }
  async function get(url, init) {
    return worker.fetch(new Request(url, init), env);
  }
  async function register() {
    const response = await post('/admin/register', {
      objectKey: KEY, size: BYTES.length, sha256: SHA, filename: 'app.apk',
    });
    assert.equal(response.status, 200);
  }
  let sequence = 0;
  async function issue(kind = 'canary', requestId) {
    const response = await post('/admin/issue', {
      objectKey: KEY, kind,
      requestId: requestId ?? `test-request-id-${++sequence}`,
    });
    return [response, await response.json()];
  }
  return { env, post, get, register, issue, data,
    get now() { return now; }, set now(value) { now = value; } };
}

test('single-use HTTPS APK with real response payload and non-GitHub origin', async () => {
  const t = setup();
  await t.register();
  const [issued, link] = await t.issue();
  assert.equal(issued.status, 201);
  assert.match(link.url, /^https:\/\/apk-once-test\.example\.workers\.dev\/d\/[0-9a-f]{64}$/);
  assert.equal(link.expiresAt - link.issuedAt, 3_600_000);
  const [a, b] = await Promise.all([t.get(link.url), t.get(link.url)]);
  assert.deepEqual([a.status, b.status].sort(), [200, 410]);
  const winner = a.status === 200 ? a : b;
  assert.equal(winner.headers.get('content-type'), 'application/vnd.android.package-archive');
  assert.equal(winner.headers.get('content-disposition'), 'attachment; filename="app.apk"');
  assert.equal(winner.headers.get('location'), null);
  assert.equal(winner.headers.get('cache-control').includes('no-store'), true);
  assert.deepEqual(new Uint8Array(await winner.arrayBuffer()), BYTES);
  assert.equal((await t.get(link.url)).status, 410);
});

test('inspection does not consume recipient token, revoke is fail-closed', async () => {
  const t = setup();
  await t.register();
  const [, link] = await t.issue('recipient');
  const token = link.url.split('/').pop();
  const inspected = await t.post('/admin/inspect', { token });
  assert.deepEqual((await inspected.json()).remainingSeconds, 3600);
  assert.equal(t.env.DB.tokens.size, 1);
  assert.equal([...t.env.DB.tokens.values()][0].consumed_at, null);
  assert.equal((await t.post('/admin/revoke', { token })).status, 200);
  assert.equal((await t.get(link.url)).status, 410);
  assert.equal((await t.post('/admin/revoke', { token })).status, 404);
});

test('absolute 60-minute boundary and expired canary never returns APK', async () => {
  const t = setup();
  await t.register();
  const [, link] = await t.issue('expiry');
  const token = link.url.split('/').pop();
  t.now = link.expiresAt - 1;
  assert.equal((await t.post('/admin/inspect', { token })).status, 200);
  t.now = link.expiresAt;
  assert.equal((await t.get(link.url)).status, 410);
  const inspect = await (await t.post('/admin/inspect', { token })).json();
  assert.equal(inspect.expired, true);
  assert.equal(inspect.consumed, false);
});

test('HEAD does not consume; Range GET receives a full response, never a 206', async () => {
  const t = setup();
  await t.register();
  const [, link] = await t.issue();
  const head = await t.get(link.url, { method: 'HEAD' });
  assert.equal(head.status, 200);
  assert.equal(head.headers.get('content-length'), String(BYTES.length));
  assert.equal((await t.get(`${BASE}/d/${'f'.repeat(64)}`)).status, 404);
  assert.equal((await t.get(`${BASE}/d/invalid`)).status, 404);
  const ranged = await t.get(link.url, { headers: { Range: 'bytes=0-2' } });
  assert.equal(ranged.status, 200);
  assert.deepEqual(new Uint8Array(await ranged.arrayBuffer()), BYTES);
  assert.equal((await t.get(link.url, { method: 'HEAD' })).status, 410);
});

test('missing R2 object or incorrect size does not consume token', async () => {
  const t = setup();
  await t.register();
  const [, link] = await t.issue();
  t.data.delete(KEY);
  assert.equal((await t.get(link.url)).status, 503);
  const token = [...t.env.DB.tokens.values()][0];
  assert.equal(token.consumed_at, null);
  t.data.set(KEY, { bytes: BYTES, etag: 'etag-original' });
  assert.equal((await t.get(link.url)).status, 200);
});

test('R2 object overwrite is rejected even if replacement has identical size', async () => {
  const t = setup();
  await t.register();
  const [, link] = await t.issue();
  t.data.set(KEY, { bytes: new Uint8Array(BYTES.length), etag: 'etag-overwritten' });
  assert.equal((await t.get(link.url)).status, 503);
  assert.equal([...t.env.DB.tokens.values()][0].consumed_at, null);
  assert.equal((await t.issue())[0].status, 503);
});

test('admin requires secret and pinned public HTTPS origin', async () => {
  const t = setup();
  const value = { objectKey: KEY, size: BYTES.length, sha256: SHA, filename: 'app.apk' };
  assert.equal((await t.post('/admin/register', value, 'wrong')).status, 401);
  assert.equal((await t.post('/admin/register', value, null)).status, 401);
  await t.register();
  t.env.PUBLIC_BASE_URL = 'https://github.com';
  assert.equal((await t.issue())[0].status, 421);
  t.env.PUBLIC_BASE_URL = BASE;
  const [res] = await t.issue();
  assert.equal(res.status, 201);
  assert.equal((await t.get('https://alias.example.net/d/' + 'a'.repeat(64))).status, 421);
});

test('registration is idempotent but rejects conflicting metadata and missing objects', async () => {
  const t = setup();
  await t.register();
  await t.register();
  const value = { objectKey: KEY, size: BYTES.length, sha256: 'b'.repeat(64), filename: 'app.apk' };
  assert.equal((await t.post('/admin/register', value)).status, 409);
  const badSize = { ...value, sha256: SHA, size: BYTES.length + 1 };
  assert.equal((await t.post('/admin/register', badSize)).status, 409);
  t.data.delete(KEY);
  assert.equal((await t.post('/admin/register', { ...value, sha256: SHA })).status, 409);
});

test('issue idempotency key blocks double token creation', async () => {
  const t = setup();
  await t.register();
  const [first] = await t.issue('recipient', 'stable-request-id-2026');
  const [again, result] = await t.issue('recipient', 'stable-request-id-2026');
  assert.equal(first.status, 201);
  assert.equal(again.status, 409);
  assert.equal(result.error, 'issue_request_already_used');
  assert.equal(t.env.DB.tokens.size, 1);
});

test('rejects malformed objects, filenames, issuance and unknown routes', async () => {
  const t = setup();
  assert.equal((await t.post('/admin/register', {
    objectKey: '../secret.apk', size: BYTES.length, sha256: SHA, filename: 'app.apk',
  })).status, 400);
  assert.equal((await t.post('/admin/register', {
    objectKey: KEY, size: BYTES.length, sha256: SHA, filename: 'bad\r\n.apk',
  })).status, 400);
  assert.equal((await t.post('/admin/issue', {
    objectKey: KEY, kind: 'recipient', requestId: 'short',
  })).status, 400);
  assert.equal((await t.get(`${BASE}/healthz`)).status, 200);
  assert.equal((await t.get(`${BASE}/does-not-exist`)).status, 404);
});
