# SharedFleet

Application de gestion pour loueurs de véhicules en autopartage : locations Getaround **et** locations
directes au même endroit, contrats automatiques, parcours client sans remise de clés, assistant de
messagerie, contraventions, facturation et tableau de bord. Conçue pour tourner sur un Raspberry Pi.

## Ce que fait la v1

| Besoin | Dans l'application |
|---|---|
| Comptes, rôles, authentification | Une société = un espace isolé. Rôles Administrateur / Gestionnaire / Lecture seule. |
| Contrat de location hors plateforme | PDF généré à la création de la location : logo en en-tête, conditions générales modifiables, photos **avant** et **après**. |
| Parcours client sans contact | Code à 6 chiffres envoyé par SMS 30 min avant le départ → code du Master Lock → état des lieux (photos, km, carburant) → même chemin au retour. |
| Assistant de messagerie Getaround | Claude répond aux questions simples et vous transmet tout ce qui relève d'une décision. Mode brouillon ou automatique. |
| Contraventions | Avis déposés dans un dossier (ou depuis le navigateur) → lecture → rapprochement avec la location Getaround ou directe et son conducteur. |
| Désignation du conducteur | Extension Chrome qui remplit les champs à chaque étape ; captcha et validation restent manuels. FPS traités à part (paiement + refacturation). |
| Factures | Pennylane (API v2) ou PDF local. Architecture à « fournisseurs » pour brancher d'autres outils. |
| Pilotage | CA par canal, coûts fixes et variables, marge, taux d'occupation, rentabilité par véhicule, planning 14 jours. |

## Installation sur le Raspberry Pi

Prérequis : Raspberry Pi OS **64 bits** et Docker (`curl -fsSL https://get.docker.com | sh`).

```bash
git clone https://github.com/LinZoldyck/SharedFleet.git && cd SharedFleet
```

