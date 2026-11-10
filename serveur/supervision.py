"""Serveur de supervision des postes.

Reçoit l'état des postes envoyé par le collecteur PowerShell, le garde dans
une base SQLite et le pousse en direct (Server-Sent Events) vers la page web.

Uniquement la bibliothèque standard de Python (3.9+) : rien à installer.

    python supervision.py --port 8080 --token "un-secret-partage"
"""

from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import os
import queue
import sqlite3
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ETATS = {"utilise", "verrouille", "deconnecte", "libre", "erreur", "hors_ligne"}
CHAMPS = ("groupe", "ou", "ip", "utilisateur", "nom_complet", "service",
          "inactif_min", "demarrage", "erreur")
TAILLE_MAX_REQUETE = 5 * 1024 * 1024
STATIQUE = Path(__file__).resolve().parent / "statique"

SCHEMA = """
CREATE TABLE IF NOT EXISTS postes (
    nom          TEXT PRIMARY KEY,
    etat         TEXT NOT NULL,
    groupe       TEXT,
    ou           TEXT,
    ip           TEXT,
    utilisateur  TEXT,
    nom_complet  TEXT,
    service      TEXT,
    inactif_min  INTEGER,
    demarrage    TEXT,
    erreur       TEXT,
    depuis       REAL NOT NULL,
    vu_le        REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS evenements (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    nom          TEXT NOT NULL,
    etat         TEXT NOT NULL,
    ancien_etat  TEXT,
    utilisateur  TEXT
);
CREATE INDEX IF NOT EXISTS evenements_nom_ts ON evenements (nom, ts);
"""


class Magasin:
    """État courant des postes et historique des changements (SQLite)."""

    def __init__(self, chemin: str, delai_perime: int = 300):
        self.delai_perime = delai_perime
        self._verrou = threading.Lock()
        self._db = sqlite3.connect(chemin, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def integrer(self, postes: list[dict], maintenant: float | None = None) -> list[dict]:
        """Enregistre un relevé et renvoie les postes dont l'affichage change."""
        maintenant = time.time() if maintenant is None else maintenant
        modifies = []
        with self._verrou, self._db:
            for brut in postes:
                p = normaliser(brut)
                if p is None:
                    continue
                ancien = self._db.execute(
                    "SELECT * FROM postes WHERE nom = ?", (p["nom"],)).fetchone()
                change_etat = (ancien is None or ancien["etat"] != p["etat"]
                               or (ancien["utilisateur"] or "") != (p["utilisateur"] or ""))
                depuis = maintenant if change_etat else ancien["depuis"]
                self._db.execute(
                    f"""INSERT OR REPLACE INTO postes
                        (nom, etat, {", ".join(CHAMPS)}, depuis, vu_le)
                        VALUES (?, ?, {", ".join("?" * len(CHAMPS))}, ?, ?)""",
                    (p["nom"], p["etat"], *(p[c] for c in CHAMPS), depuis, maintenant))
                if change_etat:
                    self._db.execute(
                        "INSERT INTO evenements (ts, nom, etat, ancien_etat, utilisateur)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (maintenant, p["nom"], p["etat"],
                         ancien["etat"] if ancien else None, p["utilisateur"]))
                # On ne pousse que ce qui change à l'écran, pas chaque relevé.
                if ancien is None or change_etat or any(ancien[c] != p[c] for c in CHAMPS) \
                        or ancien_perime(ancien, maintenant, self.delai_perime):
                    modifies.append(self._en_dict(p["nom"], maintenant))
        return modifies

    def postes(self, maintenant: float | None = None) -> list[dict]:
        maintenant = time.time() if maintenant is None else maintenant
        with self._verrou:
            lignes = self._db.execute("SELECT * FROM postes ORDER BY nom").fetchall()
        return [self._ligne_en_dict(l, maintenant) for l in lignes]

    def evenements(self, nom: str | None = None, limite: int = 50) -> list[dict]:
        limite = max(1, min(limite, 1000))
        with self._verrou:
            if nom:
                lignes = self._db.execute(
                    "SELECT * FROM evenements WHERE nom = ? ORDER BY ts DESC LIMIT ?",
                    (nom.upper(), limite)).fetchall()
            else:
                lignes = self._db.execute(
                    "SELECT * FROM evenements ORDER BY ts DESC LIMIT ?", (limite,)).fetchall()
        return [dict(l) for l in lignes]

    def purger(self, jours: int) -> None:
        with self._verrou, self._db:
            self._db.execute("DELETE FROM evenements WHERE ts < ?",
                             (time.time() - jours * 86400,))

    def _en_dict(self, nom: str, maintenant: float) -> dict:
        ligne = self._db.execute("SELECT * FROM postes WHERE nom = ?", (nom,)).fetchone()
        return self._ligne_en_dict(ligne, maintenant)

    def _ligne_en_dict(self, ligne: sqlite3.Row, maintenant: float) -> dict:
        d = dict(ligne)
        d["perime"] = ancien_perime(ligne, maintenant, self.delai_perime)
        return d


