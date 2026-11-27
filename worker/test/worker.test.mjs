// Run: node --test worker/test
// Exercises the relay with an in-memory D1 stand-in and mocked Meta/Gemini.
import { test } from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import worker from "../src/index.js";

function fakeD1() {
  const rows = new Map();
  const device = new Map();
  const stmt = (sql) => ({
    args: [],
    bind(...a) { return { ...this, args: a }; },
    async first() {
      return device.has(this.args[0]) ? { v: device.get(this.args[0]) } : null;
    },
    async run() {
      const a = this.args;
      if (sql.startsWith("INSERT INTO device")) { device.set(a[0], a[1]); return { meta: { changes: 1 } }; }
      if (sql.startsWith("INSERT OR IGNORE")) {
        if (rows.has(a[0])) return { meta: { changes: 0 } };
        rows.set(a[0], { id: a[0], kind: a[1], text: "", received_at: a[2], state: a[3] });
        return { meta: { changes: 1 } };
      }
      if (sql.startsWith("UPDATE inbox SET text")) Object.assign(rows.get(a[2]), { text: a[0], state: a[1] });
      else if (sql.startsWith("UPDATE inbox SET state")) { const r = rows.get(a[1]); if (r) r.state = a[0]; }
      else if (sql.startsWith("DELETE")) for (const [k, r] of rows) if (r.state === a[0] && r.received_at < a[1]) rows.delete(k);
      return { meta: { changes: 1 } };
    },
    async all() {
      const results = [...rows.values()].filter((r) => r.state === this.args[0])
        .sort((x, y) => x.received_at - y.received_at).slice(0, 3).map(({ id, text }) => ({ id, text }));
      return { results };
    },
  });
  return { rows, device, prepare: stmt, async batch(list) { for (const s of list) await s.run(); } };
}

const env = {
  WHATSAPP_VERIFY_TOKEN: "vt", WHATSAPP_ACCESS_TOKEN: "tok", WHATSAPP_APP_SECRET: "sec",
  WHATSAPP_PHONE_NUMBER_ID: "123", ALLOWED_PHONE_NUMBER: "923000000000",
  GEMINI_API_KEY: "g", DEVICE_API_KEY: "dev",
};

const sent = [];
globalThis.fetch = async (url, init = {}) => {
  url = String(url);
  if (url.endsWith("/123/messages")) { sent.push(JSON.parse(init.body).text.body); return Response.json({}); }
  if (url.endsWith("/media-1")) return Response.json({ url: "https://cdn.example/audio.ogg" });
  if (url === "https://cdn.example/audio.ogg") return new Response(new Uint8Array([1, 2, 3, 4]));
  if (url.includes("generativelanguage")) {
    const body = JSON.parse(init.body);
    assert.equal(body.contents[0].parts[0].inline_data.mime_type, "audio/ogg");
    return Response.json({ candidates: [{ content: { parts: [{ text: "remind me to call Ammi at 8pm" }] } }] });
  }
  throw new Error("unexpected fetch " + url);
};

function ctx() {
  const pending = [];
  return { waitUntil: (p) => pending.push(p), done: () => Promise.all(pending) };
}

function webhook(messages, secret = "sec") {
  const raw = JSON.stringify({ entry: [{ changes: [{ value: { messages } }] }] });
  const sig = "sha256=" + crypto.createHmac("sha256", secret).update(raw).digest("hex");
  return new Request("https://relay/webhook", { method: "POST", body: raw, headers: { "X-Hub-Signature-256": sig } });
}

const call = (req, e, c = ctx()) => worker.fetch(req, e, c);

test("webhook verification handshake", async () => {
  const ok = await call(new Request("https://relay/webhook?hub.mode=subscribe&hub.verify_token=vt&hub.challenge=42"), env);
  assert.equal(await ok.text(), "42");
  const bad = await call(new Request("https://relay/webhook?hub.mode=subscribe&hub.verify_token=no&hub.challenge=42"), env);
  assert.equal(bad.status, 403);
});

test("full flow: signature, dedupe, voice, inbox, ack, reply", async () => {
  const e = { ...env, DB: fakeD1() };

  // bad signature is rejected
  assert.equal((await call(webhook([{ id: "x", from: "923000000000", type: "text", text: { body: "hi" } }], "wrong"), e)).status, 403);

  // text + voice + stranger; text delivered twice
  const msgs = [
    { id: "m1", from: "923000000000", type: "text", text: { body: "add habit read 10 pages" } },
    { id: "m2", from: "923000000000", type: "audio", audio: { id: "media-1", mime_type: "audio/ogg; codecs=opus" } },
    { id: "m3", from: "929999999999", type: "text", text: { body: "spam" } },
  ];
  for (let i = 0; i < 2; i++) {
    const c = ctx();
    assert.equal((await call(webhook(msgs), e, c)).status, 200);
    await c.done();
  }
  assert.equal(e.DB.rows.size, 2, "stranger ignored, duplicates dropped");

  // device auth
  assert.equal((await call(new Request("https://relay/inbox"), e)).status, 403);
  const H = { "X-API-Key": "dev" };
  let inbox = await (await call(new Request("https://relay/inbox", { headers: H }), e)).json();
  assert.deepEqual(inbox.messages.map((m) => m.text), ["add habit read 10 pages", "remind me to call Ammi at 8pm"]);

  // ack one, it disappears
  await call(new Request("https://relay/inbox/ack", { method: "POST", headers: H, body: JSON.stringify({ ids: ["m1"] }) }), e);
  inbox = await (await call(new Request("https://relay/inbox", { headers: H }), e)).json();
  assert.deepEqual(inbox.messages.map((m) => m.id), ["m2"]);

  // reply goes to WhatsApp
  const r = await call(new Request("https://relay/reply", { method: "POST", headers: H, body: JSON.stringify({ text: "Added!" }) }), e);
  assert.equal(r.status, 200);
  assert.equal(sent.at(-1), "Added!");
});

test("offline alert: fires once after 30 min of silence, then 'back online'", async () => {
  const e = { ...env, DB: fakeD1() };
  const H = { "X-API-Key": "dev" };
  const before = sent.length;

  // ESP32 polls -> last_seen recorded
  await call(new Request("https://relay/inbox", { headers: H }), e);
  const seen = e.DB.device.get("last_seen");
  assert.ok(seen > 0);

  // cron 10 min later: still fine
  e.DB.device.set("last_seen", Date.now() - 10 * 60 * 1000);
  await worker.scheduled({}, e, ctx());
  assert.equal(sent.length, before);

  // 40 min of silence: one alert, not repeated
  e.DB.device.set("last_seen", Date.now() - 40 * 60 * 1000);
  await worker.scheduled({}, e, ctx());
  await worker.scheduled({}, e, ctx());
  assert.equal(sent.length, before + 1);
  assert.match(sent.at(-1), /Printer offline since/);

  // ESP32 comes back -> "back online"
  await call(new Request("https://relay/inbox", { headers: H }), e);
  assert.equal(sent.length, before + 2);
  assert.match(sent.at(-1), /back online/);
  assert.equal(e.DB.device.get("offline_alerted"), 0);
});
