"use strict";
const STORE_KEY = "wa_panel_token";
let role = null;
let currentPid = null;
let clientsData = [];
let newProvider = "wa";       // платформа в форме создания клиента
let currentProvider = "wa";   // платформа открытого клиента (менять нельзя)

const $ = (id) => document.getElementById(id);

function show(id, visible) {
  const el = $(id);
  if (el) el.classList.toggle("hidden", !visible);
}

function status(id, text, isError) {
  const el = typeof id === "string" ? $(id) : id;
  if (!el) return;
  el.textContent = text || "";
  el.classList.toggle("error", Boolean(isError));
  el.classList.toggle("ok", Boolean(text) && !isError);
}

function esc(text) {
  return String(text == null ? "" : text).replace(/[&<>"]/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]
  ));
}

// Переключатель платформы в форме создания: WhatsApp / Zernio / Telegram.
function setNewProvider(provider) {
  newProvider = provider;
  document.querySelectorAll("#newProviderSeg .seg-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.provider === provider);
  });
  const isTg = provider === "tg";
  const isZernio = provider === "zernio";
  show("newPidWrap", !isTg);
  show("newTokenWrap", !isZernio);
  if (isZernio) {
    $("newPidLabel").textContent = "Ключ клиента (имя файла, латиницей)";
    $("newPidHint").textContent = "Например nails-studio. Номер подключите после создания — кнопкой в карточке клиента";
    $("newPid").placeholder = "например nails-studio";
  } else {
    $("newPidLabel").textContent = "phone_number_id";
    $("newPidHint").textContent = "Цифры из WhatsApp → API Setup";
    $("newPid").placeholder = "например 1354249714436396";
  }
  $("newTokenLabel").textContent = isTg
    ? "Токен бота Telegram (от @BotFather)"
    : "Токен номера клиента, access_token";
  $("newTokenHint").textContent = isTg
    ? "Формат 123456789:AA… — ключ клиента определится автоматически"
    : "Можно пусто — будет общий";
  $("newToken").placeholder = isTg ? "123456789:AAHh…" : "";
}

// Поля редактора зависят от платформы клиента (провайдер не меняется).
function applyProviderUI(provider) {
  const isTg = provider === "tg";
  const isZernio = provider === "zernio";
  show("waSecretsWrap", !isTg && !isZernio);
  show("tgSecretsWrap", isTg);
  show("zernioSecretsWrap", isZernio);
  show("ownerWaWrap", !isTg);
  show("ownerTgWrap", isTg);
  $("providerLine").textContent = isTg
    ? "Telegram — бот отвечает в Telegram, уведомления приходят в указанный chat id."
    : (isZernio
      ? "Zernio — бот отвечает через WhatsApp-аккаунт, подключённый в Zernio. Свободный текст возможен только в 24-часовом окне; уведомление владельцу уходит шаблоном."
      : "WhatsApp — бот отвечает через WhatsApp Cloud API.");
}

// Ответ сервера → понятная человеку фраза. Технический код показываем мелкой строкой ниже.
function humanError(code, data) {
  const problems = (data && data.problems) || [];
  const detail = (data && data.error) || problems.join("; ") || "";
  let message;
  if (code === 0) message = detail || "Нет связи, проверьте интернет и попробуйте снова.";
  else if (code === 401 || code === 403) message = "Ключ не подошёл. Проверьте, что скопировали его целиком.";
  else if (code === 404) message = "Не нашли эти настройки. Возможно, клиента уже удалили.";
  else if (code === 422) message = "Проверьте заполнение полей" + (detail ? ": " + detail : ".");
  else if (code >= 500) message = "Что-то на нашей стороне. Попробуйте через минуту.";
  else message = detail || "Что-то пошло не так. Попробуйте ещё раз.";
  return message + (code ? "\n(код " + code + ")" : "");
}

async function api(method, url, body) {
  const token = localStorage.getItem(STORE_KEY) || "";
  const options = { method, headers: { "X-Admin-Token": token, "X-Client-Token": token } };
  if (body instanceof FormData) {
    // Границу multipart и Content-Type проставит сам браузер — руками они не нужны.
    options.body = body;
  } else if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  try {
    const response = await fetch(url, options);
    let data = {};
    try { data = await response.json(); } catch (e) { /* пустое тело */ }
    return { ok: response.ok, code: response.status, data };
  } catch (e) {
    return { ok: false, code: 0, data: { error: "Нет связи с сервером. Проверьте подключение и попробуйте снова." } };
  }
}

async function whoami() {
  const hadToken = Boolean(localStorage.getItem(STORE_KEY));
  const { ok, code, data } = await api("GET", "/admin/whoami");
  if (!ok) {
    show("app", false);
    show("topbarRight", false);
    show("authCard", true);
    // Первый визит без токена — не ругаемся красным, просто ждём ввода.
    status("authStatus", hadToken ? humanError(code, data) : "", hadToken);
    return;
  }
  role = data.role;
  $("roleLine").textContent = role === "admin" ? "Администратор" : "Ваш бизнес";
  show("authCard", false);
  show("topbarRight", true);
  show("app", true);
  const isAdmin = role === "admin";
  $("app").classList.toggle("has-sidebar", isAdmin);
  show("sidebar", isAdmin);
  if (isAdmin) {
    show("editorEmpty", true);
    show("editorCard", false);
    loadList();
    // Ссылка вида /admin#chat=<id> открывает нужного клиента и диалог.
    openChatFromHash();
  } else {
    show("editorEmpty", false);
    await openEditor(data.phone_number_id);
    openChatFromHash();
  }
}

function renderClientList(filterText) {
  const q = (filterText || "").trim().toLowerCase();
  const list = clientsData.filter((c) =>
    !q || (c.business_name || "").toLowerCase().includes(q) || String(c.phone_number_id).includes(q)
  );
  const box = $("clientsList");
  if (!clientsData.length) {
    box.innerHTML = '<div class="list-empty">Пока нет клиентов — добавьте первого.</div>';
    return;
  }
  if (!list.length) {
    box.innerHTML = '<div class="list-empty">Ничего не найдено.</div>';
    return;
  }
  box.innerHTML = list.map((c) => {
    const active = c.phone_number_id === currentPid ? " active" : "";
    const isTg = c.provider === "tg";
    const isZernio = c.provider === "zernio";
    const badge = '<span class="provider-badge">' +
      (isTg ? "Telegram" : (isZernio ? "Zernio" : "WhatsApp")) + "</span>";
    const tokenBits = (isTg
      ? "бот " + c.phone_number_id
      : (isZernio
        ? (c.zernio_account_id ? "аккаунт " + c.zernio_account_id : "номер не подключён")
        : (c.has_own_token ? "свой токен" : "общий токен"))) +
      (c.has_management_token ? " · панель есть" : "");
    return '<div class="client-row' + active + '" data-pid="' + esc(c.phone_number_id) + '">' +
      '<div class="client-row-name">' + esc(c.business_name || "Без названия") + "</div>" +
      '<div class="client-row-sub">' + badge + ' <span class="mono">' + esc(c.phone_number_id) + "</span></div>" +
      '<div class="client-row-sub">' + esc(tokenBits) + "</div>" +
      "</div>";
  }).join("");
  box.querySelectorAll(".client-row").forEach((row) => {
    row.onclick = () => openEditor(row.dataset.pid);
  });
}

function filterClients() {
  renderClientList($("clientSearch").value);
}

async function loadList() {
  const { ok, code, data } = await api("GET", "/admin/clients");
  if (!ok) { status("listStatus", humanError(code, data), true); return; }
  clientsData = data.clients || [];
  renderClientList($("clientSearch") ? $("clientSearch").value : "");
  $("skippedBox").innerHTML = (data.skipped || [])
    .map((s) => "<div>⚠ " + esc(s.file) + ": " + esc((s.problems || []).join("; ")) + "</div>")
    .join("");
}

function switchTab(name) {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === name);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("hidden", panel.id !== "tab-" + name);
  });
  // Load profile data when Profile tab is opened
  if (name === "profile") {
    loadProfile();
    setupProfileCounters();
  }
  // Живой чат: автообновление работает, только пока вкладка открыта.
  if (name === "chat") startChatPolling();
  else stopChatPolling();
  // Остальные вкладки подгружают данные при открытии.
  if (name === "stats") loadStats();
  if (name === "unanswered") loadUnanswered();
  if (name === "telegram") loadTgBindings();
}

function backToList(skipConfirm) {
  if (skipConfirm !== true && !confirmDiscard()) return;
  setDirty(false);
  currentPid = null;
  show("editorCard", false);
  show("editorEmpty", true);
  $("app").classList.remove("editor-open");
  loadList();
}

