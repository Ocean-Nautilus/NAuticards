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
      <div class="tag-row">${tags}</div>
      <h3>${card.title || ""}</h3>
      <div class="q">${card.question || ""}</div>
      <div class="a">${card.answer || ""}</div>
    `;

    // Квиз-режим: клик в любом месте карточки раскрывает/скрывает ответ
    const answerEl = el.querySelector(".a");
    el.addEventListener("click", () => {
      answerEl.classList.toggle("revealed");
    });

    cardsEl.appendChild(el);
  });
}

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
