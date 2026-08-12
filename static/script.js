// ============================================================
// СБРОС ФАЙЛА ПРИ ЗАГРУЗКЕ СТРАНИЦЫ
// ============================================================
// Некоторые браузеры (особенно Firefox) при обновлении страницы (F5)
// восстанавливают старое состояние формы, включая выбранный файл — но
// наша кастомная надпись в зоне загрузки на это не реагирует, и
// получается рассинхрон: файл незаметно остаётся выбранным, хотя
// визуально это не видно. Чтобы не было "невидимого" старого файла —
// принудительно сбрасываем инпут при каждой загрузке страницы.
window.addEventListener("pageshow", () => {
  const fileInputEl = document.getElementById("fileInput");
  const fileDropEl = document.getElementById("fileDrop");
  if (fileInputEl) fileInputEl.value = "";
  if (fileDropEl) {
    fileDropEl.innerHTML = `
      <span class="drop-icon">📎</span>
      <span>Перетащи файл сюда или нажми, чтобы выбрать</span>
      <span style="font-size:11px; opacity:.7">.txt, .pdf, .docx, .pptx</span>
    `;
    fileDropEl.classList.remove("has-file");
  }
});

// ============================================================
// ЛИМИТЫ — подтягиваем с бэкенда, чтобы не дублировать числа
// ============================================================
let MAX_INPUT_CHARS = 15000;
let MAX_FILE_SIZE_MB = 10;

fetch("/api/limits")
  .then(res => res.json())
  .then(data => {
    MAX_INPUT_CHARS = data.max_input_chars;
    MAX_FILE_SIZE_MB = data.max_file_size_mb;
    document.getElementById("limitHint").textContent =
      `До ${MAX_INPUT_CHARS.toLocaleString("ru-RU")} символов текста или файл до ${MAX_FILE_SIZE_MB} МБ`;
    updateCharCounter();
  })
  .catch(() => { /* останется запасное значение */ });