```bash
cp .env.example .env
```

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Collez la valeur obtenue dans `SECRET_KEY` du fichier `.env`, réglez `BASE_URL` (adresse à laquelle vos
clients ouvriront l'application), puis :

```bash
docker compose up -d --build
```

Ouvrez `http://<adresse-du-raspberry>:8000` : la première visite crée le compte administrateur.
Passez ensuite `ALLOW_SIGNUP=false` dans `.env` si vous ne voulez pas d'autres sociétés.

Pour essayer avec des données fictives (3 véhicules, 12 mois d'activité) :

```bash
docker compose exec sharedfleet python -m app.seed
```

Identifiants de démonstration : `demo@sharedfleet.local` / `demonstration`.

Sans Docker : `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`, puis le service
systemd fourni dans `deploy/sharedfleet.service`.

### Accès depuis Internet (HTTPS)

Vos clients ouvrent la page `/acces` depuis leur téléphone, et Getaround n'envoie ses webhooks qu'en HTTPS.
Le plus simple sur un Raspberry est un tunnel (Cloudflare Tunnel ou Tailscale Funnel) ou un proxy Caddy
devant le port 8000. Mettez alors `COOKIE_SECURE=true`, `BASE_URL=https://…` et `FORWARDED_ALLOW_IPS`.

### Sauvegarde

Tout est dans `./data` (base SQLite, photos, logo, avis). `deploy/sauvegarde.sh <dossier>` en fait une
copie datée et cohérente ; planifiez-le avec cron.

## Mise en route (dans l'ordre)

1. **Véhicules** — nom, plaque, code du Master Lock, tarif journalier, coûts fixes mensuels.
2. **Réglages → Société** — identité, **logo**, conditions générales de votre contrat.
3. **Réglages → SMS** — Twilio ou une passerelle HTTP. En mode « test », le SMS est seulement écrit dans le journal.
4. **Réglages → Getaround** — jeton d'API propriétaire (à demander à `owner-api@getaround.com`), puis
   communiquez à Getaround l'URL de webhook affichée. Sans webhook, la relève se fait toutes les 5 minutes.
5. **Réglages → Assistant** — clé d'API Claude, mode (brouillon conseillé au début), informations que
   l'assistant a le droit de donner.
6. **Réglages → Facturation** — jeton Pennylane, ou factures PDF locales.
7. **Réglages → Extension Chrome** — générez votre jeton.

### Dossier des contraventions

Déposez les avis (PDF, JPG, PNG) dans `data/org_<n>/contraventions/a_traiter` — le chemin exact est affiché
dans la page Contraventions. Partagez ce dossier en réseau (Samba) pour y glisser vos scans. Il est relevé
toutes les 2 minutes ; les fichiers lus sont archivés dans `traitees/`.

Avec une clé Claude, les scans et photos sont lus. Sans clé, seuls les PDF contenant du texte le sont.

### Extension Chrome

1. Chrome → `chrome://extensions` → activer le **mode développeur** → **Charger l'extension non empaquetée** → dossier `extension/`.
2. Cliquer sur l'icône SharedFleet → **Connexion** → adresse de l'application + jeton.
3. Sur le site de désignation, ouvrir l'extension, choisir la contravention, **Remplir cette page** à chaque étape.

L'extension ne s'active que sur l'onglet où vous cliquez, n'écrase jamais un champ déjà rempli, ne coche
aucune case et ne valide rien.

## Comment c'est construit

```
Navigateur / téléphone client ──► FastAPI (app/main.py)
                                   ├─ routers/   pages (une par domaine : locations, flotte, messages, contraventions…)
                                   ├─ services/  règles métier et intégrations
                                   │    ├─ getaround.py   Owner API : locations, conducteurs, messages, indisponibilités, webhooks
                                   │    ├─ assistant.py   décision « répondre / transmettre » par Claude
                                   │    ├─ fines.py       lecture des avis + rapprochement plaque / date
                                   │    ├─ invoicing.py   fournisseurs de facturation (Pennylane, local)
                                   │    ├─ pdf.py         contrat et facture
                                   │    ├─ sms.py         fournisseurs SMS
                                   │    ├─ dashboard.py   indicateurs
                                   │    └─ scheduler.py   tâches de fond (SMS, synchro, relève du dossier)
                                   └─ SQLite + fichiers dans DATA_DIR
Extension Chrome ──► /api/ext/* (jeton Bearer)
Getaround ──► /webhooks/getaround/<clé> (signature HMAC vérifiée)
```

Chaque table porte un `org_id` : l'application est multi-société dès maintenant, ce qui prépare le SaaS.
Les jetons d'API sont chiffrés en base avec `SECRET_KEY`.

## Limites connues

- **Déverrouillage Getaround** : l'API propriétaire ne propose aucun appel de déverrouillage ni de contrat.
  Pour une location directe, le parcours passe donc par la boîte à clés ; l'application bloque en revanche
  automatiquement le créneau sur le calendrier Getaround.
- **Pennylane** : requêtes écrites d'après la documentation v2, vérifiées contre un serveur simulé, pas
  encore contre un compte réel. Les factures sont créées en brouillon par défaut.
- **Getaround** : intégration écrite d'après la spécification OpenAPI officielle et testée contre une API
  simulée ; à valider avec votre jeton.
- **Extension** : les règles de remplissage reposent sur les libellés des champs et sont testées sur un
  formulaire fictif (`tests/extension/`). À ajuster dans `extension/fill.js` au premier essai réel sur l'ANTAI.
- **Contrat** : les conditions générales fournies sont un texte type ; remplacez-les par les vôtres et
  faites-les relire. L'acceptation est électronique simple (nom, date, adresse IP), sans signature qualifiée.
- **FPS** : traités comme « à payer puis refacturer ». Vérifiez votre situation avec votre conseil.

## Vers le SaaS

Déjà en place : isolation par société, rôles, fournisseurs interchangeables (SMS, facturation), secrets
chiffrés. Restera à faire : PostgreSQL à la place de SQLite, stockage objet pour les photos, file de tâches
séparée, abonnement et paiement, réinitialisation de mot de passe par e-mail, autres banques / outils de
facturation, publication de l'extension sur le Chrome Web Store.

## Développement

```bash
.venv/bin/pip install -r requirements-dev.txt
```

```bash
.venv/bin/python -m pytest
```

```bash
node tests/extension/verifier_remplissage.js
```

```bash
DISABLE_SCHEDULER=true .venv/bin/uvicorn app.main:app --reload
```
