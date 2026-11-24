"use strict";

// Главная страница. Общие функции — в common.js.

let maxInputChars = 15000;
let maxFileMb = 10;
let level = "detailed";
let lastCards = null;
let lastShareUrl = "";

const fileInput = $("fileInput");
const fileDrop = $("fileDrop");
const dropLabel = $("dropLabel");
const textInput = $("textInput");
const submitBtn = $("submitBtn");
const DROP_DEFAULT = dropLabel.textContent;

// ------------------------------------------------------------
// Лимиты и остаток квоты — одним запросом
// ------------------------------------------------------------
function showQuota(remaining, limit) {
  const el = $("quotaHint");
  el.textContent = remaining > 0
    ? `Осталось ${remaining} из ${limit} генераций сегодня`
    : `Дневной лимит (${limit}) исчерпан — вернётся завтра`;
  el.classList.toggle("low", remaining <= 1);
}

function updateCounter() {
  const len = textInput.value.length;
  const over = len > maxInputChars;
  const n = (x) => x.toLocaleString("ru-RU");
  $("charCounter").textContent = over
    ? `${n(len)} / ${n(maxInputChars)} — лишнее обрежется`
    : `${n(len)} / ${n(maxInputChars)} символов`;
  $("charCounter").classList.toggle("over", over);
}

fetch("/api/config")
  .then((r) => r.json())
  .then((d) => {
    maxInputChars = d.max_input_chars;
    maxFileMb = d.max_file_size_mb;
    $("limitHint").textContent =
      `До ${maxInputChars.toLocaleString("ru-RU")} символов текста или файл до ${maxFileMb} МБ`;
    showQuota(d.quota_remaining, d.quota_limit);
    updateCounter();
  })
  .catch(() => {});

textInput.addEventListener("input", updateCounter);
updateCounter();

// ------------------------------------------------------------
// Переключатели (вкладки и уровень подробности)
// ------------------------------------------------------------
function segmented(group, onChange) {
  group.addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    group.querySelectorAll("button").forEach((b) => {
      b.classList.toggle("active", b === btn);
      b.setAttribute("aria-pressed", String(b === btn));
    });
    onChange(btn.dataset.value);
  });
}

let activeTab = "file";
segmented($("tabs"), (v) => {
  activeTab = v;
  $("tab-file").hidden = v !== "file";
  $("tab-text").hidden = v !== "text";
});
segmented($("levelToggle"), (v) => (level = v));

// ------------------------------------------------------------
// Выбор файла: клик и drag & drop
// ------------------------------------------------------------
function showFile(file) {
  dropLabel.textContent = file ? file.name : DROP_DEFAULT;
  fileDrop.classList.toggle("has-file", Boolean(file));
}

// Firefox после F5 может вернуть старый выбранный файл — сбрасываем.
window.addEventListener("pageshow", () => {
  fileInput.value = "";
  showFile(null);
});

fileInput.addEventListener("change", () => showFile(fileInput.files[0]));

["dragenter", "dragover"].forEach((t) =>
  fileDrop.addEventListener(t, (e) => {
    e.preventDefault();
    fileDrop.classList.add("drag-over");
  })
);

["dragleave", "drop"].forEach((t) =>
  fileDrop.addEventListener(t, (e) => {
    e.preventDefault();
    fileDrop.classList.remove("drag-over");
  })
);

fileDrop.addEventListener("drop", (e) => {
  if (e.dataTransfer.files.length) {
    fileInput.files = e.dataTransfer.files;
    showFile(fileInput.files[0]);
  }
});

// ------------------------------------------------------------
// История (localStorage) и диалог
// ------------------------------------------------------------
const HISTORY_KEY = "nauticards_history";
const HISTORY_LIMIT = 30;
const historyDialog = $("historyDialog");

function loadHistory() {
  try {
    return JSON.parse(localStorage.getItem(HISTORY_KEY)) || [];
  } catch {
    return [];
  }
}

function saveHistory(list) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(list.slice(0, HISTORY_LIMIT)));
  } catch { /* хранилище недоступно — история просто не сохранится */ }
  $("historyCount").textContent = list.length;
}