// ============================================================
// ВКЛАДКИ Файл / Текст
// ============================================================
const tabButtons = document.querySelectorAll(".tab-btn");
const tabContents = document.querySelectorAll(".tab-content");
tabButtons.forEach(btn => {
  btn.addEventListener("click", () => {
    tabButtons.forEach(b => b.classList.remove("active"));
    tabContents.forEach(c => c.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
  });
});

// ============================================================
// ПЕРЕКЛЮЧАТЕЛЬ УРОВНЯ ПОДРОБНОСТИ (кратко / подробно)
// ============================================================
let selectedLevel = "detailed";
const levelButtons = document.querySelectorAll(".level-btn");
levelButtons.forEach(btn => {
  btn.addEventListener("click", () => {
    levelButtons.forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    selectedLevel = btn.dataset.level;
  });
});

// ============================================================
// ВЫБОР ФАЙЛА: клик и drag & drop
// ============================================================
const fileInput = document.getElementById("fileInput");
const fileDrop = document.getElementById("fileDrop");

function showSelectedFile(file){
  fileDrop.innerHTML = `<span>📄 ${file.name}</span>`;
  fileDrop.classList.add("has-file");
}

fileInput.addEventListener("change", () => {
  if (fileInput.files.length > 0) showSelectedFile(fileInput.files[0]);
});

["dragenter", "dragover"].forEach(evt => {
  fileDrop.addEventListener(evt, (e) => {
    e.preventDefault();
    fileDrop.classList.add("drag-over");
  });
});

["dragleave", "drop"].forEach(evt => {
  fileDrop.addEventListener(evt, (e) => {
    e.preventDefault();
    fileDrop.classList.remove("drag-over");
  });
});

fileDrop.addEventListener("drop", (e) => {
  const dropped = e.dataTransfer.files;
  if (dropped.length > 0) {
    fileInput.files = dropped;
    showSelectedFile(dropped[0]);
  }
});

// ============================================================
// СЧЁТЧИК СИМВОЛОВ В ТЕКСТОВОМ ПОЛЕ
// ============================================================
const textInput = document.getElementById("textInput");
const charCounter = document.getElementById("charCounter");

function updateCharCounter(){
  const len = textInput.value.length;
  const over = len > MAX_INPUT_CHARS;
  charCounter.textContent = over
    ? `${len.toLocaleString("ru-RU")} / ${MAX_INPUT_CHARS.toLocaleString("ru-RU")} — лишнее обрежется`
    : `${len.toLocaleString("ru-RU")} / ${MAX_INPUT_CHARS.toLocaleString("ru-RU")} символов`;
  charCounter.classList.toggle("over", over);
}

textInput.addEventListener("input", updateCharCounter);
updateCharCounter();

// ============================================================
// РЕНДЕР КАРТОЧЕК (квиз-режим: ответ скрыт до клика на вопрос)
// ============================================================
const cardsEl = document.getElementById("cards");
const emptyHint = document.getElementById("emptyHint");

function cardToText(card){
  return `${card.title || ""}\n\nВопрос: ${card.question || ""}\nОтвет: ${card.answer || ""}\n`;
}

function downloadCardsAsText(cards, filename = "карточки.txt"){
  const content = cards.map(cardToText).join("\n---\n\n");
  const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function renderCards(cards){
  cardsEl.innerHTML = "";
  if (!cards || cards.length === 0){
    emptyHint.style.display = "block";
    return;
  }
  emptyHint.style.display = "none";

  cards.forEach((card) => {
    const el = document.createElement("div");
    el.className = "card";
    const tags = (card.tags || []).map(t => `<span class="tag">${t}</span>`).join("");

    el.innerHTML = `
      <button class="copy-btn" title="Скопировать карточку">📋</button>
      <div class="tag-row">${tags}</div>
      <h3>${card.title || ""}</h3>
      <div class="q">${card.question || ""}</div>
      <div class="a">
        ${card.answer || ""}
        <div class="clarify-box">
          <div class="clarify-history"></div>
          <div class="clarify-input-row">
            <input type="text" placeholder="Спросить уточнение у ИИ..." />
            <button>Спросить</button>
          </div>
        </div>
      </div>
    `;

    // Квиз-режим: клик в любом месте карточки раскрывает/скрывает ответ
    const answerEl = el.querySelector(".a");
    el.addEventListener("click", () => {
      answerEl.classList.toggle("revealed");
    });

    // Копирование — отдельная кнопка, клик по ней не должен раскрывать ответ
    const copyBtn = el.querySelector(".copy-btn");
    copyBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      navigator.clipboard.writeText(cardToText(card));
      copyBtn.textContent = "✅";
      copyBtn.classList.add("copied");
      setTimeout(() => {
        copyBtn.textContent = "📋";
        copyBtn.classList.remove("copied");
      }, 1200);
    });

    // Блок уточнений — клики внутри не должны закрывать карточку
    const clarifyBox = el.querySelector(".clarify-box");
    const clarifyHistory = el.querySelector(".clarify-history");
    const clarifyInput = el.querySelector(".clarify-input-row input");
    const clarifyBtn = el.querySelector(".clarify-input-row button");

    clarifyBox.addEventListener("click", (e) => e.stopPropagation());

    async function sendClarify(){
      const question = clarifyInput.value.trim();
      if (!question) return;

      clarifyBtn.disabled = true;
      clarifyBtn.textContent = "...";

      try {
        const res = await fetch("/api/clarify", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            card_question: card.question || "",
            card_answer: card.answer || "",
            user_question: question,
          }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Ошибка");

        const qaEl = document.createElement("div");
        qaEl.className = "clarify-qa";
        qaEl.innerHTML = `<div class="cq">Ты: ${question}</div><div class="ca">${data.answer}</div>`;
        clarifyHistory.appendChild(qaEl);
        clarifyInput.value = "";
      } catch (err) {
        const qaEl = document.createElement("div");
        qaEl.className = "clarify-qa";
        qaEl.innerHTML = `<div class="ca">Ошибка: ${err.message}</div>`;
        clarifyHistory.appendChild(qaEl);
      } finally {
        clarifyBtn.disabled = false;
        clarifyBtn.textContent = "Спросить";
      }
    }

    clarifyBtn.addEventListener("click", sendClarify);
    clarifyInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") sendClarify();
    });

    cardsEl.appendChild(el);
  });
}

// ============================================================
// КАРТА ЛЕКЦИИ (майндмэп через markmap)
// ============================================================
let currentMapSessionId = null;

const mapBtn = document.getElementById("mapBtn");
const mapModal = document.getElementById("mapModal");
const mapModalClose = document.getElementById("mapModalClose");
const mapLoading = document.getElementById("mapLoading");
const mapSvg = document.getElementById("mapSvg");

function closeMapModal(){
  mapModal.style.display = "none";
}

mapModalClose.addEventListener("click", closeMapModal);
mapModal.addEventListener("click", (e) => {
  if (e.target === mapModal) closeMapModal();
});

