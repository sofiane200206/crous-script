# Surveillance logements CROUS — Rouen

Surveille [trouverunlogement.lescrous.fr](https://trouverunlogement.lescrous.fr)
et envoie une notification push (via [ntfy.sh](https://ntfy.sh)) dès qu'un
nouveau logement apparaît dans la zone de Rouen pour l'année 2026-2027.

Deux modes d'exécution :

- **Local (Windows)** : `python crous_watch.py` boucle toutes les 5 minutes
  (config dans `.env`, voir `.env.example`). Lancement auto possible via
  `lancer_surveillance.bat` + Planificateur de tâches.
- **GitHub Actions (recommandé)** : le workflow `.github/workflows/surveillance.yml`
  exécute un cycle toutes les ~5 minutes, 24h/24, PC éteint. C'est le mode
  décrit ci-dessous.

## Installation sur GitHub Actions

1. **Créer un compte GitHub** si besoin, puis un **dépôt public** (par ex.
   `crous-rouen`). Public = minutes d'Actions illimitées et gratuites ;
   en privé, le quota gratuit (2000 min/mois) serait dépassé à ce rythme.
   Rien de sensible n'est publié : le topic ntfy reste dans les secrets.

2. **Pousser ces fichiers** dans le dépôt :
   `crous_watch.py`, `.github/workflows/surveillance.yml`, `.gitignore`,
   `README.md`, `.env.example` (PAS le `.env` : il est ignoré par git).

3. **Ajouter le secret** : sur GitHub → *Settings* → *Secrets and variables*
   → *Actions* → *New repository secret* :
   - Nom : `NTFY_TOPIC`
   - Valeur : ton topic ntfy (celui du `.env` local)

4. **Vérifier** : onglet *Actions* → workflow « Surveillance CROUS » →
   *Run workflow* pour lancer un premier cycle à la main. Le premier run
   enregistre l'existant sans notifier (juste une notif de confirmation)
   et committe `logements_vus.json` — c'est ainsi que l'état est conservé
   entre deux cycles.

## Points d'attention

- **Délai réel** : GitHub exécute les crons « au mieux » ; un cycle toutes
  les 5 min planifiées prend souvent 5 à 15 min réelles. Suffisant en
  pratique, mais pas garanti à la minute près.
- **Repos inactifs** : GitHub désactive les workflows planifiés après
  60 jours sans activité sur le dépôt (un email prévient ; il suffit de
  cliquer « Enable » pour réactiver). Les commits d'état comptent comme
  de l'activité.
- **Changement de phase** : l'ID de la phase en cours (`SEARCH_ID`, 47 =
  « Phase complémentaire 2026-2027 », jusqu'au 02/11/2026) est défini dans
  le workflow. La liste à jour des phases est publique :
  <https://trouverunlogement.lescrous.fr/api/fr/tools>. En cas d'ID périmé
  (erreurs 4xx répétées), une notification « vérifie l'ID de l'endpoint »
  est envoyée automatiquement.
- **Ne pas faire tourner les deux modes en même temps** (local + Actions)
  avec le même topic, sinon notifications en double.
