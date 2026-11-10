// Remplissage d'un formulaire à partir des données d'une contravention.
// fillPage() est injectée telle quelle dans l'onglet actif : elle doit rester autonome (aucune variable externe).
// Elle ne clique sur aucun bouton, ne touche ni aux captchas ni aux cases à cocher : la validation reste humaine.
function fillPage(values) {
  const norm = (s) => (s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/[_\-.:*]+/g, " ").replace(/\s+/g, " ").trim();

  // Texte décrivant un champ : libellé associé, aria-label, placeholder, name, id.
  const describe = (el) => {
    const parts = [];
    if (el.id) document.querySelectorAll(`label[for="${CSS.escape(el.id)}"]`).forEach((l) => parts.push(l.textContent));
    const wrap = el.closest("label");
    if (wrap) parts.push(wrap.textContent);
    (el.getAttribute("aria-labelledby") || "").split(" ").filter(Boolean).forEach((id) => {
      const ref = document.getElementById(id);
      if (ref) parts.push(ref.textContent);
    });
    parts.push(el.getAttribute("aria-label"), el.placeholder, el.name, el.id, el.getAttribute("formcontrolname"));
    return norm(parts.filter(Boolean).join(" "));
  };

  // Règles évaluées dans l'ordre : la première qui correspond donne la clé de la valeur à saisir.
  const rules = [
    [/captcha|code de securite|recopi/, null],
    [/siret|siren/, "siret"],
    [/raison sociale|denomination|nom de la societe|personne morale/, "company_name"],
    [/(numero|n°|no|ref).*(avis|contravention|paiement|fps)|avis de (contravention|paiement)|numero d avis/, "notice_number"],
    [/immatric|plaque/, "plate"],
    [/date.*(infraction|contravention|avis|constat)/, "offense_date"],
    [/permis.*(date|delivr|obten)|(date|delivr|obten).*permis/, "license_date"],
    [/permis.*(pays|lieu|prefecture|autorite)/, "license_country"],
    [/permis/, "license_number"],
    [/nom de naissance|nom d usage|nom de famille|nom patronymique/, "last_name"],
    [/naissance.*(lieu|ville|commune|pays|departement)|(lieu|ville|commune|pays|departement).*naissance/, null],
    [/naissance.*jour|jour.*naissance/, "birth_day"],
    [/naissance.*mois|mois.*naissance/, "birth_month"],
    [/naissance.*annee|annee.*naissance/, "birth_year"],
    [/date.*naissance|naissance.*date|\bnee? le\b/, "birth_date"],
    [/mail|courriel/, "email"],
    [/telephone|portable|mobile|\btel\b/, "phone"],
    [/code postal|\bcp\b|postal/, "postal_code"],
    [/ville|commune|localite/, "city"],
    [/pays/, "country"],
    [/adresse|voie|rue|complement/, "address"],
    [/prenom/, "first_name"],
    [/\bnom\b/, "last_name"],
  ];

  const setValue = (el, value) => {
    if (el.tagName === "SELECT") {
      const wanted = norm(value);
      const option = [...el.options].find((o) => norm(o.textContent) === wanted || norm(o.value) === wanted)
        || [...el.options].find((o) => wanted && norm(o.textContent).includes(wanted));
      if (!option) return false;
      el.value = option.value;
    } else {
      if (el.type === "date" && /^\d{2}\/\d{2}\/\d{4}$/.test(value)) value = value.split("/").reverse().join("-");
      // Passe par le setter natif pour que React / Angular / Vue voient le changement
      const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
    }
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("blur", { bubbles: true }));
    return true;
  };

  let filled = 0;
  const skipTypes = ["hidden", "submit", "button", "checkbox", "radio", "file", "password", "image", "reset"];
  document.querySelectorAll("input, select, textarea").forEach((el) => {
    if (skipTypes.includes(el.type) || el.disabled || el.readOnly || !el.offsetParent) return;
    if (el.tagName !== "SELECT" && el.value) return; // ne jamais écraser une saisie existante
    const text = describe(el);
    const rule = rules.find(([re]) => re.test(text));
    const value = rule && rule[1] ? values[rule[1]] : "";
    if (value && setValue(el, String(value))) {
      el.style.outline = "2px solid #4f46e5";
      el.style.outlineOffset = "1px";
      filled += 1;
    }
  });
  return filled;
}

// Aplatis les données d'une contravention en « clé → valeur à saisir ».
function fineValues(fine) {
  const d = fine.driver || {};
  const [day, month, year] = (d.birth_date || "").split("/");
  return {
    notice_number: fine.notice_number, plate: fine.plate, offense_date: fine.offense_date,
    company_name: d.is_company ? d.company_name : "", siret: d.is_company ? (d.siret || "").replace(/\s/g, "") : "",
    first_name: d.first_name, last_name: d.last_name, birth_date: d.birth_date,
    birth_day: day || "", birth_month: month || "", birth_year: year || "",
    address: d.address, postal_code: d.postal_code, city: d.city, country: d.country,
    email: d.email, phone: d.phone, license_number: d.license_number, license_date: d.license_date,
    license_country: d.license_country,
  };
}

if (typeof module !== "undefined") module.exports = { fillPage, fineValues };