async function openEditor(pid) {
  const editorOpen = !$("editorCard").classList.contains("hidden");
  if (editorOpen && pid === currentPid) return;   // повторный клик по тому же клиенту не затирает правки
  if (editorOpen && !confirmDiscard()) return;
  show("editorEmpty", false);
  show("editorCard", true);
  const isAdmin = role === "admin";
  if (isAdmin) $("app").classList.add("editor-open");
  const { ok, code, data } = await api("GET", "/admin/clients/" + encodeURIComponent(pid));
  if (!ok) {
    status("editorStatus", humanError(code, data), true);
    return;
  }
  currentPid = pid;
  for (const key of [
    "business_name", "tone", "knowledge_base", "owner_whatsapp_phone",
    "owner_telegram_chat_id", "style_examples", "fallback_reply_ru", "fallback_reply_kk",
    "timeout_reply_ru", "timeout_reply_kk", "access_token", "management_token",
    "telegram_bot_token", "telegram_webhook_secret",
    "zernio_account_id", "owner_template_name", "owner_template_language",
  ]) {
    $("f_" + key).value = data[key] != null ? data[key] : "";
  }
  $("f_fallback_triggers").value = (data.fallback_triggers || []).join(", ");
  const llm = data.llm || {};
  $("f_llm_model").value = llm.model != null ? llm.model : "";
  $("f_llm_temperature").value = llm.temperature != null ? llm.temperature : "";
  $("f_llm_max_tokens").value = llm.max_tokens != null ? llm.max_tokens : "";
  $("f_llm_timeout_seconds").value = llm.timeout_seconds != null ? llm.timeout_seconds : "";
  $("f_llm_reasoning_effort").value = llm.reasoning_effort != null ? llm.reasoning_effort : "";

  const media = data.media || {};
  $("f_media_audio").checked = media.audio !== false;
  $("f_media_image").checked = media.image !== false;
  $("f_media_max_audio_seconds").value = media.max_audio_seconds != null ? media.max_audio_seconds : 120;
  $("f_media_max_image_mb").value = media.max_image_mb != null ? media.max_image_mb : 8;
  $("f_media_daily_limit").value = media.daily_limit != null ? media.daily_limit : 50;
  show("tabAdminBtn", isAdmin);
  show("deleteBtn", isAdmin);
  show("backBtn", isAdmin);
  show("tabStatsBtn", true);
  show("tabTelegramBtn", true);
  show("tabUnansweredBtn", true);
  // Show Profile tab for WhatsApp (wa) and Zernio clients
  const isWhatsApp = ["wa", "zernio"].includes(data.provider);
  show("tabProfileBtn", isWhatsApp);
  currentProvider = ["tg", "zernio"].includes(data.provider) ? data.provider : "wa";
  applyProviderUI(currentProvider);
  // Zernio: сбрасываем состояние блока подключения при открытии другого клиента.
  show("zernioLinkWrap", false);
  $("zernioLink").value = "";
  status("zernioStatus", "");
  status("zernioWebhookStatus", "");
  status("zernioNumberStatus", "");
  $("zernioPin").value = "";
  if (currentProvider === "zernio" && !$("f_zernio_account_id").value.trim()) {
    status("zernioStatus", "Номер ещё не подключён — нажмите «Сгенерировать ссылку подключения» и отправьте её клиенту.");
  }
  $("tgWebhookHint").textContent = currentProvider === "tg"
    ? "Адрес вебхука: " + location.origin + "/webhooks/telegram/" + pid +
      " — привязывается автоматически при сохранении, если в .env задан PUBLIC_BASE_URL."
    : "";
  $("editorTitle").textContent = data.business_name || (isAdmin ? pid : "Ваш бот");
  clientTimezone = data.timezone || "Asia/Almaty";
  $("editorSub").textContent = (isAdmin ? pid : "Настройки вашего бота") +
    " · время в часовом поясе " + clientTimezone;
  $("chatTzHint").textContent = "время: " + clientTimezone;
  // Живой чат: сбрасываем состояние при смене клиента.
  stopChatPolling();
  currentConvId = null;
  chatConvs = [];
  $("chatSearch").value = "";
  $("chatMsgs").innerHTML = '<div class="muted">Выберите диалог слева.</div>';
  document.querySelectorAll("#chatModeSeg .seg-btn").forEach((btn) => btn.classList.remove("active"));
  $("chatModeHint").textContent = "";
  status("chatStatus", "");
  status("statsStatus", "");
  status("uaStatusLine", "");
  status("uaDetailStatus", "");
  status("tgPanelStatus", "");
  closeUaDetail();
  $("statsMetrics").innerHTML = "";
  $("statsHandoffs").innerHTML = "";
  $("statsHourly").innerHTML = "";
  $("statsTopUnanswered").innerHTML = "";
  $("statsOverview").innerHTML = "";
  $("statsOverviewTitle").style.display = "none";
  $("uaList").innerHTML = "";
  $("tgBindings").innerHTML = "";
  show("tgLinkWrap", false);
  $("tgLink").value = "";
  $("tgLinkHint").textContent = "";
  setDirty(false);
  status("editorStatus", "");
  switchTab("business");
  renderClientList($("clientSearch") ? $("clientSearch").value : "");
  updateKnowledgeHelpers();
}

function collectForm() {
  const value = (id) => $(id).value;
  const config = {
    business_name: value("f_business_name").trim(),
    tone: value("f_tone").trim(),
    language: "auto",
    knowledge_base: value("f_knowledge_base"),
    owner_whatsapp_phone: normalizeOwnerPhone(value("f_owner_whatsapp_phone")),
    owner_telegram_chat_id: value("f_owner_telegram_chat_id").trim(),
    fallback_triggers: value("f_fallback_triggers").split(",").map((s) => s.trim()).filter(Boolean),
    style_examples: value("f_style_examples"),
    fallback_reply_ru: value("f_fallback_reply_ru"),
    fallback_reply_kk: value("f_fallback_reply_kk"),
    timeout_reply_ru: value("f_timeout_reply_ru"),
    timeout_reply_kk: value("f_timeout_reply_kk"),
    media: {
      audio: Boolean($("f_media_audio") ? $("f_media_audio").checked : true),
      image: Boolean($("f_media_image") ? $("f_media_image").checked : true),
      max_audio_seconds: parseInt(value("f_media_max_audio_seconds"), 10) || 120,
      max_image_mb: parseInt(value("f_media_max_image_mb"), 10) || 8,
      daily_limit: parseInt(value("f_media_daily_limit"), 10) || 50,
    },
    features: {
      media: Boolean($("f_media_audio") ? $("f_media_audio").checked : true) || Boolean($("f_media_image") ? $("f_media_image").checked : true),
      live_chat: true,
      telegram_notify: true,
      unanswered: true,
      stats: true,
    },
  };
  if (role === "admin") {
    config.management_token = value("f_management_token").trim();
    if (currentProvider === "tg") {
      // Провайдер не меняем у существующего клиента — передаём только токены;
      // маскированные/пустые значения сервер сохраняет как есть.
      config.telegram_bot_token = value("f_telegram_bot_token").trim();
      config.telegram_webhook_secret = value("f_telegram_webhook_secret").trim();
    } else if (currentProvider === "zernio") {
      config.zernio_account_id = value("f_zernio_account_id").trim();
      config.owner_template_name = value("f_owner_template_name").trim();
      config.owner_template_language = value("f_owner_template_language").trim();
    } else {
      config.access_token = value("f_access_token").trim();
    }
    config.llm = {
      model: value("f_llm_model").trim(),
      temperature: parseFloat(value("f_llm_temperature")) || 1.0,
      max_tokens: parseInt(value("f_llm_max_tokens"), 10) || 3500,
      timeout_seconds: parseInt(value("f_llm_timeout_seconds"), 10) || 15,
      reasoning_effort: value("f_llm_reasoning_effort").trim(),
    };
  }
  return config;
}

// Номер владельца: убираем пробелы, скобки и дефисы. Формат, который ждёт
// сервер: плюс, код страны, номер (7–15 цифр). Проверяем до отправки:
// иначе конфиг сохранится, а уведомления владельцу доходить не будут.
function normalizeOwnerPhone(raw) {
  return String(raw || "").replace(/[\s()\-]/g, "");
}

function ownerPhoneError(phone) {
  if (!phone) return "";
  return /^\+?\d{8,15}$/.test(phone)
    ? ""
    : "Номер выглядит неполным. Введите его в международном формате — плюс, код страны, номер: например +77770001122 (или очистите поле, если уведомления не нужны).";
}

