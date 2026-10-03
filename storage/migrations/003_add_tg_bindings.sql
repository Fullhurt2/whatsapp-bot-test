-- 003_add_tg_bindings.sql: привязки Telegram для уведомлений менеджеров

-- Привязки чатов менеджеров к клиентам
CREATE TABLE IF NOT EXISTS tg_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_key TEXT NOT NULL,
    chat_id TEXT NOT NULL,                  -- Telegram chat_id (может быть отрицательным для групп)
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(client_key, chat_id)
);

CREATE INDEX IF NOT EXISTS idx_tg_bindings_client_key ON tg_bindings(client_key);

-- Одноразовые коды привязки (хранится хэш)
CREATE TABLE IF NOT EXISTS tg_link_codes (
    code_hash TEXT PRIMARY KEY,             -- SHA-256(code)
    client_key TEXT NOT NULL,
    chat_id TEXT,                           -- заполняется после успешной привязки
    expires_at TEXT NOT NULL,               -- ISO8601, 15 минут от создания
    used_at TEXT                            -- когда код использован
);

CREATE INDEX IF NOT EXISTS idx_tg_link_codes_client_key ON tg_link_codes(client_key);
CREATE INDEX IF NOT EXISTS idx_tg_link_codes_expires_at ON tg_link_codes(expires_at);