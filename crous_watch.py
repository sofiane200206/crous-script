# -*- coding: utf-8 -*-
"""
crous_watch.py — Surveillance des logements CROUS (trouverunlogement.lescrous.fr)
==================================================================================

Le site est une SPA React : on n'y fait pas de scraping HTML, on appelle
directement son API interne de recherche en POST.

------------------------------------------------------------------------------
COMMENT RÉCUPÉRER / VÉRIFIER LE PAYLOAD VIA DEVTOOLS (à refaire à chaque phase)
------------------------------------------------------------------------------
1. Ouvrir https://trouverunlogement.lescrous.fr et lancer une recherche
   sur "Rouen" (avec la carte cadrée sur la zone qui t'intéresse).
2. F12 -> onglet "Network" (Réseau) -> filtrer sur "Fetch/XHR".
3. Repérer la requête POST vers un chemin du type :  /api/fr/search/43
   -> le nombre à la fin (ici 43) est l'ID DE PHASE ("idTool"). Il change
      à chaque tour d'attribution (rentrée, phase complémentaire, etc.).
4. Clic droit sur la requête -> Copy -> "Copy as cURL" pour tout récupérer,
   ou onglet "Payload" pour voir le JSON envoyé. Les points importants :
     - "idTool"   : doit correspondre à l'ID dans l'URL
     - "location" : 2 coins de la bounding box [ {lon,lat} NO, {lon,lat} SE ]
5. Reporter l'ID dans SEARCH_ID ci-dessous (ou dans le .env), et si besoin
   ajuster la bounding box.

Si l'API se met à répondre 4xx en boucle, c'est presque toujours un ID de
phase périmé : le script t'envoie alors une notification pour te le dire.

------------------------------------------------------------------------------
DÉPENDANCES : Python 3.11+, `pip install requests` (seule dépendance externe).
------------------------------------------------------------------------------
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# =============================================================================
# CONFIGURATION
# Chaque valeur peut être surchargée par une variable d'environnement du même
# nom, ou par un fichier `.env` posé à côté du script (format KEY=VALUE).
# =============================================================================

# --- Chargement (optionnel) du fichier .env, sans dépendance externe --------
def _charger_dotenv() -> None:
    env_path = Path(__file__).with_name(".env")
    if not env_path.is_file():
        return
    for ligne in env_path.read_text(encoding="utf-8").splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#") or "=" not in ligne:
            continue
        cle, _, valeur = ligne.partition("=")
        # Les variables déjà définies dans l'environnement ont priorité.
        os.environ.setdefault(cle.strip(), valeur.strip())

_charger_dotenv()

def _env(nom: str, defaut: str) -> str:
    return os.environ.get(nom, defaut)

# --- API CROUS ---------------------------------------------------------------
# ID de la phase en cours (le nombre à la fin de /api/fr/search/XX).
# ASTUCE : plus simple que DevTools, l'endpoint public
#   https://trouverunlogement.lescrous.fr/api/fr/tools
# liste toutes les phases avec leur nom et leurs dates. Au 12/07/2026 :
#   47 = "Phase complémentaire 2026-2027"  (06/07/2026 -> 02/11/2026)
#   42 = "Fil de l'Eau 2025-26"            (année universitaire en cours)
SEARCH_ID = int(_env("SEARCH_ID", "47"))
API_URL = f"https://trouverunlogement.lescrous.fr/api/fr/search/{SEARCH_ID}"
FICHE_URL = "https://trouverunlogement.lescrous.fr/tools/{search_id}/accommodations/{item_id}"

# Bounding box autour de Rouen : coin Nord-Ouest puis coin Sud-Est.
# (centre de Rouen ~ lat 49.443, lon 1.099 ; la box couvre l'agglomération,
#  Mont-Saint-Aignan inclus). Élargir/réduire au besoin.
BBOX_LON_OUEST = float(_env("BBOX_LON_OUEST", "0.95"))
BBOX_LAT_NORD  = float(_env("BBOX_LAT_NORD",  "49.54"))
BBOX_LON_EST   = float(_env("BBOX_LON_EST",   "1.25"))
BBOX_LAT_SUD   = float(_env("BBOX_LAT_SUD",   "49.34"))

# Payload envoyé à l'API — calqué sur ce que la SPA envoie (Copy as cURL).
# pageSize élevé pour tout récupérer d'un coup sur une zone comme Rouen ;
# le script pagine quand même si jamais il y a plus de résultats.
def construire_payload(page: int = 1) -> dict:
    return {
        "idTool": SEARCH_ID,
        "need_aggregation": True,
        "page": page,
        "pageSize": 200,
        "sector": None,
        "occupationModes": [],
        "location": [
            {"lon": BBOX_LON_OUEST, "lat": BBOX_LAT_NORD},   # coin Nord-Ouest
            {"lon": BBOX_LON_EST,  "lat": BBOX_LAT_SUD},     # coin Sud-Est
        ],
        "residence": None,
        "precision": 7,
        "equipment": [],
        "price": {"min": 0, "max": 10000000},  # en centimes, très large
        "toolMechanism": "residual",
    }

# En-têtes proches d'un navigateur (le site n'exige pas de cookie/token).
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0.0.0 Safari/537.36",
    "Origin": "https://trouverunlogement.lescrous.fr",
    "Referer": "https://trouverunlogement.lescrous.fr/",
}

# --- Notifications ntfy.sh ---------------------------------------------------
# Choisir un topic difficile à deviner (il fait office de mot de passe),
# puis s'y abonner dans l'app ntfy (Android/iOS) ou sur https://ntfy.sh/<topic>
NTFY_TOPIC = _env("NTFY_TOPIC", "crous-rouen-CHANGE-MOI")
NTFY_URL = _env("NTFY_URL", "https://ntfy.sh")  # serveur ntfy (public par défaut)

# --- Comportement ------------------------------------------------------------
INTERVALLE_SECONDES = int(_env("INTERVALLE_SECONDES", "300"))  # 5 min par défaut
FICHIER_ETAT = Path(__file__).with_name(_env("FICHIER_ETAT", "logements_vus.json"))
TIMEOUT_HTTP = 30            # secondes, pour chaque appel réseau
SEUIL_ERREURS_4XX = 3        # nb d'erreurs 4xx consécutives avant l'alerte
                             # "ID de phase probablement périmé"

# RUN_ONCE=1 : effectue UN SEUL cycle puis se termine, au lieu de boucler.
# C'est le mode utilisé par GitHub Actions (le planificateur cron relance le
# script à intervalle régulier) ; le mode boucle reste le défaut en local.
RUN_ONCE = _env("RUN_ONCE", "0") == "1"

# =============================================================================
# UTILITAIRES
# =============================================================================

def log(message: str) -> None:
    """Affiche un message horodaté sur la console."""
    horodatage = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{horodatage}] {message}", flush=True)


def charger_etat() -> dict | None:
    """Charge le fichier d'état. Renvoie None si premier lancement."""
    try:
        if FICHIER_ETAT.is_file():
            return json.loads(FICHIER_ETAT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log(f"ATTENTION : fichier d'état illisible ({e}), il sera réinitialisé.")
    return None


def sauvegarder_etat(initialise: bool, ids_vus: set[int],
                     erreurs_4xx: int = 0, alerte_4xx_envoyee: bool = False) -> None:
    """
    Persiste l'état (écriture atomique via fichier temporaire).
    Le compteur d'erreurs 4xx est persisté lui aussi, pour que l'alerte
    "ID périmé" fonctionne même en mode RUN_ONCE (un processus par cycle).
    """
    try:
        contenu = json.dumps(
            {
                "search_id": SEARCH_ID,
                "initialise": initialise,
                "ids": sorted(ids_vus),
                "erreurs_4xx": erreurs_4xx,
                "alerte_4xx_envoyee": alerte_4xx_envoyee,
            },
            ensure_ascii=False, indent=2,
        )
        tmp = FICHIER_ETAT.with_suffix(".tmp")
        tmp.write_text(contenu, encoding="utf-8")
        tmp.replace(FICHIER_ETAT)
    except OSError as e:
        log(f"ERREUR : impossible d'écrire le fichier d'état : {e}")


def notifier(titre: str, message: str, lien: str | None = None,
             tags: list[str] | None = None, priorite: int = 3) -> None:
    """
    Envoie une notification push via ntfy.sh.
    On publie en JSON (et non via les en-têtes HTTP) pour que les accents
    passent proprement en UTF-8. Ne lève jamais d'exception.
    """
    corps = {
        "topic": NTFY_TOPIC,
        "title": titre,
        "message": message,
        "priority": priorite,
        "tags": tags or [],
    }
    if lien:
        corps["click"] = lien  # rend la notification cliquable
    try:
        r = requests.post(NTFY_URL, json=corps, timeout=TIMEOUT_HTTP)
        if r.status_code >= 400:
            log(f"ERREUR ntfy : HTTP {r.status_code} — {r.text[:200]}")
    except requests.RequestException as e:
        log(f"ERREUR ntfy : {e}")


# =============================================================================
# APPEL DE L'API ET EXTRACTION DES DONNÉES
# =============================================================================

class ErreurHttp4xx(Exception):
    """Levée quand l'API répond 4xx (ID de phase probablement périmé)."""


def recuperer_logements() -> list[dict]:
    """
    Interroge l'API (avec pagination) et renvoie la liste brute des items.
    Lève ErreurHttp4xx sur un statut 4xx, requests.RequestException sur un
    problème réseau, ValueError si la structure JSON est inattendue.
    """
    items: list[dict] = []
    page = 1
    while True:
        r = requests.post(API_URL, json=construire_payload(page),
                          headers=HEADERS, timeout=TIMEOUT_HTTP)
        if 400 <= r.status_code < 500:
            raise ErreurHttp4xx(f"HTTP {r.status_code} sur {API_URL}")
        r.raise_for_status()

        data = r.json()
        resultats = data.get("results")
        if not isinstance(resultats, dict) or "items" not in resultats:
            raise ValueError(f"Structure JSON inattendue : clés = {list(data)}")

        page_items = resultats.get("items") or []
        items.extend(page_items)

        # "total" peut être un entier ou un objet {"value": N} selon les versions.
        total = resultats.get("total", 0)
        if isinstance(total, dict):
            total = total.get("value", 0)

        if not page_items or len(items) >= int(total) or page > 20:
            return items
        page += 1


def extraire_infos(item: dict) -> tuple[int | None, str, str, str]:
    """
    Extrait (id, nom, résidence, loyer) d'un item, en restant tolérant aux
    variations de structure : une clé absente donne un champ '?' plutôt
    qu'un crash.
    """
    item_id = item.get("id")

    nom = item.get("label") or "Logement sans nom"

    residence = "?"
    res = item.get("residence")
    if isinstance(res, dict):
        residence = res.get("label") or "?"

    # Le loyer se trouve selon les cas dans occupationModes[].rent {min,max}
    # (montants en CENTIMES, ex. 40359 = 403,59 €) ou dans rentRange (en euros).
    def _euros(centimes: float) -> str:
        v = centimes / 100
        return f"{v:.2f}".rstrip("0").rstrip(".").replace(".", ",")

    loyer = "?"
    try:
        modes = item.get("occupationModes") or []
        if modes and isinstance(modes[0], dict):
            rent = modes[0].get("rent") or {}
            rmin, rmax = rent.get("min"), rent.get("max")
            if rmin is not None:
                rmax = rmax if rmax is not None else rmin
                loyer = (f"{_euros(rmin)} €" if rmin == rmax
                         else f"{_euros(rmin)}–{_euros(rmax)} €")
        if loyer == "?" and item.get("rentRange"):
            rr = item["rentRange"]
            loyer = (f"{rr[0]:.0f} €" if rr[0] == rr[-1]
                     else f"{rr[0]:.0f}–{rr[-1]:.0f} €")
    except (TypeError, ValueError, IndexError, KeyError):
        pass  # on garde '?' : mieux vaut une notif incomplète que pas de notif

    return item_id, str(nom), str(residence), loyer


# =============================================================================
# BOUCLE PRINCIPALE
# =============================================================================

def main() -> None:
    log("=== Surveillance logements CROUS — zone Rouen ===")
    log(f"API           : {API_URL}")
    log(f"Topic ntfy    : {NTFY_TOPIC}")
    log(f"Mode          : {'un seul cycle (RUN_ONCE)' if RUN_ONCE else f'boucle toutes les {INTERVALLE_SECONDES} s'}")
    log(f"Fichier d'état: {FICHIER_ETAT}")

    if "CHANGE-MOI" in NTFY_TOPIC:
        log("ATTENTION : topic ntfy par défaut, pense à le personnaliser "
            "(NTFY_TOPIC dans le .env) !")

    etat = charger_etat() or {}
    # Si l'ID de phase a changé depuis la dernière exécution, on repart de
    # zéro (les IDs d'items d'une ancienne phase ne sont plus comparables).
    if etat and etat.get("search_id") != SEARCH_ID:
        log("ID de phase différent de celui du fichier d'état : réinitialisation.")
        etat = {}

    # "initialise" absent (ancien format de fichier) = considéré initialisé.
    initialise = bool(etat) and bool(etat.get("initialise", True))
    ids_vus: set[int] = set(etat.get("ids", []))
    erreurs_4xx_consecutives = int(etat.get("erreurs_4xx", 0))
    alerte_4xx_envoyee = bool(etat.get("alerte_4xx_envoyee", False))

    while True:
        cycle_reussi = False
        try:
            items = recuperer_logements()
            cycle_reussi = True

        except ErreurHttp4xx as e:
            erreurs_4xx_consecutives += 1
            log(f"ERREUR API (4xx) : {e} "
                f"[{erreurs_4xx_consecutives}/{SEUIL_ERREURS_4XX}]")
            # 4xx répété = très probablement un ID de phase périmé.
            if (erreurs_4xx_consecutives >= SEUIL_ERREURS_4XX
                    and not alerte_4xx_envoyee):
                notifier(
                    "CROUS : vérifie l'ID de l'endpoint",
                    f"L'API répond en erreur 4xx depuis "
                    f"{erreurs_4xx_consecutives} cycles ({e}).\n"
                    "L'ID de phase (SEARCH_ID) est probablement périmé : "
                    "consulte https://trouverunlogement.lescrous.fr/api/fr/tools "
                    "pour trouver le nouveau.",
                    tags=["warning"], priorite=4,
                )
                alerte_4xx_envoyee = True  # une seule alerte jusqu'au rétablissement
            # On persiste le compteur pour que le seuil fonctionne aussi
            # quand chaque cycle est un processus séparé (GitHub Actions).
            sauvegarder_etat(initialise, ids_vus,
                             erreurs_4xx_consecutives, alerte_4xx_envoyee)

        except requests.RequestException as e:
            log(f"ERREUR réseau : {e} — nouvelle tentative au prochain cycle.")

        except (ValueError, KeyError, TypeError) as e:
            log(f"ERREUR de structure JSON : {e} — l'API a peut-être changé. "
                "Nouvelle tentative au prochain cycle.")

        except Exception as e:  # filet de sécurité : ne jamais crasher
            log(f"ERREUR inattendue : {type(e).__name__}: {e}")

        if cycle_reussi:
            # --- Cycle réussi : on remet les compteurs d'erreur à zéro -------
            if alerte_4xx_envoyee:
                log("L'API répond de nouveau normalement.")
            erreurs_4xx_consecutives = 0
            alerte_4xx_envoyee = False

            ids_actuels = {i.get("id") for i in items if i.get("id") is not None}

            if not initialise:
                # Premier lancement : on enregistre l'existant SANS notifier
                # chaque logement, juste une confirmation que tout fonctionne.
                ids_vus = ids_actuels
                initialise = True
                log(f"Premier lancement : {len(ids_vus)} logement(s) déjà en "
                    "ligne, enregistrés sans notification.")
                notifier(
                    "Surveillance CROUS active",
                    f"Surveillance de la zone Rouen démarrée : "
                    f"{len(ids_vus)} logement(s) actuellement en ligne.",
                    lien="https://trouverunlogement.lescrous.fr/",
                    tags=["white_check_mark"], priorite=3,
                )

            else:
                nouveaux = ids_actuels - ids_vus
                if nouveaux:
                    log(f"{len(nouveaux)} NOUVEAU(X) logement(s) détecté(s) !")
                    for item in items:
                        item_id, nom, residence, loyer = extraire_infos(item)
                        if item_id not in nouveaux:
                            continue
                        lien = FICHE_URL.format(search_id=SEARCH_ID,
                                                item_id=item_id)
                        log(f"  -> {nom} | {residence} | {loyer} | {lien}")
                        notifier(
                            f"Nouveau logement CROUS : {nom}",
                            f"Résidence : {residence}\nLoyer : {loyer}\n"
                            "Fonce, les places partent vite !",
                            lien=lien, tags=["house", "tada"], priorite=5,
                        )
                    ids_vus |= nouveaux
                else:
                    log(f"Aucun nouveau logement ({len(ids_actuels)} en ligne).")

                # Note : on ne retire PAS de ids_vus les logements disparus,
                # pour éviter de re-notifier un logement qui clignote (pris
                # puis libéré, ou zone momentanément vide côté API).

            sauvegarder_etat(initialise, ids_vus)

        if RUN_ONCE:
            log("Cycle terminé (mode RUN_ONCE), arrêt.")
            return
        time.sleep(INTERVALLE_SECONDES)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Arrêt demandé (Ctrl+C). À bientôt !")
        sys.exit(0)
