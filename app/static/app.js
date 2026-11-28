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

// --- Prise de vue guidée ---------------------------------------------------------------------------
// Les photos sont réduites dans le téléphone avant l'envoi (1600 px, JPEG) : une dizaine de photos de 3 à 5 Mo
// passeraient très lentement sur une connexion mobile. En cas d'échec (vieux navigateur), la photo d'origine est envoyée.
async function shrinkPhoto(file, maxSide = 1600) {
  if (!file || !file.type.startsWith("image/") || !window.createImageBitmap) return file;
  try {
    const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
    const scale = Math.min(1, maxSide / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((done) => canvas.toBlob(done, "image/jpeg", 0.85));
    if (!blob || blob.size >= file.size) return file;
    return new File([blob], file.name.replace(/\.[^.]+$/, "") + ".jpg", { type: "image/jpeg", lastModified: Date.now() });
  } catch (e) {
    return file;
  }
}

async function replaceFiles(input, files) {
  try {
    const box = new DataTransfer();
    files.forEach((f) => box.items.add(f));
    input.files = box.files;  // ne redéclenche pas « change »
  } catch (e) { /* DataTransfer indisponible : les fichiers d'origine partent */ }
}

function updatePhotoCount() {
  const badge = document.getElementById("photo-count");
  if (!badge) return;
  const required = [...document.querySelectorAll("input[data-slot][data-required]")];
  const done = required.filter((i) => i.files.length).length;
  badge.textContent = `${done}/${required.length} obligatoires`;
  badge.className = "badge " + (done === required.length ? "green" : "gray");
}

document.querySelectorAll("input[data-slot]").forEach((input) => {
  input.addEventListener("change", async () => {
    const card = input.closest("[data-slot-card]");
    const file = input.files[0];
    card.classList.remove("missing");
    if (!file) { card.classList.remove("done"); updatePhotoCount(); return; }
    card.classList.add("busy");
    card.querySelector(".slot-action").textContent = "Préparation…";
    const small = await shrinkPhoto(file);
    if (small !== file) await replaceFiles(input, [small]);
    const img = card.querySelector(".slot-photo");
    if (img.dataset.url) URL.revokeObjectURL(img.dataset.url);
    img.dataset.url = img.src = URL.createObjectURL(small);
    card.classList.remove("busy");
    card.classList.add("done");
    card.querySelector(".slot-action").textContent = "↺ Reprendre la photo";
    updatePhotoCount();
  });
});

document.querySelectorAll("input[type=file][data-compress]").forEach((input) => {
  input.addEventListener("change", async () => {
    const files = await Promise.all([...input.files].map((f) => shrinkPhoto(f)));
    await replaceFiles(input, files);
  });
});

document.querySelectorAll("form[data-guided]").forEach((form) => {
  form.addEventListener("submit", (e) => {
    const missing = [...form.querySelectorAll("input[data-slot][data-required]")].filter((i) => !i.files.length);
    const box = document.getElementById("photo-missing");
    if (missing.length) {
      e.preventDefault();
      missing.forEach((i) => i.closest("[data-slot-card]").classList.add("missing"));
      const names = missing.map((i) => i.closest("[data-slot-card]").querySelector(".slot-title").firstChild.textContent.trim());
      box.textContent = "Il manque " + (missing.length > 1 ? "des photos obligatoires : " : "une photo obligatoire : ") + names.join(", ") + ".";
      box.hidden = false;
      missing[0].closest("[data-slot-card]").scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    if (form.querySelector(".slot.busy")) { e.preventDefault(); return; }  // une photo est encore en préparation
    const button = form.querySelector("button.big");
    if (button) { button.disabled = true; button.textContent = "Envoi des photos… ne fermez pas la page"; }
  });
});
updatePhotoCount();