async function saveEditor() {
  const payload = collectForm();
  const digits = normalizeOwnerPhone(payload.owner_whatsapp_phone);
  const phoneProblem = ownerPhoneError(digits);
  if (phoneProblem) {
    status("editorStatus", phoneProblem, true);
    return;
  }
  payload.owner_whatsapp_phone = digits;
  const saveBtn = $("saveBtn");
  saveBtn.disabled = true;
  saveBtn.textContent = "Сохраняем…";
  const { ok, code, data } = await api("PUT", "/admin/clients/" + encodeURIComponent(currentPid), payload);
  saveBtn.disabled = false;
  saveBtn.textContent = "Сохранить настройки";
  if (ok) {
    setDirty(false);
    const warnings = (data.warnings || []).length ? "\n" + data.warnings.join("\n") : "";
    status("editorStatus", "Готово. Бот уже отвечает по новым настройкам." + warnings, false);
    if (role === "admin") loadList();
    return;
  }
  status("editorStatus", humanError(code, data), true);
}

async function deleteCurrent() {
  if (!confirm("Удалить клиента " + currentPid + "? Он перестанет отвечать; копия конфига останется в .history.")) return;
  const { ok, code, data } = await api("DELETE", "/admin/clients/" + encodeURIComponent(currentPid));
  if (ok) { backToList(true); return; }
  status("editorStatus", humanError(code, data), true);
}

// --- Zernio: подключение аккаунта из панели ---------------------------------

function zernioApi(method, path, body) {
  return api(method, "/admin/clients/" + encodeURIComponent(currentPid) + path, body);
}

async function zernioConnectLink() {
  const btn = $("zernioLinkBtn");
  btn.disabled = true;
  status("zernioStatus", "Готовим ссылку…");
  const { ok, code, data } = await zernioApi("POST", "/zernio/connect-link",
    { redirect_url: location.origin + "/connect/done" });
  btn.disabled = false;
  if (!ok) { status("zernioStatus", humanError(code, data), true); return; }
  $("zernioLink").value = data.authUrl || "";
  show("zernioLinkWrap", Boolean(data.authUrl));
  status("zernioStatus", "Ссылка готова — отправьте её клиенту. После подключения нажмите «Проверить и сохранить».");
}

async function zernioSyncAccount() {
  const btn = $("zernioSyncBtn");
  btn.disabled = true;
  status("zernioStatus", "Проверяем в Zernio…");
  const { ok, code, data } = await zernioApi("POST", "/zernio/sync-account");
  btn.disabled = false;
  if (!ok) { status("zernioStatus", humanError(code, data), true); return; }
  $("f_zernio_account_id").value = data.accountId || "";
  setDirty(false);   // сервер уже записал accountId в конфиг и перечитал реестр
  status("zernioStatus", "Аккаунт привязан: " + (data.username || data.accountId) + ".");
}

async function zernioRegisterWebhook() {
  const btn = $("zernioWebhookBtn");
  btn.disabled = true;
  status("zernioWebhookStatus", "Регистрируем…");
  const { ok, code, data } = await api("POST", "/admin/zernio/register-webhook");
  btn.disabled = false;
  if (!ok) { status("zernioWebhookStatus", humanError(code, data), true); return; }
  status("zernioWebhookStatus", "Вебхук зарегистрирован: " + (data.url || ""));
}

async function zernioNumberInfo() {
  const btn = $("zernioInfoBtn");
  btn.disabled = true;
  status("zernioNumberStatus", "Запрашиваем статус у Meta…");
  const { ok, code, data } = await zernioApi("GET", "/zernio/number-info");
  btn.disabled = false;
  if (!ok) { status("zernioNumberStatus", humanError(code, data), true); return; }
  const parts = [
    data.displayPhoneNumber,
    "статус: " + (data.status || "—"),
    "имя: " + (data.nameStatus || "—"),
    "качество: " + (data.qualityRating || "—"),
    "лимит: " + (data.messagingLimitTier || "—"),
  ].filter(Boolean);
  status("zernioNumberStatus", parts.join(" · "));
}

async function zernioListAccounts() {
  const btn = $("zernioAccountsBtn");
  btn.disabled = true;
  status("zernioNumberStatus", "Смотрим профиль в Zernio…");
  const { ok, code, data } = await zernioApi("GET", "/zernio/accounts");
  btn.disabled = false;
  if (!ok) { status("zernioNumberStatus", humanError(code, data), true); return; }
  const list = data.accounts || [];
  if (!list.length) {
    status("zernioNumberStatus", "В профиле Zernio нет аккаунтов — клиент не довёл подключение до конца. Отправьте ссылку заново.", true);
    return;
  }
  status("zernioNumberStatus", list.map((a) =>
    a.platform + " " + (a.username || a.accountId) + (a.isActive ? "" : " (неактивен)")).join("; "));
}

async function zernioRegisterNumber() {
  const btn = $("zernioRegisterBtn");
  const pin = $("zernioPin").value.trim();
  if (pin && !/^\d{6}$/.test(pin)) {
    status("zernioNumberStatus", "PIN — ровно 6 цифр (или оставьте пустым для дефолтной регистрации).", true);
    return;
  }
  btn.disabled = true;
  status("zernioNumberStatus", "Регистрируем номер в Meta…");
  const { ok, code, data } = await zernioApi("POST", "/zernio/register-number", { pin: pin });
  btn.disabled = false;
  if (!ok) {
    status("zernioNumberStatus", humanError(code, data) +
      "\nПроверьте PIN: он задаётся в WhatsApp Business (Настройки → Аккаунт → Подтверждение в два шага).", true);
    return;
  }
  status("zernioNumberStatus", "Готово, номер зарегистрирован" +
    (data.phoneNumberId ? " (phoneNumberId " + data.phoneNumberId + ")" : "") + ". Проверьте статус кнопкой выше.");
}

// --- WhatsApp Profile (Meta/Zernio) ------------------------------------------

// Категории бизнеса — закрытый список Meta (VerticalCategory в Graph API).
// Значения приходят из документации business-profiles; свои добавлять нельзя.
const WHATSAPP_VERTICALS = [
  ["", "Не выбрана"],
  ["BEAUTY", "Красота и салоны"],
  ["RESTAURANT", "Рестораны"],
  ["GROCERY", "Продукты и супермаркеты"],
  ["RETAIL", "Розничная торговля"],
  ["APPAREL", "Одежда"],
  ["HOTEL", "Отели"],
  ["TRAVEL", "Путешествия"],
  ["HEALTH", "Здоровье и медицина"],
  ["PROF_SERVICES", "Профессиональные услуги"],
  ["EDU", "Образование"],
  ["ENTERTAIN", "Развлечения"],
  ["EVENT_PLAN", "Мероприятия"],
  ["FINANCE", "Финансы"],
  ["GOVT", "Госструктуры"],
  ["NONPROFIT", "Некоммерческие организации"],
  ["AUTO", "Автотовары"],
  ["ALCOHOL", "Алкоголь"],
  ["OTC_DRUGS", "Лекарства"],
  ["ONLINE_GAMBLING", "Онлайн-ставки"],
  ["PHYSICAL_GAMBLING", "Оффлайн-ставки"],
  ["OTHER", "Другое"],
];

function fillVerticals() {
  const select = $("f_vertical");
  if (!select) return;
  select.innerHTML = WHATSAPP_VERTICALS
    .map(([value, label]) => '<option value="' + esc(value) + '">' + esc(label) + "</option>")
    .join("");
}

fillVerticals();

function updateCounter(inputId, counterId, maxLen) {
  const input = $(inputId);
  const counter = $(counterId);
  if (input && counter) {
    const len = (input.value || "").length;
    counter.textContent = len + "/" + maxLen;
    counter.style.color = len > maxLen ? "var(--danger)" : "#666";
  }
}

function setupProfileCounters() {
  const descInput = $("f_description");
  if (descInput) descInput.addEventListener("input", () => updateCounter("f_description", "descriptionCounter", 512));
}

async function loadProfile() {
  const statusEl = $("profileStatus");
  status(statusEl, "Загружаем профиль…");
  try {
    const { ok, code, data } = await api("GET", "/admin/clients/" + encodeURIComponent(currentPid) + "/profile");
    if (!ok) {
      if (code === 409) {
        status(statusEl, "Профиль недоступен для этого провайдера", true);
      } else {
        status(statusEl, humanError(code, data), true);
      }
      return;
    }
    // Fill form
    $("f_description").value = data.description || "";
    $("f_email").value = data.email || "";
    $("f_websites").value = (data.websites || []).join("\n");
    $("f_vertical").value = data.vertical || "";
    $("f_address").value = data.address || "";
    // Avatar
    const avatarEl = $("avatarCurrent");
    if (data.photo_url) {
      avatarEl.innerHTML = '<img src="' + esc(data.photo_url) + '" style="max-width:100px;max-height:100px;border-radius:8px">';
    } else {
      avatarEl.textContent = "не загружен";
    }
    updateCounter("f_description", "descriptionCounter", 512);
    status(statusEl, "Профиль загружен", false);
  } catch (e) {
    status(statusEl, "Ошибка загрузки: " + e.message, true);
  }
}

