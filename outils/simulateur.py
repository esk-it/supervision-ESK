"""Simulateur : envoie de faux relevés au serveur pour tester l'écran sans AD.

    python outils/simulateur.py --url http://localhost:8080 --token secret
"""

import argparse
import json
import random
import time
import urllib.request

# classe : (préfixe du nom de poste, nombre de postes)
GROUPES = {"Salle A01": ("A01", 16), "Salle A02": ("A02", 16), "Salle A03": ("A03", 20),
           "Salle B11 – Informatique": ("B11", 30), "Salle B12 – Informatique": ("B12", 30),
           "Salle B13 – Réseaux": ("B13", 24), "Salle C21 – CAO": ("C21", 24),
           "Salle C22 – Bureautique": ("C22", 20), "CDI": ("CDI", 18), "Salle de langues": ("LAN", 24),
           "Laboratoire": ("LAB", 16), "Salle D31": ("D31", 20), "Salle D32": ("D32", 20),
           "Administration": ("ADM", 22), "Vie scolaire": ("VSC", 8), "Salle des profs": ("PRF", 12),
           "Salle E41 – Projets": ("E41", 28), "Amphithéâtre": ("AMP", 6), "Accueil": ("ACC", 4),
           "Direction": ("DIR", 6), "Salle E42": ("E42", 36)}
PRENOMS = ["Julie", "Thomas", "Sarah", "Nicolas", "Camille", "Lucas", "Emma", "Hugo",
           "Léa", "Antoine", "Chloé", "Maxime", "Manon", "Pierre", "Inès", "Romain"]
NOMS = ["Martin", "Bernard", "Dubois", "Durand", "Lefebvre", "Moreau", "Laurent",
        "Simon", "Michel", "Garcia", "Roux", "Fournier", "Girard", "Bonnet", "Lambert"]


def creer_parc(graine: int):
    rnd = random.Random(graine)
    parc = []
    for groupe, (prefixe, nombre) in GROUPES.items():
        for i in range(1, nombre + 1):
            prenom, nom = rnd.choice(PRENOMS), rnd.choice(NOMS)
            parc.append({
                "nom": f"PC-{prefixe}-{i:03d}",
                "groupe": groupe,
                "ou": f"OU={groupe},OU=Postes,DC=esk,DC=local",
                "ip": f"10.{list(GROUPES).index(groupe) + 10}.0.{i + 10}",
                "proprio": (f"ESK\\{prenom[0].lower()}{nom.lower()}", f"{prenom} {nom}", groupe),
                "etat": rnd.choices(["utilise", "verrouille", "deconnecte", "libre", "hors_ligne", "erreur"],
                                    [45, 12, 3, 10, 29, 1])[0],
                "inactif_min": 0,
            })
    return parc


def faire_evoluer(parc, rnd):
    for p in parc:
        if rnd.random() < 0.03:
            p["etat"] = rnd.choices(["utilise", "verrouille", "deconnecte", "libre", "hors_ligne"],
                                    [50, 20, 3, 10, 17])[0]
            p["inactif_min"] = 0
        elif p["etat"] == "utilise":
            p["inactif_min"] = p["inactif_min"] + 1 if rnd.random() < 0.3 else 0


def releve(parc):
    sortie = []
    for p in parc:
        r = {k: p[k] for k in ("nom", "groupe", "ou", "etat")}
        if p["etat"] != "hors_ligne":
            r["ip"] = p["ip"]
            r["demarrage"] = "10/11/2026 07:45"
        if p["etat"] in ("utilise", "verrouille", "deconnecte"):
            r["utilisateur"], r["nom_complet"], r["service"] = p["proprio"]
            r["inactif_min"] = p["inactif_min"]
        if p["etat"] == "erreur":
            r["erreur"] = "Accès refusé (WinRM)"
        sortie.append(r)
    return sortie


def envoyer(url, token, postes):
    corps = json.dumps({"postes": postes}).encode("utf-8")
    req = urllib.request.Request(url.rstrip("/") + "/api/releve", data=corps, method="POST",
                                 headers={"Content-Type": "application/json", "X-Token": token})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--token", required=True)
    ap.add_argument("--intervalle", type=float, default=5)
    ap.add_argument("--une-fois", action="store_true")
    ap.add_argument("--graine", type=int, default=29)
    args = ap.parse_args()
    rnd = random.Random(args.graine)
    parc = creer_parc(args.graine)
    while True:
        print(envoyer(args.url, args.token, releve(parc)), flush=True)
        if args.une_fois:
            break
        time.sleep(args.intervalle)
        faire_evoluer(parc, rnd)


if __name__ == "__main__":
    main()
