"""Simulateur : envoie de faux relevés au serveur pour tester l'écran sans AD.

    python outils/simulateur.py --url http://localhost:8080 --token secret
"""

import argparse
import json
import random
import time
import urllib.request

# groupe : (préfixe du nom de poste, nombre de postes)
GROUPES = {"Accueil": ("ACC", 12), "Comptabilité": ("CPT", 40), "Direction": ("DIR", 15),
           "Informatique": ("INF", 25), "Production": ("PRD", 120), "Commercial": ("COM", 60),
           "Logistique": ("LOG", 80), "Salle de formation": ("FOR", 48)}
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