async function saveProfile() {
  const payload = {
    description: $("f_description").value.trim(),
    email: $("f_email").value.trim(),
    websites: $("f_websites").value.split("\n").map(s => s.trim()).filter(Boolean),
    vertical: $("f_vertical").value.trim(),
    address: $("f_address").value.trim(),
  };
  const saveBtn = document.querySelector("#tab-profile .btn-primary");
  if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = "Сохраняем…"; }
  try {
    const { ok, code, data } = await api("PATCH", "/admin/clients/" + encodeURIComponent(currentPid) + "/profile", payload);
    if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = "Сохранить профиль"; }
    if (!ok) { status($("profileStatus"), humanError(code, data), true); return; }
    status($("profileStatus"), "Профиль сохранён", false);
    loadProfile();
  } catch (e) {
    if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = "Сохранить профиль"; }
    status($("profileStatus"), "Ошибка: " + e.message, true);
  }
}

async function uploadProfilePhoto() {
  const input = $("f_profile_photo");
  if (!input || !input.files.length) { status($("profileStatus"), "Выберите файл", true); return; }
  const file = input.files[0];
  const allowed = ["image/jpeg", "image/png", "image/webp"];
  if (!allowed.includes(file.type)) { status($("profileStatus"), "Только JPG/PNG/WebP", true); return; }
  if (file.size > 5 * 1024 * 1024) { status($("profileStatus"), "Файл больше 5 МБ", true); return; }
  const formData = new FormData();
  formData.append("file", file);
  const statusEl = $("profileStatus");
  status(statusEl, "Загружаем аватар…");
  try {
    const { ok, code, data } = await api("POST", "/admin/clients/" + encodeURIComponent(currentPid) + "/profile/photo", formData, { headers: {} });
    if (!ok) { status(statusEl, humanError(code, data), true); return; }
    // Reload profile to show new avatar
    await loadProfile();
    input.value = "";
    status(statusEl, "Аватар загружен", false);
  } catch (e) {
    status(statusEl, "Ошибка: " + e.message, true);
  }
}

async function copyZernioLink() {
  const input = $("zernioLink");
  if (!input.value) return;
  try {
    await navigator.clipboard.writeText(input.value);
    status("zernioStatus", "Ссылка скопирована.");
  } catch (e) {
    input.select();
    document.execCommand("copy");
    status("zernioStatus", "Ссылка скопирована.");
  }
}

// --- Живой чат ----------------------------------------------------------------
// Диалоги и сообщения читаются из БД через /admin/clients/{pid}/conversations*.
// Роли сообщений: client — клиент, bot — ответы бота, human — сообщения менеджера.

// Часовой пояс открытого клиента: время в чате и аналитике показываем в нём.
let clientTimezone = "Asia/Almaty";
let chatConvs = [];
let chatConvFilter = "";
let currentConvId = null;
let chatTimer = null;

function chatApi(method, path, body) {
  return api(method, "/admin/clients/" + encodeURIComponent(currentPid) + path, body);
}

// В БД время в UTC («YYYY-MM-DD HH:MM:SS»).
// Показываем в часовом поясе клиента (по умолчанию Алматы), а не в поясе
// браузера: иначе менеджер видит чужое время в чате и в аналитике.
function fmtChatTime(iso) {
  if (!iso) return "";
  const d = new Date(iso.includes("T") ? iso : iso.replace(" ", "T") + "Z");
  if (isNaN(d)) return iso;
  try {
    return d.toLocaleString("ru-RU", {
      timeZone: clientTimezone,
      day: "2-digit", month: "2-digit",
      hour: "2-digit", minute: "2-digit",
    });
  } catch (e) {
    return d.toLocaleString("ru-RU");
  }
}

function renderChatList() {
  const box = $("chatConvList");
  const q = chatConvFilter.trim().toLowerCase();
  const list = !q ? chatConvs : chatConvs.filter((c) =>
    (c.contact_name || "").toLowerCase().includes(q) || (c.contact_phone || "").includes(q)
  );
  if (!list.length) {
    box.innerHTML = '<div class="list-empty">' + (chatConvs.length
      ? "Ничего не найдено."
      : "Диалогов пока нет — они появятся, когда клиенты напишут боту.") + "</div>";
    return;
  }
  box.innerHTML = list.map((c) => {
    const active = c.id === currentConvId ? " active" : "";
    const name = c.contact_name || c.contact_phone;
    const unread = c.unread_count > 0 ? '<span class="chat-unread">' + c.unread_count + "</span>" : "";
    const badge = c.status === "manual" ? '<span class="provider-badge">вручную</span>' : "";

    let preview = "";
    const kind = c.last_message_content_kind || "";
    if (kind === "voice" || kind === "audio") {
      const dur = c.last_message_duration_s ? Math.round(c.last_message_duration_s) : 0;
      const m = Math.floor(dur / 60);
      const s = String(dur % 60).padStart(2, "0");
      preview = "🎤 Голосовое (" + m + ":" + s + ")";
    } else if (kind === "image") {
      let cap = c.last_message_text || "";
      if (cap.startsWith("[Фото]")) {
        cap = cap.replace(/^\[Фото\]\s*/, "");
        if (cap.startsWith("Описание:")) cap = "";
      }
      preview = "📷 Фото" + (cap ? ": " + cap.slice(0, 30) : "");
    } else if (c.last_message_text) {
      preview = c.last_message_text.slice(0, 40);
    }
    const previewHtml = preview ? '<div class="chat-conv-preview" style="font-size:12px;color:#888;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px;">' + esc(preview) + '</div>' : "";

    return '<div class="chat-conv' + active + '" data-cid="' + esc(c.id) + '">' +
      '<div class="chat-conv-name">' + esc(name) + unread + "</div>" +
      '<div class="chat-conv-sub">' + badge + " " + esc(fmtChatTime(c.last_message_at)) + "</div>" +
      previewHtml +
      "</div>";
  }).join("");
  box.querySelectorAll(".chat-conv").forEach((row) => {
    row.onclick = () => openConversation(row.dataset.cid);
  });
}

function filterChats() {
  chatConvFilter = $("chatSearch").value;
  renderChatList();
}

async function refreshChat() {
  const { ok, code, data } = await chatApi("GET", "/conversations?limit=100");
  if (!ok) { status("chatStatus", humanError(code, data), true); return; }
  chatConvs = data.conversations || [];
  renderChatList();
}

function setChatModeUI(conv) {
  document.querySelectorAll("#chatModeSeg .seg-btn").forEach((btn) => {
    btn.classList.toggle("active", conv && btn.dataset.mode === conv.status);
  });
  $("chatModeHint").textContent = !conv ? ""
    : (conv.status === "manual"
      ? "Диалог в ручном режиме — бот не отвечает, отвечаете вы."
      : "Диалог на автопилоте — отвечает бот.");
}

async function openConversation(cid) {
  currentConvId = cid;
  renderChatList();
  status("chatStatus", "");
  setChatModeUI(chatConvs.find((c) => c.id === cid));
  const chatLayout = $("chatLayout");
  if (chatLayout) chatLayout.classList.add("in-conversation");
  const mobileBack = $("chatMobileBack");
  if (mobileBack) mobileBack.classList.remove("hidden");
  await loadConversationMessages();
  // Открыв диалог, сбрасываем счётчик непрочитанных.
  chatApi("POST", "/conversations/" + encodeURIComponent(cid) + "/read").then(() => refreshChat());
}

function closeMobileChat() {
  currentConvId = null;
  const chatLayout = $("chatLayout");
  if (chatLayout) chatLayout.classList.remove("in-conversation");
  const mobileBack = $("chatMobileBack");
  if (mobileBack) mobileBack.classList.add("hidden");
  renderChatList();
}

