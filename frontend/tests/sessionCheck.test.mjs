// node --test frontend/tests/   (also run by tests/test_frontend_logic.py)
import assert from "node:assert/strict";
import { test } from "node:test";

import { checkStoredSession, classify, RETRY_DELAYS_MS } from "../src/lib/sessionCheck.mjs";

const USER = { id: 1, email: "demo@example.com" };
const fail = (status) => Object.assign(new Error(`HTTP ${status}`), { status });

/** A /auth/me that fails with each given error in turn, then succeeds. */
function scripted(...errors) {
  const calls = { n: 0 };
  const check = async () => {
    const error = errors[calls.n];
    calls.n += 1;
    if (error) throw error;
    return USER;
  };
  return { check, calls };
}

function harness() {
  const slept = [];
  const waiting = [];
  return {
    slept,
    waiting,
    options: { sleep: async (ms) => void slept.push(ms), onWaiting: (n) => waiting.push(n) },
  };
}

test("a valid token is adopted on the first try, with no waiting notice", async () => {
  const { check, calls } = scripted();
  const h = harness();
  assert.deepEqual(await checkStoredSession(check, h.options), { outcome: "valid", user: USER });
  assert.equal(calls.n, 1);
  assert.deepEqual(h.waiting, []);
});

test("401 clears the session at once, without retrying", async () => {
  const { check, calls } = scripted(fail(401));
  const h = harness();
  assert.deepEqual(await checkStoredSession(check, h.options), { outcome: "invalid" });
  assert.equal(calls.n, 1);
  assert.deepEqual(h.slept, []);
});

test("429 keeps the session, shows the waking notice and retries with backoff", async () => {
  const { check, calls } = scripted(fail(429), fail(429));
  const h = harness();
  assert.deepEqual(await checkStoredSession(check, h.options), { outcome: "valid", user: USER });
  assert.equal(calls.n, 3);
  assert.deepEqual(h.waiting, [1, 2]);
  assert.deepEqual(h.slept, RETRY_DELAYS_MS.slice(0, 2));
});

test("a timeout or network error (status 0) keeps the session and retries", async () => {
  const timeout = Object.assign(new Error("Cannot reach the API"), { status: 0 });
  const { check, calls } = scripted(timeout, new TypeError("fetch failed"));
  const h = harness();
  assert.deepEqual(await checkStoredSession(check, h.options), { outcome: "valid", user: USER });
  assert.equal(calls.n, 3);
  assert.deepEqual(h.waiting, [1, 2]);
});

test("5xx keeps the session and retries", async () => {
  const { check, calls } = scripted(fail(502), fail(503), fail(500));
  const h = harness();
  assert.deepEqual(await checkStoredSession(check, h.options), { outcome: "valid", user: USER });
  assert.equal(calls.n, 4);
});

test("an API that never answers leaves the session kept, not cleared", async () => {
  const errors = Array.from({ length: RETRY_DELAYS_MS.length + 1 }, () => fail(503));
  const { check, calls } = scripted(...errors);
  const h = harness();
  assert.deepEqual(await checkStoredSession(check, h.options), { outcome: "unverified" });
  assert.equal(calls.n, RETRY_DELAYS_MS.length + 1);
  assert.deepEqual(h.slept, RETRY_DELAYS_MS);
});

test("the backoff waits grow and stay bounded", () => {
  for (let i = 1; i < RETRY_DELAYS_MS.length; i += 1) assert.ok(RETRY_DELAYS_MS[i] >= RETRY_DELAYS_MS[i - 1]);
  assert.ok(Math.max(...RETRY_DELAYS_MS) <= 15000);
});

test("a 401 after a retry still clears the session", async () => {
  const { check } = scripted(fail(429), fail(401));
  assert.deepEqual(await checkStoredSession(check, harness().options), { outcome: "invalid" });
});

test("another status (404) keeps the session without retrying", async () => {
  const { check, calls } = scripted(fail(404));
  assert.deepEqual(await checkStoredSession(check, harness().options), { outcome: "unverified" });
  assert.equal(calls.n, 1);
});

test("a page that has gone away stops retrying", async () => {
  const { check, calls } = scripted(fail(503), fail(503), fail(503));
  let alive = true;
  const options = { sleep: async () => void (alive = false), isAlive: () => alive };
  assert.deepEqual(await checkStoredSession(check, options), { outcome: "unverified" });
  assert.equal(calls.n, 1);
});

test("classify reads only the status", () => {
  assert.equal(classify(fail(401)), "invalid");
  for (const s of [0, 408, 429, 500, 502, 503, 504]) assert.equal(classify(fail(s)), "retry", String(s));
  for (const s of [400, 403, 404, 422]) assert.equal(classify(fail(s)), "keep", String(s));
  assert.equal(classify(new TypeError("fetch failed")), "retry");
  assert.equal(classify(undefined), "retry");
});
