/**
 * Personal Newspaper — WhatsApp relay (Cloudflare Worker, free tier)
 * ===================================================================
 * The ESP32 at home can't receive Meta's webhook (no public address), so
 * this tiny Worker sits in between:
 *
 *   WhatsApp --webhook--> Worker --(D1 inbox)--> ESP32 polls GET /inbox
 *   ESP32 --POST /reply--> Worker --Graph API--> your WhatsApp
 *
 * It only relays. All the "brain" (Gemini, habits/tasks, schedule,
 * printing) runs on the ESP32. The one exception is voice notes: the Worker
 * downloads the audio and has Gemini transcribe it, so the ESP32 (no PSRAM)
 * only ever deals with text.
 *
 * Secrets (wrangler secret put ...):
 *   WHATSAPP_VERIFY_TOKEN, WHATSAPP_ACCESS_TOKEN, WHATSAPP_APP_SECRET,
 *   WHATSAPP_PHONE_NUMBER_ID, ALLOWED_PHONE_NUMBER, GEMINI_API_KEY,
 *   DEVICE_API_KEY
 */

const GRAPH = "https://graph.facebook.com/v25.0";
const GEMINI_MODEL = "gemini-2.5-flash";

// inbox.state: 0 = still being transcribed, 1 = ready for the ESP32, 2 = done
const STATE_PENDING = 0;
const STATE_READY = 1;
const STATE_DONE = 2;

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    try {
      if (url.pathname === "/webhook") {
        if (request.method === "GET") return verifyWebhook(url, env);
        if (request.method === "POST") return await receiveWebhook(request, env, ctx);
      }

      // Everything below is for the ESP32 only.
      if (!safeEqual(request.headers.get("X-API-Key") || "", env.DEVICE_API_KEY || "")) {
        return json({ error: "forbidden" }, 403);
      }
      if (url.pathname === "/inbox" && request.method === "GET") return await getInbox(env);
      if (url.pathname === "/inbox/ack" && request.method === "POST") return await ackInbox(request, env);
      if (url.pathname === "/reply" && request.method === "POST") return await reply(request, env);
      if (url.pathname === "/health") return json({ ok: true, time: new Date().toISOString() });
      return json({ error: "not found" }, 404);
    } catch (err) {
      console.error(err);
      return json({ error: String(err).slice(0, 200) }, 500);
    }
  },

  // Cron (wrangler.toml [triggers]): warn on WhatsApp if the ESP32 stops polling.
  async scheduled(event, env, ctx) {
    await checkDeviceOnline(env, Date.now());
  },
};

// ---------------------------------------------------------------------------
// Printer online/offline watchdog
// ---------------------------------------------------------------------------
const OFFLINE_AFTER_MS = 30 * 60 * 1000;

async function getState(env, key) {
  const row = await env.DB.prepare("SELECT v FROM device WHERE k = ?").bind(key).first();
  return row ? Number(row.v) : 0;
}

async function setState(env, key, value) {
  await env.DB.prepare("INSERT INTO device (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v")
    .bind(key, value).run();
}

function pkt(ms) {
  return new Date(ms).toLocaleString("en-GB", {
    timeZone: "Asia/Karachi", weekday: "short", hour: "2-digit", minute: "2-digit",
  });
}

// Called on every ESP32 poll. Writes at most once a minute to stay well
// inside D1's free write quota.
async function markDeviceSeen(env, now) {
  const last = await getState(env, "last_seen");
  if (now - last < 60 * 1000) return;
  await setState(env, "last_seen", now);
  if (await getState(env, "offline_alerted")) {
    await setState(env, "offline_alerted", 0);
    await sendWhatsApp(env, `Printer is back online (was offline since ${pkt(last)}).`);
  }
}

async function checkDeviceOnline(env, now) {
  const last = await getState(env, "last_seen");
  if (!last || now - last < OFFLINE_AFTER_MS) return;
  if (await getState(env, "offline_alerted")) return;
  await setState(env, "offline_alerted", 1);
  await sendWhatsApp(env, `Printer offline since ${pkt(last)} - power cut or Wi-Fi down? ` +
    "Your messages are saved and will print when it's back.");
}

// ---------------------------------------------------------------------------
// WhatsApp webhook
// ---------------------------------------------------------------------------
function verifyWebhook(url, env) {
  const p = url.searchParams;
  if (env.WHATSAPP_VERIFY_TOKEN && p.get("hub.mode") === "subscribe" &&
      p.get("hub.verify_token") === env.WHATSAPP_VERIFY_TOKEN) {
    return new Response(p.get("hub.challenge") || "", { status: 200 });
  }
  return new Response("Verification failed", { status: 403 });
}

async function receiveWebhook(request, env, ctx) {
  const raw = await request.text();
  if (!(await validSignature(raw, request.headers.get("X-Hub-Signature-256"), env.WHATSAPP_APP_SECRET))) {
    return new Response("Bad signature", { status: 403 });
  }
  let body;
  try {
    body = JSON.parse(raw);
  } catch {
    return new Response("ok");
  }

  const allowed = String(env.ALLOWED_PHONE_NUMBER || "").replace(/^\+/, "");
  for (const entry of body.entry || []) {
    for (const change of entry.changes || []) {
      for (const msg of change.value?.messages || []) {
        if (!allowed || msg.from !== allowed) continue;
        // Answer Meta right away; transcription etc. continues in the background.
        ctx.waitUntil(ingest(msg, env));
      }
    }
  }
  return new Response("ok");
}