function renderHistory() {
  const list = loadHistory();
  $("historyList").innerHTML = list.length
    ? list.map((i) => `
        <a class="history-item" href="${esc(i.url)}" target="_blank" rel="noopener">
          <b>${esc(i.title)}</b><span>${esc(i.count)} карт. · ${esc(i.date)}</span>
        </a>`).join("")
    : '<p class="empty">Пока пусто — сгенерируй первые карточки.</p>';
  $("clearHistoryBtn").hidden = !list.length;
}

$("historyBtn").addEventListener("click", () => {
  renderHistory();
  historyDialog.showModal();
});
$("historyClose").addEventListener("click", () => historyDialog.close());
historyDialog.addEventListener("click", (e) => {
  if (e.target === historyDialog) historyDialog.close();
});
$("clearHistoryBtn").addEventListener("click", () => {
  if (confirm("Очистить историю? Сами наборы по ссылкам останутся доступны.")) {
    saveHistory([]);
    renderHistory();
  }
});

$("historyCount").textContent = loadHistory().length;

// ------------------------------------------------------------
// Поделиться и скачать
// ------------------------------------------------------------
const shareRow = $("shareRow");
const shareInput = $("shareLinkInput");

$("shareBtn").addEventListener("click", () => {
  shareInput.value = lastShareUrl;
  shareRow.hidden = !shareRow.hidden;
});

$("copyLinkBtn").addEventListener("click", async (e) => {
  shareInput.select();
  try {
    await navigator.clipboard.writeText(shareInput.value);
    e.target.textContent = "Скопировано";
  } catch {
    e.target.textContent = "Не удалось";
  }
  setTimeout(() => (e.target.textContent = "Копировать"), 1500);
});

$("downloadTxtBtn").addEventListener("click", () => lastCards && downloadCardsAsText(lastCards));

async function exportAs(fmt) {
  if (!lastCards) return;
  try {
    const res = await fetch(`/api/export/${fmt}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cards: lastCards }),
    });
    if (!res.ok) throw new Error((await res.json()).detail || "Ошибка экспорта");
    saveBlob(await res.blob(), `карточки.${fmt}`);
  } catch (err) {
    setStatus("Ошибка экспорта: " + err.message, true);
  }
}

$("downloadDocxBtn").addEventListener("click", () => exportAs("docx"));
$("downloadPdfBtn").addEventListener("click", () => exportAs("pdf"));

// ------------------------------------------------------------
// Генерация
// ------------------------------------------------------------
const submitText = $("submitText");
const SUBMIT_DEFAULT = submitText.textContent;

submitBtn.addEventListener("click", async () => {
  const formData = new FormData();

  if (activeTab === "file") {
    const file = fileInput.files[0];
    if (!file) return setStatus("Сначала выбери файл", true);
    if (file.size > maxFileMb * 1024 * 1024) {
      return setStatus(`Файл больше ${maxFileMb} МБ — выбери файл поменьше`, true);
    }
    formData.append("file", file);
  } else {
    const text = textInput.value.trim();
    if (!text) return setStatus("Вставь текст лекции", true);
    formData.append("text", text);
  }
  formData.append("level", level);

  submitBtn.disabled = true;
  submitBtn.classList.add("loading");
  submitText.textContent = "Собираю карточки…";
  setStatus("");

  try {
    const res = await fetch("/api/generate-cards", { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Неизвестная ошибка");

    lastCards = data.cards;
    lastShareUrl = location.origin + data.url;
    renderCards(data.cards, { clarify: true });
    $("results").hidden = false;
    shareRow.hidden = true;

    saveHistory([
      {
        url: data.url,
        title: data.title,
        count: data.cards.length,
        date: new Date().toLocaleDateString("ru-RU"),
      },
      ...loadHistory(),
    ]);

    const note = data.was_truncated ? " (лекция длиннее лимита — обработана только первая часть)" : "";
    setStatus(`Готово — ${data.cards.length} карточек${note}`);
    showQuota(data.quota_remaining, data.quota_limit);
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    submitBtn.disabled = false;
    submitBtn.classList.remove("loading");
    submitText.textContent = SUBMIT_DEFAULT;
  }
});