async function loadConversationMessages() {
  if (!currentConvId) return;
  const { ok, code, data } = await chatApi("GET",
    "/conversations/" + encodeURIComponent(currentConvId) + "/messages?limit=100");
  if (!ok) { status("chatStatus", humanError(code, data), true); return; }
  const box = $("chatMsgs");
  const messages = (data.messages || []).slice().reverse();   // были новые -> старые
  if (!messages.length) {
    box.innerHTML = '<div class="muted">Сообщений пока нет.</div>';
    return;
  }
  box.innerHTML = messages.map((m) => {
    const statusBit = m.role === "human" && m.delivery_status ? " · " + esc(m.delivery_status) : "";
    let mediaHtml = "";
    const tokenParam = "?token=" + encodeURIComponent(localStorage.getItem(STORE_KEY) || "");
    const mediaUrl = "/admin/clients/" + encodeURIComponent(currentPid) +
      "/conversations/" + encodeURIComponent(currentConvId) +
      "/messages/" + m.id + "/media" + tokenParam;

    const kind = (m.content_kind || "").toLowerCase();
    if (kind === "voice" || kind === "audio") {
      const durText = m.media_duration_s ? Math.round(m.media_duration_s) + " с" : "";
      const modelBadge = m.media_model ? '<span class="provider-badge">' + esc(m.media_model) + '</span>' : '<span class="provider-badge">Whisper</span>';
      if (m.media_status === "expired") {
        mediaHtml = '<div class="media-expired" style="font-size:12px;color:#888;margin-bottom:4px;">⚠️ Файл удалён по истечении срока хранения</div>';
      } else if (m.media_status === "failed") {
        mediaHtml = '<div class="media-failed" style="margin-bottom:4px;"><span class="provider-badge" style="background:#fee;color:#c00;">Ошибка расшифровки</span> <button type="button" class="btn btn-sm" style="padding:2px 6px;font-size:11px;" onclick="retryMedia(' + m.id + ')">Повторить расшифровку</button></div>';
      } else if (m.media_path) {
        mediaHtml = '<div class="media-voice" style="margin-bottom:4px;"><audio controls src="' + esc(mediaUrl) + '" preload="none" style="max-width:100%;height:32px;"></audio><div style="font-size:11px;color:#888;margin-top:2px;">' + esc(durText) + ' ' + modelBadge + '</div></div>';
      }
    } else if (kind === "image") {
      const modelBadge = m.media_model ? '<span class="provider-badge">' + esc(m.media_model) + '</span>' : '';
      if (m.media_status === "expired") {
        mediaHtml = '<div class="media-expired" style="font-size:12px;color:#888;margin-bottom:4px;">⚠️ Файл удалён по истечении срока хранения</div>';
      } else if (m.media_status === "failed") {
        mediaHtml = '<div class="media-failed" style="margin-bottom:4px;"><span class="provider-badge" style="background:#fee;color:#c00;">Ошибка распознавания</span> <button type="button" class="btn btn-sm" style="padding:2px 6px;font-size:11px;" onclick="retryMedia(' + m.id + ')">Повторить распознавание</button></div>';
      } else if (m.media_path) {
        mediaHtml = '<div class="media-image" style="margin-bottom:4px;"><a href="' + esc(mediaUrl) + '" target="_blank" rel="noopener"><img src="' + esc(mediaUrl) + '" style="max-width:220px;max-height:220px;border-radius:6px;display:block;cursor:pointer;"></a><div style="font-size:11px;color:#888;margin-top:2px;">' + modelBadge + '</div></div>';
      }
    }

    let textHtml = "";
    if (m.text) {
      if ((kind === "voice" || kind === "audio") && m.text.startsWith("[Голосовое сообщение]")) {
        const transcript = m.text.replace(/^\[Голосовое сообщение\]\s*/, "");
        textHtml = '<div class="transcript-text" style="font-style:italic;color:#555;margin-top:2px;">' + esc(transcript) + '</div>';
      } else if (kind === "image" && m.text.startsWith("[Фото]")) {
        const desc = m.text.replace(/^\[Фото\]\s*/, "");
        textHtml = '<div class="transcript-text" style="color:#444;margin-top:2px;">' + esc(desc) + '</div>';
      } else {
        textHtml = '<div>' + esc(m.text) + '</div>';
      }
    }

    return '<div class="chat-msg ' + esc(m.role) + '">' +
      mediaHtml +
      textHtml +
      '<span class="chat-msg-time">' + esc(fmtChatTime(m.created_at)) + statusBit + "</span></div>";
  }).join("");
  box.scrollTop = box.scrollHeight;
}

async function retryMedia(msgId) {
  if (!currentConvId) return;
  status("chatStatus", "Повторная расшифровка…");
  const { ok, code, data } = await chatApi(
    "POST",
    "/conversations/" + encodeURIComponent(currentConvId) + "/messages/" + msgId + "/retry-media"
  );
  if (!ok) {
    status("chatStatus", humanError(code, data), true);
    return;
  }
  status("chatStatus", "Расшифровка завершена.");
  await loadConversationMessages();
  refreshChat();
}


async function sendChatMessage() {
  const input = $("chatInput");
  const text = input.value.trim();
  if (!text) return;
  if (!currentConvId) { status("chatStatus", "Сначала выберите диалог слева.", true); return; }
  $("chatSendBtn").disabled = true;
  const { ok, code, data } = await chatApi("POST",
    "/conversations/" + encodeURIComponent(currentConvId) + "/messages",
    { text: text, idempotency_key: crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) });
  $("chatSendBtn").disabled = false;
  if (!ok) {
    status("chatStatus", (code === 409 && data.error === "window_closed"
      ? "24-часовое окно закрыто — свободный текст запрещён правилами WhatsApp. Напишите клиенту в самом WhatsApp или дождитесь его сообщения."
      : humanError(code, data)), true);
    return;
  }
  input.value = "";
  status("chatStatus", "Отправлено.");
  await loadConversationMessages();
  refreshChat();
}

async function setChatMode(mode) {
  if (!currentConvId) { status("chatStatus", "Выберите диалог, затем переключите режим.", true); return; }
  const { ok, code, data } = await chatApi("POST",
    "/conversations/" + encodeURIComponent(currentConvId) + "/mode", { mode: mode });
  if (!ok) { status("chatStatus", humanError(code, data), true); return; }
  const conv = chatConvs.find((c) => c.id === currentConvId);
  if (conv) conv.status = mode;
  setChatModeUI(conv);
  renderChatList();
  status("chatStatus", mode === "manual"
    ? "Режим «вручную» включён: бот молчит в этом диалоге."
    : "Режим «бот» включён: бот снова отвечает сам.");
}

// Автообновление раз в 5 секунд, пока открыта вкладка чата.
function startChatPolling() {
  stopChatPolling();
  refreshChat();
  chatTimer = setInterval(() => {
    refreshChat();
    loadConversationMessages();
  }, 2000);
}

function stopChatPolling() {
  if (chatTimer) { clearInterval(chatTimer); chatTimer = null; }
}

// Enter отправляет сообщение на десктопе, Shift+Enter — перенос строки. На мобильных Enter делает перенос.
$("chatInput").addEventListener("keydown", (event) => {
  const isMobile = window.innerWidth <= 760 || navigator.maxTouchPoints > 0;
  if (event.key === "Enter" && !event.shiftKey && !isMobile) {
    event.preventDefault();
    sendChatMessage();
  }
});

// --- Аналитика ------------------------------------------------------------------

function statsQuery() {
  const from = $("stFrom").value;
  const to = $("stTo").value;
  const params = [];
  if (from) params.push("from_date=" + encodeURIComponent(from));
  if (to) params.push("to_date=" + encodeURIComponent(to + "T23:59:59"));
  return params.length ? "?" + params.join("&") : "";
}

function metricCard(label, value, sub) {
  return '<div class="metric"><div class="metric-label">' + esc(label) + "</div>" +
    '<div class="metric-value">' + esc(value) + "</div>" +
    (sub ? '<div class="metric-sub">' + esc(sub) + "</div>" : "") + "</div>";
}

function seconds(ms) {
  return (Number(ms || 0) / 1000).toFixed(1) + " с";
}

