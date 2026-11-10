const $ = (id) => document.getElementById(id);
const LABELS = {
  notice_number: "N° d'avis", plate: "Immatriculation", offense_date: "Date de l'infraction", company_name: "Raison sociale",
  siret: "SIRET", last_name: "Nom", first_name: "Prénom", birth_date: "Date de naissance", address: "Adresse",
  postal_code: "Code postal", city: "Ville", country: "Pays", email: "E-mail", phone: "Téléphone",
  license_number: "N° de permis", license_date: "Permis délivré le", license_country: "Pays du permis",
};
let config = { server: "", token: "" };
let current = null;

function status(text, error = false) {
  $("status").textContent = text;
  $("status").className = error ? "error" : "";
}

async function api(path, method = "GET") {
  const response = await fetch(config.server.replace(/\/+$/, "") + path, { method, headers: { Authorization: "Bearer " + config.token } });
  if (!response.ok) throw new Error(response.status === 401 ? "Jeton refusé : régénérez-le dans les réglages." : "Erreur " + response.status);
  return response.json();
}

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  children.forEach((c) => node.append(c));
  return node;
}

async function loadList() {
  $("detail").hidden = true;
  $("list").replaceChildren();
  if (!config.server || !config.token) {
    $("settings").hidden = false;
    return status("Renseignez l'adresse de SharedFleet et votre jeton.");
  }
  status("Chargement…");
  try {
    const data = await api("/api/ext/fines");
    status(data.fines.length ? `${data.fines.length} contravention(s) à traiter — ${data.org}` : "Aucune contravention à traiter. 🎉");
    data.fines.forEach((fine) => {
      const who = fine.driver ? (fine.driver.is_company ? fine.driver.company_name : `${fine.driver.first_name} ${fine.driver.last_name}`) : "conducteur inconnu";
      const button = el("button", { className: "fine", type: "button" },
        el("b", {}, fine.notice_number || "N° illisible", el("span", { className: "tag " + fine.kind, textContent: fine.kind === "fps" ? "FPS" : "Amende" })),
        el("small", { textContent: `${fine.plate} · ${fine.offense_at} · ${who}` }));
      button.addEventListener("click", () => showDetail(fine));
      $("list").append(button);
    });
  } catch (error) {
    status(error.message === "Failed to fetch" ? "SharedFleet est injoignable à cette adresse." : error.message, true);
  }
}

function showDetail(fine) {
  current = fine;
  $("list").replaceChildren();
  $("detail").hidden = false;
  $("title").textContent = (fine.kind === "fps" ? "FPS " : "Avis ") + (fine.notice_number || "");
  status(fine.kind === "fps"
    ? "Ouvrez le site de paiement du FPS : l'extension saisit le n° d'avis et la plaque."
    : "Ouvrez la désignation sur antai.gouv.fr, puis remplissez chaque étape.");
  const values = fineValues(fine);
  $("fields").replaceChildren(...Object.entries(LABELS).filter(([key]) => values[key]).map(([key, label]) => {
    const row = el("div", { className: "row", title: "Cliquer pour copier" }, el("span", { textContent: label }), el("span", { textContent: values[key] }));
    row.addEventListener("click", async () => { await navigator.clipboard.writeText(values[key]); status(`${label} copié.`); });
    return row;
  }));
}

$("fill").addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  try {
    const results = await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, func: fillPage, args: [fineValues(current)] });
    const count = results.reduce((sum, r) => sum + (r.result || 0), 0);
    status(count ? `${count} champ(s) rempli(s) et entouré(s) en violet. Vérifiez, puis validez vous-même.`
      : "Aucun champ reconnu sur cette page. Cliquez sur une valeur ci-dessous pour la copier.", !count);
  } catch (error) {
    status("Impossible d'agir sur cet onglet (page interne du navigateur ?).", true);
  }
});

$("done").addEventListener("click", async () => {
  try { await api(`/api/ext/fines/${current.id}/done`, "POST"); await loadList(); } catch (error) { status(error.message, true); }
});
$("back").addEventListener("click", loadList);
$("openSettings").addEventListener("click", () => { $("settings").hidden = !$("settings").hidden; });
$("save").addEventListener("click", async () => {
  config = { server: $("server").value.trim(), token: $("token").value.trim() };
  await chrome.storage.local.set(config);
  $("settings").hidden = true;
  loadList();
});

chrome.storage.local.get(["server", "token"]).then((stored) => {
  config = { server: stored.server || "", token: stored.token || "" };
  $("server").value = config.server;
  $("token").value = config.token;
  loadList();
});
