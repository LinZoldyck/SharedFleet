// SharedFleet — petit script commun : lignes cliquables, info-bulles des graphiques, aperçu des photos.
document.addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-href]");
  if (row && !e.target.closest("a, button, input, select, form")) location.href = row.dataset.href;
});

// Info-bulle : data-tip contient du texte brut, une ligne par entrée (1re ligne en gras ;
// une ligne « #rrggbb|texte » affiche une pastille de couleur). Jamais interprété comme du HTML.
const tip = document.getElementById("tip");
if (tip) {
  const show = (el, x, y) => {
    tip.replaceChildren();
    el.dataset.tip.split("\n").forEach((line, i) => {
      const row = document.createElement(i === 0 ? "b" : "div");
      const m = line.match(/^(#[0-9a-f]{6})\|(.*)$/i);
      if (m) { const dot = document.createElement("i"); dot.style.background = m[1]; row.appendChild(dot); line = m[2]; }
      row.appendChild(document.createTextNode(line));
      tip.appendChild(row);
    });
    tip.style.display = "block";
    const w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.max(8, Math.min(x + 14, innerWidth - w - 8)) + "px";
    tip.style.top = Math.max(8, y - h - 12) + "px";
  };
  document.addEventListener("pointermove", (e) => {
    const el = e.target.closest("[data-tip]");
    if (el) show(el, e.clientX, e.clientY); else tip.style.display = "none";
  });
  document.addEventListener("focusin", (e) => {
    const el = e.target.closest("[data-tip]");
    if (el) { const r = el.getBoundingClientRect(); show(el, r.left + r.width / 2, r.top); }
  });
  document.addEventListener("focusout", () => { tip.style.display = "none"; });
}

// Aperçu des photos choisies + compteur.
document.querySelectorAll("input[type=file][data-preview]").forEach((input) => {
  input.addEventListener("change", () => {
    const box = document.getElementById(input.dataset.preview);
    box.innerHTML = "";
    [...input.files].forEach((f) => {
      const img = document.createElement("img");
      img.src = URL.createObjectURL(f);
      img.alt = f.name;
      box.appendChild(img);
    });
  });
});

// Valeur affichée à côté d'un curseur (niveau de carburant).
document.querySelectorAll("input[type=range][data-out]").forEach((input) => {
  const out = document.getElementById(input.dataset.out);
  const sync = () => { out.textContent = input.value + " %"; };
  input.addEventListener("input", sync); sync();
});

// Confirmation avant une action irréversible.
document.querySelectorAll("form[data-confirm]").forEach((form) => {
  form.addEventListener("submit", (e) => { if (!confirm(form.dataset.confirm)) e.preventDefault(); });
});

// Copie dans le presse-papiers.
document.querySelectorAll("[data-copy]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    await navigator.clipboard.writeText(btn.dataset.copy);
    const old = btn.textContent; btn.textContent = "Copié ✓"; setTimeout(() => { btn.textContent = old; }, 1500);
  });
});

// Attente d'une validation externe (connexion Microsoft) : interroge le serveur jusqu'au résultat.
document.querySelectorAll("[data-poll]").forEach((box) => {
  const status = document.getElementById("poll-status");
  const every = Math.max(3, Number(box.dataset.pollEvery) || 5) * 1000;
  const tick = async () => {
    try {
      const r = await fetch(box.dataset.poll, { method: "POST", headers: { "Content-Type": "application/json" } });
      const data = await r.json();
      if (data.status === "ok") { location.href = box.dataset.pollDone; return; }
      if (data.status === "error") { status.textContent = data.message; status.style.color = "var(--red)"; return; }
    } catch (e) { /* réseau momentanément indisponible : on réessaie */ }
    setTimeout(tick, every);
  };
  setTimeout(tick, every);
});
