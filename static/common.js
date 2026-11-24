"use strict";

// Общий код главной страницы и страницы расшаренного набора.

const $ = (id) => document.getElementById(id);

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);

const cardToText = (c) => `${c.title || ""}\n\nВопрос: ${c.question || ""}\nОтвет: ${c.answer || ""}\n`;

function saveBlob(blob, filename) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

function downloadCardsAsText(cards) {
  const text = cards.map(cardToText).join("\n---\n\n");
  saveBlob(new Blob([text], { type: "text/plain;charset=utf-8" }), "карточки.txt");
}

function setStatus(msg, isError = false) {
  const el = $("status");
  el.textContent = msg;
  el.classList.toggle("error", isError);
}

// ------------------------------------------------------------
// Карточки. Весь текст экранируется: он приходит от модели и из файлов.
// ------------------------------------------------------------
const CLARIFY_HTML = `
  <div class="clarify">
    <div class="clarify-log"></div>
    <form>
      <input type="text" maxlength="500" placeholder="Задать уточняющий вопрос…" aria-label="Уточняющий вопрос" />
      <button type="submit">Спросить</button>
    </form>
  </div>`;

function buildCard(card, withClarify) {
  const el = document.createElement("article");
  el.className = "card";
  const tags = (card.tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join("");

  el.innerHTML = `
    ${tags ? `<div class="tags">${tags}</div>` : ""}
    <h3>${esc(card.title)}</h3>
    <p class="q">${esc(card.question)}</p>
    <div class="a" hidden>
      <p>${esc(card.answer)}</p>
      ${withClarify ? CLARIFY_HTML : ""}
    </div>
    <div class="card-foot">
      <button type="button" class="link reveal" aria-expanded="false">Показать ответ</button>
      <button type="button" class="link copy">Копировать</button>
    </div>`;

  const answer = el.querySelector(".a");
  const reveal = el.querySelector(".reveal");
  const copy = el.querySelector(".copy");

  // Клик по карточке открывает/скрывает ответ; сам ответ и кнопка копирования — не в счёт.
  el.addEventListener("click", (e) => {
    if (e.target.closest(".a, .copy")) return;
    const open = answer.hidden;
    answer.hidden = !open;
    reveal.textContent = open ? "Скрыть ответ" : "Показать ответ";
    reveal.setAttribute("aria-expanded", String(open));
  });

  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(cardToText(card));
      copy.textContent = "Скопировано";
    } catch {
      copy.textContent = "Не удалось";
    }
    setTimeout(() => (copy.textContent = "Копировать"), 1500);
  });

  if (withClarify) attachClarify(el, card);
  return el;
}

function attachClarify(el, card) {
  const form = el.querySelector(".clarify form");
  const input = form.querySelector("input");
  const button = form.querySelector("button");
  const log = el.querySelector(".clarify-log");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const question = input.value.trim();
    if (!question) return;

    button.disabled = true;
    button.textContent = "…";
    const entry = document.createElement("div");
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
      entry.innerHTML = `<div class="cq">${esc(question)}</div><div class="ca">${esc(data.answer)}</div>`;
      input.value = "";
    } catch (err) {
      entry.innerHTML = `<div class="ca">Ошибка: ${esc(err.message)}</div>`;
    }
    log.append(entry);
    button.disabled = false;
    button.textContent = "Спросить";
  });
}

function renderCards(cards, { clarify = false } = {}) {
  $("cards").replaceChildren(...(cards || []).map((c) => buildCard(c, clarify)));
  $("emptyHint").hidden = Boolean(cards && cards.length);
}