async function loadStats() {
  status("statsStatus", "Считаем…");
  const { ok, code, data } = await chatApi("GET", "/stats" + statsQuery());
  if (!ok) { status("statsStatus", humanError(code, data), true); return; }
  const msgs = data.messages || {};
  const closed = data.closed_by_bot || {};
  const reaction = data.manager_reaction || {};
  status("statsStatus", "Период: " + fmtChatTime(data.period && data.period.from) +
    " — " + fmtChatTime(data.period && data.period.to), false);

  $("statsMetrics").innerHTML = [
    metricCard("Диалоги", data.dialogues || 0, "новых контактов: " + (data.new_contacts || 0)),
    metricCard("Сообщения", msgs.total || 0, "клиентов: " + (msgs.client || 0) + " · бота: " + (msgs.bot || 0)),
    metricCard("Закрыто ботом", (closed.percentage || 0) + "%", "диалогов: " + (closed.count || 0)),
    metricCard("Передачи", (data.handoffs || {}).total || 0, "менеджеру"),
    metricCard("Ответ бота (среднее)", seconds((data.response_time || {}).avg_ms),
      "p95: " + seconds((data.response_time || {}).p95_ms)),
    metricCard("Реакция менеджера", (Number(reaction.avg_seconds) || 0).toFixed(1) + " с",
      "p95: " + (Number(reaction.p95_seconds) || 0).toFixed(1) + " с · случаев: " + (reaction.count || 0)),
    metricCard("Сэкономлено времени", (data.estimated_time_saved_minutes || 0) + " мин",
      "ответов бота вместо менеджера"),
    metricCard("Токены", ((data.tokens || {}).in || 0), "вход · выход: " + ((data.tokens || {}).out || 0)),
    metricCard("Медиа", ((data.media || {}).voice || 0) + " гол. / " + ((data.media || {}).image || 0) + " фото",
      "секунд: " + ((data.media || {}).duration_s || 0) + " · расход: $" + ((data.media || {}).cost || 0)),
    metricCard("Без ответа", (data.unanswered || {}).total || 0,
      "новых: " + ((data.unanswered || {}).new_count || 0)),
  ].join("");


  const byReason = (data.handoffs || {}).by_reason || {};
  const reasonKeys = Object.keys(byReason);
  $("statsHandoffs").innerHTML = reasonKeys.length
    ? '<div class="table-scroll"><table class="panel-table"><thead><tr><th>Причина</th><th class="num">Сколько</th></tr></thead><tbody>' +
      reasonKeys.map((r) => "<tr><td>" + esc(r) + '</td><td class="num">' + byReason[r] + "</td></tr>").join("") +
      "</tbody></table></div>"
    : '<div class="muted">Передач за период не было.</div>';

  const hourly = (data.hourly_peak || []).slice().sort((a, b) => a.hour - b.hour);
  if (hourly.length) {
    const max = Math.max.apply(null, hourly.map((h) => h.count || 0)) || 1;
    $("statsHourly").innerHTML = '<div class="bars">' + hourly.map((h) =>
      '<div class="bar-col"><div class="bar" style="height:' +
      Math.max(2, Math.round((h.count || 0) / max * 110)) + 'px" title="' +
      esc(h.hour + ":00 — " + h.count) + '"></div><div class="bar-label">' + h.hour + "</div></div>"
    ).join("") + "</div>";
  } else {
    $("statsHourly").innerHTML = '<div class="muted">Нет сообщений за период.</div>';
  }

  const top = data.top_unanswered || [];
  $("statsTopUnanswered").innerHTML = top.length
    ? '<div class="table-scroll"><table class="panel-table"><thead><tr><th>Тема</th><th class="num">Спросили раз</th></tr></thead><tbody>' +
      top.map((t) => "<tr><td>" + esc(t.name) + '</td><td class="num">' + (t.count || 0) + "</td></tr>").join("") +
      "</tbody></table></div>"
    : '<div class="muted">Тем без ответа пока нет.</div>';

  await loadStatsOverview();
}

async function loadStatsOverview() {
  if (!isAdmin) { $("statsOverview").innerHTML = ""; $("statsOverviewTitle").style.display = "none"; return; }
  const { ok, code, data } = await api("GET", "/admin/stats/overview" + statsQuery());
  if (!ok || !data.clients) { $("statsOverview").innerHTML = ""; $("statsOverviewTitle").style.display = "none"; return; }
  const clients = data.clients || {};
  const pids = Object.keys(clients);
  $("statsOverviewTitle").style.display = "block";
  $("statsOverview").innerHTML =
    '<div class="metrics">' + [
      metricCard("Клиентов", data.total_clients || 0, ""),
      metricCard("Диалогов всего", data.total_dialogues || 0, ""),
      metricCard("Передач всего", data.total_handoffs || 0, ""),
      metricCard("Расход на LLM", "$" + Number(data.estimated_cost_usd || 0).toFixed(4),
        "вход: " + (data.total_tokens_in || 0) + " · выход: " + (data.total_tokens_out || 0)),
    ].join("") + "</div>" +
    (pids.length ? '<div class="table-scroll"><table class="panel-table"><thead><tr><th>Клиент</th>' +
      '<th class="num">Диалоги</th><th class="num">Передачи</th><th class="num">Закрыто ботом</th>' +
      '<th class="num">Токены</th></tr></thead><tbody>' +
      pids.map((pid) => {
        const c = clients[pid];
        return "<tr><td>" + esc(pid) + '</td><td class="num">' + (c.dialogues || 0) +
          '</td><td class="num">' + (c.handoffs || 0) + '</td><td class="num">' +
          (c.closed_by_bot_pct || 0) + '%</td><td class="num">' +
          ((c.tokens_in || 0) + (c.tokens_out || 0)) + "</td></tr>";
      }).join("") + "</tbody></table></div>" : "");
}

async function exportStatsCsv() {
  status("statsStatus", "Готовим файл…");
  const token = localStorage.getItem(STORE_KEY) || "";
  try {
    const response = await fetch(
      "/admin/clients/" + encodeURIComponent(currentPid) + "/stats/export.csv" + statsQuery(),
      { headers: { "X-Admin-Token": token, "X-Client-Token": token } }
    );
    if (!response.ok) { status("statsStatus", "Не удалось выгрузить (код " + response.status + ")", true); return; }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "stats-" + currentPid + ".csv";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
    status("statsStatus", "Файл сохранён.", false);
  } catch (e) {
    status("statsStatus", "Не удалось выгрузить: " + e.message, true);
  }
}

// --- Вопросы без ответа ----------------------------------------------------------

let uaGroups = [];
let uaUngrouped = [];
let uaOpenGroupId = null;

function uaStatusBadge(st) {
  const s = String(st || "").toLowerCase();
  if (s === "active" || s === "new") {
    return '<span class="provider-badge" style="background:#e0f2fe;color:#0369a1;border:1px solid #bae6fd">Без ответа</span>';
  }
  if (s === "answered") {
    return '<span class="provider-badge" style="background:#dcfce7;color:#15803d;border:1px solid #bbf7d0">Отвечен</span>';
  }
  if (s === "ignored") {
    return '<span class="provider-badge" style="background:#f1f5f9;color:#64748b;border:1px solid #cbd5e1">Скрыт</span>';
  }
  return '<span class="provider-badge">' + esc(st) + '</span>';
}

async function loadUnanswered() {
  status("uaStatusLine", "Загружаем…");
  const filter = $("uaStatus").value;
  const { ok, code, data } = await chatApi("GET", "/unanswered" + (filter ? "?status=" + encodeURIComponent(filter) : ""));
  if (!ok) { status("uaStatusLine", humanError(code, data), true); return; }
  uaGroups = data.groups || [];
  uaUngrouped = data.ungrouped || [];
  const total = uaGroups.length + uaUngrouped.length;
  status("uaStatusLine", total
    ? "Групп: " + uaGroups.length + " · отдельных вопросов: " + uaUngrouped.length
    : "Вопросов без ответа пока нет.", false);
  const box = $("uaList");

  // Отдельные вопросы (ещё не сгруппированные) идут первыми: их можно закрыть
  // сразу, не дожидаясь группировки.
  let html = uaUngrouped.length
    ? '<div class="section-title">Отдельные вопросы</div>' + uaUngrouped.map((q) =>
        '<div class="ua-row" data-qid="' + q.id + '">' +
        '<div class="ua-row-head"><span>' + esc(q.question) + "</span>" +
        uaStatusBadge(q.status) + "</div>" +
        '<div class="chat-conv-sub">' + esc(fmtChatTime(q.created_at)) + "</div>" +
        '<div class="row" style="margin-top:8px">' +
        '<input class="ua-q-answer" data-qid="' + q.id + '" placeholder="Ответ — попадёт в базу знаний">' +
        '<div style="flex:0 0 auto;display:flex;gap:6px;"><button type="button" class="btn btn-primary btn-sm ua-q-save" ' +
        'data-qid="' + q.id + '">Сохранить</button>' +
        '<button type="button" class="btn btn-ghost btn-sm ua-q-ignore" data-qid="' + q.id + '" title="Скрыть вопрос">Скрыть</button></div>' +
        "</div></div>").join("")
    : "";

  if (uaGroups.length) {
    html += '<div class="section-title">Группы похожих вопросов</div>' + uaGroups.map((g) => {
      const count = (g.question_ids || []).length;
      return '<div class="ua-row' + (g.id === uaOpenGroupId ? " active" : "") + '" data-gid="' + g.id + '">' +
        '<div class="ua-row-head"><span>' + esc(g.name || "Без названия") + "</span>" +
        uaStatusBadge(g.status) + "</div>" +
        '<div class="chat-conv-sub">вопросов: ' + count + " · " + esc(fmtChatTime(g.updated_at || g.created_at)) + "</div></div>";
    }).join("");
  }

  box.innerHTML = html || '<div class="list-empty">Здесь появятся вопросы клиентов, на которые бот не нашёл ответа в базе знаний.</div>';

  box.querySelectorAll(".ua-row[data-gid]").forEach((row) => {
    row.onclick = () => openUaGroup(Number(row.dataset.gid));
  });
  box.querySelectorAll(".ua-q-save").forEach((btn) => {
    btn.onclick = (event) => {
      event.stopPropagation();
      answerSingleQuestion(Number(btn.dataset.qid));
    };
  });
  box.querySelectorAll(".ua-q-ignore").forEach((btn) => {
    btn.onclick = (event) => {
      event.stopPropagation();
      ignoreSingleQuestion(Number(btn.dataset.qid));
    };
  });
}

