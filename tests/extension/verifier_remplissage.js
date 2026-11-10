// Vérifie fillPage() dans un vrai navigateur :  node tests/extension/verifier_remplissage.js
const path = require("path");
const assert = require("assert");
const { chromium } = require("playwright");
const { fillPage, fineValues } = require("../../extension/fill.js");

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto("file://" + path.join(__dirname, "formulaire_test.html"));
  let submitted = false;
  await page.exposeFunction("onSubmit", () => { submitted = true; });
  await page.evaluate(() => document.querySelector("form").addEventListener("submit", (e) => { e.preventDefault(); window.onSubmit(); }));
  const fine = { notice_number: "4012345678", plate: "AB-123-CD", offense_date: "03/11/2026", driver: {
    is_company: false, first_name: "Léa", last_name: "Martin", birth_date: "12/04/1990", address: "3 rue de la Paix",
    postal_code: "69002", city: "Lyon", country: "France", email: "lea@example.org", phone: "", license_number: "09AB12345",
    license_date: "01/06/2009", license_country: "France" } };
  const filled = await page.evaluate(fillPage, fineValues(fine));
  const v = (sel) => page.inputValue(sel);
  assert.strictEqual(await v("#a1"), "4012345678");
  assert.strictEqual(await v("#a2"), "AB-123-CD");
  assert.strictEqual(await v("#a3"), "2026-11-03");            // champ date natif : format ISO
  assert.strictEqual(await v("[name=nomNaissance]"), "Martin");
  assert.strictEqual(await v("[name=prenom]"), "Léa");
  assert.strictEqual(await v("#b1"), "12/04/1990");
  assert.strictEqual(await v("#b0"), "");                      // lieu de naissance : inconnu, laissé vide
  assert.strictEqual(await v("[name=voie]"), "3 rue de la Paix");
  assert.strictEqual(await v("[name=cp]"), "69002");
  assert.strictEqual(await v("[name=commune]"), "Lyon");
  assert.strictEqual(await v("#p"), "FR");                     // liste déroulante choisie par son libellé
  assert.strictEqual(await v("#c1"), "09AB12345");
  assert.strictEqual(await v("#c2"), "01/06/2009");
  assert.strictEqual(await v("#e"), "lea@example.org");
  assert.strictEqual(await v("#d"), "NE PAS ÉCRASER");         // saisie existante respectée
  assert.strictEqual(await v("#k"), "");                       // captcha jamais touché
  assert.strictEqual(await page.isChecked("[name=certifie]"), false);
  assert.strictEqual(submitted, false);                        // aucune validation automatique
  assert.strictEqual(filled, 13);
  console.log(`OK — ${filled} champs remplis, captcha / case / validation laissés à l'utilisateur.`);
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
