CREATE TABLE IF NOT EXISTS inbox (
  id          TEXT PRIMARY KEY,     -- WhatsApp message id (dedupes re-deliveries)
  kind        TEXT NOT NULL,        -- text | audio | ...
  text        TEXT NOT NULL DEFAULT '',
  received_at INTEGER NOT NULL,     -- epoch ms
  state       INTEGER NOT NULL DEFAULT 0  -- 0 pending, 1 ready, 2 done
);
CREATE INDEX IF NOT EXISTS inbox_state ON inbox (state, received_at);

-- Small key/value state: last_seen (epoch ms of last ESP32 poll), offline_alerted (0/1)
CREATE TABLE IF NOT EXISTS device (
  k TEXT PRIMARY KEY,
  v INTEGER NOT NULL
);