async function ingest(msg, env) {
  const now = Date.now();
  // INSERT OR IGNORE on the WhatsApp message id = Meta re-deliveries are dropped.
  const res = await env.DB.prepare(
    "INSERT OR IGNORE INTO inbox (id, kind, text, received_at, state) VALUES (?, ?, '', ?, ?)"
  ).bind(msg.id, msg.type || "unknown", now, STATE_PENDING).run();
  if (!res.meta || res.meta.changes === 0) return; // duplicate

  let text = "";
  try {
    if (msg.type === "text") {
      text = msg.text?.body || "";
    } else if (msg.type === "audio") {
      text = await transcribeVoiceNote(msg.audio, env);
    } else {
      await sendWhatsApp(env, "I can only handle text and voice notes.");
    }
  } catch (err) {
    console.error("ingest failed", err);
    await sendWhatsApp(env, "Sorry, I couldn't process that voice note. Please try again or type it.");
  }

  await env.DB.prepare("UPDATE inbox SET text = ?, state = ? WHERE id = ?")
    .bind(text, text ? STATE_READY : STATE_DONE, msg.id).run();
}

async function transcribeVoiceNote(audio, env) {
  const auth = { Authorization: `Bearer ${env.WHATSAPP_ACCESS_TOKEN}` };
  const meta = await fetch(`${GRAPH}/${audio.id}`, { headers: auth });
  if (!meta.ok) throw new Error(`media lookup ${meta.status}`);
  const { url } = await meta.json();
  const file = await fetch(url, { headers: auth });
  if (!file.ok) throw new Error(`media download ${file.status}`);
  const bytes = new Uint8Array(await file.arrayBuffer());

  const mime = String(audio.mime_type || "audio/ogg").split(";")[0].trim();
  const r = await fetch(
    `https://generativelanguage.googleapis.com/v1beta/models/${GEMINI_MODEL}:generateContent`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-goog-api-key": env.GEMINI_API_KEY },
      body: JSON.stringify({
        contents: [{
          parts: [
            { inline_data: { mime_type: mime, data: toBase64(bytes) } },
            { text: "Transcribe this voice note exactly. It may be English, Urdu or a mix; " +
                    "write Urdu in Roman Urdu (Latin letters). Output only the transcript." },
          ],
        }],
        generationConfig: { thinkingConfig: { thinkingBudget: 0 } },
      }),
    }
  );
  if (!r.ok) throw new Error(`gemini ${r.status}: ${(await r.text()).slice(0, 200)}`);
  const data = await r.json();
  const transcript = (data.candidates?.[0]?.content?.parts || []).map((p) => p.text || "").join("").trim();
  if (!transcript) throw new Error("empty transcript");
  return transcript;
}

// ---------------------------------------------------------------------------
// ESP32 endpoints
// ---------------------------------------------------------------------------
async function getInbox(env) {
  await markDeviceSeen(env, Date.now());
  const { results } = await env.DB.prepare(
    "SELECT id, text FROM inbox WHERE state = ? ORDER BY received_at ASC LIMIT 3"
  ).bind(STATE_READY).all();
  return json({ messages: results || [] });
}

async function ackInbox(request, env) {
  const { ids } = await request.json();
  if (Array.isArray(ids) && ids.length) {
    const stmt = env.DB.prepare("UPDATE inbox SET state = ? WHERE id = ?");
    await env.DB.batch(ids.slice(0, 20).map((id) => stmt.bind(STATE_DONE, String(id))));
  }
  // Housekeeping: forget handled messages after 7 days.
  await env.DB.prepare("DELETE FROM inbox WHERE state = ? AND received_at < ?")
    .bind(STATE_DONE, Date.now() - 7 * 86400 * 1000).run();
  return json({ ok: true });
}

async function reply(request, env) {
  const { text } = await request.json();
  if (!text) return json({ error: "text required" }, 400);
  const ok = await sendWhatsApp(env, String(text));
  return json({ ok }, ok ? 200 : 502);
}

async function sendWhatsApp(env, text) {
  const r = await fetch(`${GRAPH}/${env.WHATSAPP_PHONE_NUMBER_ID}/messages`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.WHATSAPP_ACCESS_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      messaging_product: "whatsapp",
      to: String(env.ALLOWED_PHONE_NUMBER).replace(/^\+/, ""),
      type: "text",
      text: { body: text.slice(0, 4096) },
    }),
  });
  if (!r.ok) console.error("WhatsApp send failed", r.status, await r.text());
  return r.ok;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
async function validSignature(raw, header, secret) {
  if (!secret) return false; // refuse to run unsigned
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(raw));
  const expected = "sha256=" + [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
  return safeEqual(header || "", expected);
}

function safeEqual(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function toBase64(bytes) {
  let bin = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return btoa(bin);
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