async function answerSingleQuestion(questionId) {
  const input = $("uaList").querySelector('.ua-q-answer[data-qid="' + questionId + '"]');
  const answer = input ? input.value.trim() : "";
  if (!answer) { status("uaStatusLine", "Впишите ответ, чтобы добавить его в базу знаний.", true); return; }
  const { ok, code, data } = await chatApi("POST", "/unanswered/question/" + questionId + "/answer", { answer: answer });
  if (!ok) { status("uaStatusLine", humanError(code, data), true); return; }
  status("uaStatusLine", "Ответ добавлен в базу знаний — бот ответит на такой вопрос сам.", false);
  loadUnanswered();
}

async function ignoreSingleQuestion(questionId) {
  const { ok, code, data } = await chatApi("POST", "/unanswered/question/" + questionId + "/ignore");
  if (!ok) { status("uaStatusLine", humanError(code, data), true); return; }
  status("uaStatusLine", "Вопрос скрыт.", false);
  loadUnanswered();
}

async function openUaGroup(groupId) {
  uaOpenGroupId = groupId;
  renderUnansweredList();
  show("uaDetail", true);
  status("uaDetailStatus", "Загружаем вопросы…");
  const { ok, code, data } = await chatApi("GET", "/unanswered/" + groupId + "/questions");
  if (!ok) { status("uaDetailStatus", humanError(code, data), true); return; }
  const questions = data.questions || [];
  $("uaQuestions").innerHTML = questions.length
    ? questions.map((q) => '<div class="ua-question">' + esc(q.question) +
      '<span class="muted">' + esc(fmtChatTime(q.created_at)) +
      (q.answer_text ? " · ответ: " + esc(q.answer_text) : "") + "</span></div>").join("")
    : '<div class="muted">В группе нет вопросов.</div>';
  $("uaAnswer").value = "";
  status("uaDetailStatus", "Вопросов в группе: " + questions.length, false);
}

function renderUnansweredList() {
  loadUnanswered();
}

function closeUaDetail() {
  show("uaDetail", false);
  uaOpenGroupId = null;
  $("uaQuestions").innerHTML = "";
  $("uaAnswer").value = "";
}

async function answerUnanswered() {
  const answer = $("uaAnswer").value.trim();
  if (!answer) { status("uaDetailStatus", "Напишите ответ — он уйдёт в базу знаний.", true); return; }
  const btn = $("uaSaveBtn");
  btn.disabled = true;
  btn.textContent = "Сохраняем…";
  const { ok, code, data } = await chatApi("POST", "/unanswered/" + uaOpenGroupId + "/answer", { answer: answer });
  btn.disabled = false;
  btn.textContent = "Дописать в базу знаний";
  if (!ok) { status("uaDetailStatus", humanError(code, data), true); return; }
  closeUaDetail();
  status("uaStatusLine", "Ответ добавлен в базу знаний — бот ответит на такие вопросы сам.", false);
  loadUnanswered();
}

async function ignoreUnansweredGroup() {
  if (!uaOpenGroupId) return;
  const btn = $("uaIgnoreBtn");
  if (btn) btn.disabled = true;
  const { ok, code, data } = await chatApi("POST", "/unanswered/" + uaOpenGroupId + "/ignore");
  if (btn) btn.disabled = false;
  if (!ok) { status("uaDetailStatus", humanError(code, data), true); return; }
  closeUaDetail();
  status("uaStatusLine", "Группа вопросов скрыта.", false);
  loadUnanswered();
}

async function regroupUnanswered() {
  const btn = $("uaRegroupBtn");
  btn.disabled = true;
  btn.textContent = "Группируем…";
  status("uaStatusLine", "LLM группирует похожие вопросы — это может занять до минуты.");
  const { ok, code, data } = await chatApi("POST", "/unanswered/regroup");
  btn.disabled = false;
  btn.textContent = "Сгруппировать похожие";
  if (!ok) { status("uaStatusLine", humanError(code, data), true); return; }
  status("uaStatusLine", "Создано групп: " + ((data && data.groups_created) || 0), false);
  loadUnanswered();
}

// --- Telegram-менеджер ------------------------------------------------------------

async function loadTgBindings() {
  status("tgPanelStatus", "Загружаем привязки…");
  const { ok, code, data } = await chatApi("GET", "/telegram");
  if (!ok) { status("tgPanelStatus", humanError(code, data), true); return; }
  const bindings = data.bindings || [];
  status("tgPanelStatus",
    "Привязано чатов: " + bindings.length + " из " + (data.max_bindings || 5), false);
  if (!data.owner_bot_configured) {
    status("tgPanelStatus",
      "Бот JAUAP не настроен: в переменных окружения нет TELEGRAM_OWNER_BOT_TOKEN — " +
      "ссылка работать не будет, задайте его и перезапустите сервис.", true);
  }
  $("tgBindings").innerHTML = bindings.length
    ? '<div class="table-scroll"><table class="panel-table"><thead><tr><th>Chat id</th><th>Когда</th><th></th></tr></thead><tbody>' +
      bindings.map((b) => "<tr><td class=\"mono\">" + esc(b.chat_id) + "</td><td>" +
        esc(fmtChatTime(b.created_at)) + '</td><td class="num"><button type="button" class="btn btn-sm btn-danger ' +
        'tg-unbind" data-binding-id="' + b.id + '">Отвязать</button></td></tr>').join("") +
      "</tbody></table></div>"
    : '<div class="muted">Ни один чат не привязан.</div>';
  $("tgBindings").querySelectorAll(".tg-unbind").forEach((btn) => {
    btn.onclick = () => removeTgBinding(Number(btn.dataset.bindingId));
  });
}

async function createTgLinkCode() {
  const btn = $("tgLinkBtn");
  btn.disabled = true;
  btn.textContent = "Создаём…";
  status("tgPanelStatus", "");
  const { ok, code, data } = await chatApi("POST", "/telegram/link-code");
  btn.disabled = false;
  btn.textContent = "Получить ссылку для менеджера";
  if (!ok) { status("tgPanelStatus", humanError(code, data), true); return; }
  $("tgLink").value = data.url || "";
  show("tgLinkWrap", Boolean(data.url));
  $("tgLinkHint").textContent = data.url
    ? "Ссылка действует до " + fmtChatTime(data.expires_at) + ". Отправьте её менеджеру: он нажмёт и привяжет свой чат."
    : "@username бота не задан (TELEGRAM_OWNER_BOT_USERNAME). Код: " + data.code +
      " — отправьте его менеджеру и попросите написать боту /start " + data.code;
  status("tgPanelStatus", data.bound >= data.max_bindings
    ? "Внимание: уже привязано " + data.bound + " из " + data.max_bindings + " чатов."
    : "Готово.", data.bound >= data.max_bindings);
}

function copyTgLink() {
  const input = $("tgLink");
  if (!input.value) return;
  const done = () => { $("tgLinkHint").textContent = "Ссылка скопирована."; };
  if (navigator.clipboard) {
    navigator.clipboard.writeText(input.value).then(done).catch(() => {
      input.select(); document.execCommand("copy"); done();
    });
  } else {
    input.select(); document.execCommand("copy"); done();
  }
}

async function removeTgBinding(bindingId) {
  if (!confirm("Отвязать этот чат? Уведомления в него больше не придут.")) return;
  const { ok, code, data } = await chatApi("DELETE", "/telegram/" + bindingId);
  if (!ok) { status("tgPanelStatus", humanError(code, data), true); return; }
  loadTgBindings();
}