mapBtn.addEventListener("click", async () => {
  if (!currentMapSessionId) return;

  mapModal.style.display = "flex";
  mapLoading.style.display = "block";
  mapSvg.innerHTML = "";

  try {
    const res = await fetch("/api/generate-map", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ map_session_id: currentMapSessionId }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Не удалось построить карту");

    const { Transformer } = window.markmap;
    const { Markmap } = window.markmap;
    const transformer = new Transformer();
    const { root } = transformer.transform(data.markdown);

    mapLoading.style.display = "none";
    Markmap.create(mapSvg, undefined, root);
  } catch (err) {
    mapLoading.textContent = "Ошибка: " + err.message;
  }
});

// ============================================================
// КНОПКА "ПОДЕЛИТЬСЯ ССЫЛКОЙ"
// ============================================================
const shareBox = document.getElementById("shareBox");
const shareBtn = document.getElementById("shareBtn");
const shareLinkRow = document.getElementById("shareLinkRow");
const shareLinkInput = document.getElementById("shareLinkInput");
const copyLinkBtn = document.getElementById("copyLinkBtn");

let lastGeneratedCards = null;

shareBtn.addEventListener("click", async () => {
  if (!lastGeneratedCards) return;

  shareBtn.disabled = true;
  shareBtn.textContent = "Сохраняю...";

  try {
    const res = await fetch("/api/save-set", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cards: lastGeneratedCards, title: "Набор карточек" }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Не удалось сохранить");

    const fullUrl = window.location.origin + data.url;
    shareLinkInput.value = fullUrl;
    shareLinkRow.style.display = "flex";
    shareBtn.textContent = "🔗 Поделиться ссылкой";
  } catch (err) {
    setStatus("Не удалось создать ссылку: " + err.message, true);
    shareBtn.textContent = "🔗 Поделиться ссылкой";
  } finally {
    shareBtn.disabled = false;
  }
});

copyLinkBtn.addEventListener("click", () => {
  shareLinkInput.select();
  navigator.clipboard.writeText(shareLinkInput.value);
  copyLinkBtn.textContent = "Скопировано!";
  setTimeout(() => { copyLinkBtn.textContent = "Копировать"; }, 1500);
});

const downloadBtn = document.getElementById("downloadBtn");
downloadBtn.addEventListener("click", () => {
  if (!lastGeneratedCards) return;
  downloadCardsAsText(lastGeneratedCards);
});

// ============================================================
// ОТПРАВКА ЗАПРОСА НА ГЕНЕРАЦИЮ
// ============================================================
const statusEl = document.getElementById("status");
const submitBtn = document.getElementById("submitBtn");
const submitText = document.getElementById("submitText");

function setStatus(msg, isError=false){
  statusEl.textContent = msg;
  statusEl.classList.toggle("error", isError);
}

submitBtn.addEventListener("click", async () => {
  const activeTab = document.querySelector(".tab-btn.active").dataset.tab;
  const formData = new FormData();

  if (activeTab === "file"){
    if (!fileInput.files.length){
      setStatus("Сначала выбери файл", true);
      return;
    }
    const fileSizeMb = fileInput.files[0].size / (1024 * 1024);
    if (fileSizeMb > MAX_FILE_SIZE_MB){
      setStatus(`Файл больше ${MAX_FILE_SIZE_MB} МБ — выбери файл поменьше`, true);
      return;
    }
    formData.append("file", fileInput.files[0]);
  } else {
    const text = textInput.value.trim();
    if (!text){
      setStatus("Вставь текст лекции", true);
      return;
    }
    formData.append("text", text);
  }

  formData.append("level", selectedLevel);

  submitBtn.disabled = true;
  submitBtn.classList.add("loading");
  submitText.textContent = "ИИ раскладывает лекцию по карточкам...";
  setStatus("");

  try {
    const res = await fetch("/api/generate-cards", { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok){
      throw new Error(data.detail || "Неизвестная ошибка");
    }
    renderCards(data.cards);
    lastGeneratedCards = data.cards;
    currentMapSessionId = data.map_session_id;
    shareBox.style.display = "block";
    shareLinkRow.style.display = "none";
    const truncNote = data.was_truncated
      ? ` (лекция длиннее лимита — обработана только первая часть)`
      : "";
    setStatus(`Готово — ${data.cards.length} карточек${truncNote}`);
  } catch (err){
    setStatus("Ошибка: " + err.message, true);
  } finally {
    submitBtn.disabled = false;
    submitBtn.classList.remove("loading");
    submitText.textContent = "Превратить в карточки";
  }
});