def ancien_perime(ligne, maintenant: float, delai: int) -> bool:
    """Vrai si le collecteur n'a plus donné de nouvelles du poste depuis trop longtemps."""
    return maintenant - ligne["vu_le"] > delai


def normaliser(brut) -> dict | None:
    if not isinstance(brut, dict):
        return None
    nom = str(brut.get("nom") or "").strip().upper()
    if not nom or len(nom) > 64:
        return None
    etat = str(brut.get("etat") or "").strip()
    if etat not in ETATS:
        etat = "erreur"
    p = {"nom": nom, "etat": etat}
    for c in CHAMPS:
        v = brut.get(c)
        if c == "inactif_min":
            try:
                v = None if v in (None, "") else max(0, int(v))
            except (TypeError, ValueError):
                v = None
        else:
            v = None if v in (None, "") else str(v)[:256]
        p[c] = v
    if etat in ("libre", "hors_ligne"):
        p["utilisateur"] = p["nom_complet"] = p["service"] = None
    return p


class Diffuseur:
    """Liste des navigateurs abonnés au flux temps réel."""

    def __init__(self):
        self._abonnes: set[queue.Queue] = set()
        self._verrou = threading.Lock()

    def abonner(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._verrou:
            self._abonnes.add(q)
        return q

    def desabonner(self, q: queue.Queue) -> None:
        with self._verrou:
            self._abonnes.discard(q)

    def publier(self, type_: str, donnees) -> None:
        message = f"event: {type_}\ndata: {json.dumps(donnees, ensure_ascii=False)}\n\n"
        with self._verrou:
            abonnes = list(self._abonnes)
        for q in abonnes:
            try:
                q.put_nowait(message)
            except queue.Full:
                # Navigateur trop lent : on le déconnecte, il se reconnectera
                # et recevra un état complet.
                self.desabonner(q)
                try:
                    while True:
                        q.get_nowait()
                except queue.Empty:
                    pass
                q.put_nowait(None)


def creer_gestionnaire(magasin: Magasin, diffuseur: Diffuseur, token: str):
    class Gestionnaire(BaseHTTPRequestHandler):
        server_version = "SupervisionESK/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # journal plus sobre
            if os.environ.get("SUPERVISION_DEBUG"):
                super().log_message(fmt, *args)

        # --- GET -------------------------------------------------------
        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/api/postes":
                return self._json(magasin.postes())
            if url.path == "/api/evenements":
                q = parse_qs(url.query)
                try:
                    limite = int(q.get("limite", ["50"])[0])
                except ValueError:
                    limite = 50
                return self._json(magasin.evenements(q.get("nom", [None])[0], limite))
            if url.path == "/api/flux":
                return self._flux()
            return self._fichier(url.path)

        # --- POST ------------------------------------------------------
        def do_POST(self):
            if urlparse(self.path).path != "/api/releve":
                return self._erreur(HTTPStatus.NOT_FOUND)
            fourni = self.headers.get("X-Token", "")
            if not token or not hmac.compare_digest(fourni.encode(), token.encode()):
                return self._erreur(HTTPStatus.UNAUTHORIZED)
            try:
                longueur = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                longueur = -1
            if longueur <= 0 or longueur > TAILLE_MAX_REQUETE:
                return self._erreur(HTTPStatus.BAD_REQUEST)
            try:
                corps = json.loads(self.rfile.read(longueur).decode("utf-8-sig"))
            except (ValueError, UnicodeDecodeError):
                return self._erreur(HTTPStatus.BAD_REQUEST)
            postes = corps.get("postes") if isinstance(corps, dict) else corps
            if not isinstance(postes, list):
                return self._erreur(HTTPStatus.BAD_REQUEST)
            modifies = magasin.integrer(postes)
            if modifies:
                diffuseur.publier("maj", modifies)
            return self._json({"recus": len(postes), "modifies": len(modifies)})

        # --- outils ----------------------------------------------------
        def _flux(self):
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q = diffuseur.abonner()
            try:
                self._ecrire(f"retry: 3000\nevent: complet\ndata: "
                             f"{json.dumps(magasin.postes(), ensure_ascii=False)}\n\n")
                while True:
                    try:
                        message = q.get(timeout=15)
                    except queue.Empty:
                        message = ": battement\n\n"
                    if message is None:
                        break
                    self._ecrire(message)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                diffuseur.desabonner(q)
                self.close_connection = True

        def _ecrire(self, texte: str):
            self.wfile.write(texte.encode("utf-8"))
            self.wfile.flush()

        def _fichier(self, chemin: str):
            nom = "index.html" if chemin in ("", "/") else chemin.lstrip("/")
            cible = (STATIQUE / nom).resolve()
            if STATIQUE not in cible.parents or not cible.is_file():
                return self._erreur(HTTPStatus.NOT_FOUND)
            contenu = cible.read_bytes()
            type_ = mimetypes.guess_type(cible.name)[0] or "application/octet-stream"
            if type_.startswith("text/") or type_ == "application/javascript":
                type_ += "; charset=utf-8"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", type_)
            self.send_header("Content-Length", str(len(contenu)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(contenu)

        def _json(self, donnees, statut=HTTPStatus.OK):
            corps = json.dumps(donnees, ensure_ascii=False).encode("utf-8")
            self.send_response(statut)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(corps)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(corps)

        def _erreur(self, statut: HTTPStatus):
            self._json({"erreur": statut.phrase}, statut)

    return Gestionnaire


def surveiller_peremption(magasin: Magasin, diffuseur: Diffuseur, arret: threading.Event):
    """Prévient les navigateurs quand un poste n'est plus remonté par le collecteur."""
    deja = set()
    while not arret.wait(30):
        postes = magasin.postes()
        perimes = {p["nom"] for p in postes if p["perime"]}
        nouveaux = [p for p in postes if p["nom"] in perimes - deja]
        if nouveaux:
            diffuseur.publier("maj", nouveaux)
        deja = perimes


def main(argv=None):
    ap = argparse.ArgumentParser(description="Serveur de supervision des postes")
    ap.add_argument("--hote", default=os.environ.get("SUPERVISION_HOTE", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("SUPERVISION_PORT", "8080")))
    ap.add_argument("--base", default=os.environ.get("SUPERVISION_BASE", "supervision.db"),
                    help="fichier SQLite")
    ap.add_argument("--token", default=os.environ.get("SUPERVISION_TOKEN", ""),
                    help="secret partagé avec le collecteur (en-tête X-Token)")
    ap.add_argument("--perime", type=int, default=300,
                    help="secondes sans nouvelles avant de griser un poste")
    ap.add_argument("--historique-jours", type=int, default=90,
                    help="durée de conservation de l'historique")
    args = ap.parse_args(argv)
    if not args.token:
        ap.error("un token est obligatoire (--token ou variable SUPERVISION_TOKEN)")

    magasin = Magasin(args.base, args.perime)
    magasin.purger(args.historique_jours)
    diffuseur = Diffuseur()
    arret = threading.Event()
    threading.Thread(target=surveiller_peremption, args=(magasin, diffuseur, arret),
                     daemon=True).start()

    serveur = ThreadingHTTPServer((args.hote, args.port),
                                  creer_gestionnaire(magasin, diffuseur, args.token))
    serveur.daemon_threads = True
    print(f"Supervision : http://{args.hote}:{args.port}/  (base : {args.base})", flush=True)
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        arret.set()
        serveur.server_close()


if __name__ == "__main__":
    main()