// --- Переход по ссылке из уведомления Telegram ------------------------------------

async function openChatFromHash() {
  const match = /#chat=([A-Za-z0-9-]+)/.exec(location.hash || "");
  if (!match) return;
  const conversationId = match[1];
  history.replaceState(null, "", location.pathname + location.search);

  // Карточка клиента ещё не открыта (админ только зашёл по ссылке) — узнаём,
  // чей это диалог, и откроем нужного клиента, а не первую страницу.
  const editorHidden = $("editorCard").classList.contains("hidden");
  if (editorHidden) {
    const { ok, data } = await api("GET", "/admin/conversations/" + encodeURIComponent(conversationId) + "/client");
    if (!ok) return;
    await openEditor(data.pid);
  }
  if ($("editorCard").classList.contains("hidden")) return;
  switchTab("chat");
  await openConversation(conversationId);
}

window.addEventListener("hashchange", openChatFromHash);

function toggleNew() {
  const opening = $("newCard").classList.contains("hidden");
  show("newCard", opening);
  if (opening) setNewProvider(newProvider);
}

async function createClient() {
  const token = $("newToken").value.trim();
  const payload = {
    business_name: $("newBusiness").value.trim() || "Новый бизнес",
    tone: "вежливый, дружелюбный, на «вы»",
    language: "auto",
    knowledge_base: $("newKnowledge").value || "База знаний пока не заполнена.",
    owner_whatsapp_phone: "",
    fallback_triggers: [],
    style_examples: "",
    management_token: $("newMgmtToken").value.trim(),
  };
  let pid;
  if (newProvider === "tg") {
    // Ключ клиента Telegram — id бота из токена (цифры до «:»).
    if (!/^\d{5,15}:[A-Za-z0-9_-]{20,}$/.test(token)) {
      status("listStatus", "Токен бота Telegram выглядит неполным. Скопируйте его целиком из @BotFather (формат 123456789:AA…).", true);
      return;
    }
    pid = token.split(":")[0];
    payload.provider = "tg";
    payload.telegram_bot_token = token;
  } else if (newProvider === "zernio") {
    // Ключ клиента Zernio — slug (имя файла); номер подключается позже
    // кнопкой «Сгенерировать ссылку подключения» в карточке клиента.
    pid = $("newPid").value.trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(pid)) {
      status("listStatus", "Ключ клиента — латиница, цифры, дефис или подчёркивание (например nails-studio).", true);
      return;
    }
    payload.provider = "zernio";
  } else {
    pid = $("newPid").value.trim();
    if (!/^\d{1,20}$/.test(pid)) {
      status("listStatus", "phone_number_id — только цифры; он показан в WhatsApp → API Setup", true);
      return;
    }
    payload.provider = "wa";
    payload.access_token = token;
  }
  const { ok, code, data } = await api("PUT", "/admin/clients/" + encodeURIComponent(pid), payload);
  if (!ok) {
    status("listStatus", humanError(code, data), true);
    return;
  }
  $("newPid").value = "";
  $("newToken").value = "";
  $("newMgmtToken").value = "";
  show("newCard", false);
  loadList();
  const warnings = data.warnings || [];
  if (warnings.length) status("listStatus", "Клиент создан. " + warnings.join(" "), false);
  // Zernio: сразу открываем карточку — там кнопки подключения номера.
  if (newProvider === "zernio" && role === "admin") openEditor(pid);
}

function login() {
  localStorage.setItem(STORE_KEY, $("tokenInput").value.trim());
  whoami();
}

function logout() {
  localStorage.removeItem(STORE_KEY);
  location.reload();
}

// --- dirty tracking для конфига (не для профиля WhatsApp) --------------------

let dirty = false;

function setDirty(value) {
  dirty = value;
  show("dirtyNote", value);
  if (value) status("editorStatus", "");
}

function confirmDiscard() {
  return !dirty || confirm("Есть несохранённые изменения. Продолжить без сохранения?");
}

// Любая правка настроек бота → «несохранённые изменения».
// Ввод в живом чате к конфигу не относится — там свои кнопки.
function markDirty(event) {
  if (event && event.target && event.target.closest && event.target.closest("#tab-chat")) return;
  setDirty(true);
}

$("editorCard").addEventListener("input", markDirty);
$("editorCard").addEventListener("change", markDirty);

window.addEventListener("beforeunload", (event) => {
  if (dirty) { event.preventDefault(); event.returnValue = ""; }
});

// Enter отправляет формы входа и создания клиента; Ctrl/Cmd+S сохраняет настройки.
$("tokenInput").addEventListener("keydown", (event) => { if (event.key === "Enter") login(); });
["newPid", "newBusiness", "newToken", "newMgmtToken"].forEach((id) => {
  $(id).addEventListener("keydown", (event) => { if (event.key === "Enter") createClient(); });
});
document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s" && !$("editorCard").classList.contains("hidden")) {
    event.preventDefault();
    saveEditor();
  }
});

// --- auto-resize для textarea.auto-resize ---
// Важно: при смене высоты страница на миг меняет длину, и браузер может сбросить
// прокрутку наверх. Запоминаем позицию страницы и курсора и возвращаем их обратно.
function autoResizeTextarea(textarea) {
  if (!textarea.offsetParent) return;             // поле скрыто (другая вкладка / закрытый details) — не мерим
  const pageY = window.scrollY;
  const innerY = textarea.scrollTop;
  textarea.style.height = "auto";
  textarea.style.height = (textarea.scrollHeight + 2) + "px";
  window.scrollTo(0, pageY);
  textarea.scrollTop = innerY;
}

document.querySelectorAll("textarea.auto-resize").forEach((ta) => {
  ta.addEventListener("input", () => autoResizeTextarea(ta));
  // Инициализация при загрузке
  autoResizeTextarea(ta);
});

// Пересчёт при раскрытии <details>: пока блок закрыт, его поля имеют высоту 0,
// и длинный текст обрезался бы до первого нажатия.
document.querySelectorAll("details").forEach((details) => {
  details.addEventListener("toggle", () => {
    if (!details.open) return;
    details.querySelectorAll("textarea.auto-resize").forEach(autoResizeTextarea);
  });
});

// При переключении вкладок тоже обновляем высоту
const originalSwitchTab = switchTab;
switchTab = function(name) {
  originalSwitchTab(name);
  document.querySelectorAll("textarea.auto-resize").forEach((ta) => autoResizeTextarea(ta));
};

// --- образец, счётчик и подсказка для «Информации о бизнесе» ---
const KNOWLEDGE_TEMPLATE = "Адрес:\nЧасы работы:\nУслуги и цены:\nКак записаться:\nЧастые вопросы:";
const KNOWLEDGE_SECTIONS = ["Адрес", "Часы работы", "Услуги и цены", "Как записаться", "Частые вопросы"];

function knowledgeFilledCount(text) {
  return KNOWLEDGE_SECTIONS.filter((name) =>
    new RegExp("^\\s*" + name + "\\s*:\\s*\\S", "m").test(text)
  ).length;
}

// Жёлтая полоска сверху — только клиенту и только пока информация не заполнена.
function updateKnowledgeNotice() {
  if (role === "admin") { show("knowledgeNotice", false); return; }
  const text = ($("f_knowledge_base").value || "").trim();
  show("knowledgeNotice", !text || text === "База знаний пока не заполнена.");
}

function updateKnowledgeHelpers() {
  const ta = $("f_knowledge_base");
  const empty = !ta.value.trim();
  show("knowledgeTemplateRow", empty);
  $("knowledgeProgress").textContent = empty
    ? ""
    : "Заполнено: " + knowledgeFilledCount(ta.value) + " из " + KNOWLEDGE_SECTIONS.length + " разделов";
}

function insertKnowledgeTemplate() {
  const ta = $("f_knowledge_base");
  ta.value = KNOWLEDGE_TEMPLATE;
  autoResizeTextarea(ta);
  updateKnowledgeHelpers();
  updateKnowledgeNotice();
  setDirty(true);
  ta.focus();
}

$("f_knowledge_base").addEventListener("input", () => {
  updateKnowledgeHelpers();
  updateKnowledgeNotice();
});

function togglePassword(inputId) {
  const input = $(inputId);
  const btn = input.parentElement.querySelector(".pwd-toggle");
  const openEye = btn.querySelector(".eye-open");
  const closedEye = btn.querySelector(".eye-closed");
  if (input.type === "password") {
    input.type = "text";
    show(openEye, false);
    show(closedEye, true);
  } else {
    input.type = "password";
    show(openEye, true);
    show(closedEye, false);
  }
}

whoami();
