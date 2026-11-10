import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "serveur"))
import supervision as s  # noqa: E402


class TestMagasin(unittest.TestCase):
    def setUp(self):
        self.m = s.Magasin(":memory:", delai_perime=300)

    def test_premier_releve_cree_le_poste_et_un_evenement(self):
        modifies = self.m.integrer([{"nom": "pc-01", "etat": "utilise", "utilisateur": "ESK\\jdupont"}], 1000)
        self.assertEqual(len(modifies), 1)
        self.assertEqual(modifies[0]["nom"], "PC-01")
        self.assertEqual(modifies[0]["depuis"], 1000)
        self.assertEqual(len(self.m.evenements("PC-01")), 1)

    def test_releve_identique_ne_pousse_rien_et_garde_depuis(self):
        p = {"nom": "PC-01", "etat": "utilise", "utilisateur": "ESK\\jdupont"}
        self.m.integrer([p], 1000)
        self.assertEqual(self.m.integrer([p], 1030), [])
        poste = self.m.postes(1030)[0]
        self.assertEqual(poste["depuis"], 1000)
        self.assertEqual(poste["vu_le"], 1030)
        self.assertEqual(len(self.m.evenements("PC-01")), 1)

    def test_changement_utilisateur_est_un_changement_d_etat(self):
        self.m.integrer([{"nom": "PC-01", "etat": "utilise", "utilisateur": "ESK\\a"}], 1000)
        modifies = self.m.integrer([{"nom": "PC-01", "etat": "utilise", "utilisateur": "ESK\\b"}], 1100)
        self.assertEqual(modifies[0]["depuis"], 1100)
        evts = self.m.evenements("PC-01")
        self.assertEqual([e["utilisateur"] for e in evts], ["ESK\\b", "ESK\\a"])

    def test_verrouillage_enregistre_l_ancien_etat(self):
        self.m.integrer([{"nom": "PC-01", "etat": "utilise", "utilisateur": "ESK\\a"}], 1000)
        self.m.integrer([{"nom": "PC-01", "etat": "verrouille", "utilisateur": "ESK\\a"}], 1100)
        dernier = self.m.evenements("PC-01")[0]
        self.assertEqual((dernier["etat"], dernier["ancien_etat"]), ("verrouille", "utilise"))

    def test_etat_inconnu_devient_erreur_et_libre_efface_l_utilisateur(self):
        self.m.integrer([{"nom": "PC-01", "etat": "nimporte"},
                         {"nom": "PC-02", "etat": "libre", "utilisateur": "ESK\\x"},
                         {"etat": "utilise"}, "pas un objet"], 1000)
        postes = {p["nom"]: p for p in self.m.postes(1000)}
        self.assertEqual(set(postes), {"PC-01", "PC-02"})
        self.assertEqual(postes["PC-01"]["etat"], "erreur")
        self.assertIsNone(postes["PC-02"]["utilisateur"])

    def test_poste_sans_nouvelles_est_perime_puis_revient(self):
        p = {"nom": "PC-01", "etat": "libre"}
        self.m.integrer([p], 1000)
        self.assertTrue(self.m.postes(1400)[0]["perime"])
        modifies = self.m.integrer([p], 1400)
        self.assertEqual(len(modifies), 1)
        self.assertFalse(modifies[0]["perime"])


class TestHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.magasin = s.Magasin(":memory:")
        cls.diffuseur = s.Diffuseur()
        cls.serveur = ThreadingHTTPServer(("127.0.0.1", 0),
                                          s.creer_gestionnaire(cls.magasin, cls.diffuseur, "secret"))
        cls.serveur.daemon_threads = True
        cls.url = f"http://127.0.0.1:{cls.serveur.server_port}"
        threading.Thread(target=cls.serveur.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.serveur.shutdown()
        cls.serveur.server_close()

    def poster(self, corps, token="secret"):
        req = urllib.request.Request(self.url + "/api/releve", data=json.dumps(corps).encode(),
                                     method="POST", headers={"X-Token": token})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)

    def test_token_obligatoire(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.poster({"postes": []}, token="faux")
        self.assertEqual(ctx.exception.code, 401)

    def test_releve_puis_lecture_et_flux(self):
        q = self.diffuseur.abonner()
        rep = self.poster({"postes": [{"nom": "PC-HTTP", "etat": "verrouille",
                                       "utilisateur": "ESK\\léa", "nom_complet": "Léa Roux"}]})
        self.assertEqual(rep, {"recus": 1, "modifies": 1})
        message = q.get(timeout=2)
        self.assertIn("event: maj", message)
        self.assertIn("Léa Roux", message)
        self.diffuseur.desabonner(q)
        with urllib.request.urlopen(self.url + "/api/postes", timeout=5) as r:
            postes = json.load(r)
        self.assertIn("PC-HTTP", [p["nom"] for p in postes])

    def test_pages_statiques_et_traversee_refusee(self):
        with urllib.request.urlopen(self.url + "/", timeout=5) as r:
            self.assertIn(b"Supervision des postes", r.read())
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.url + "/../supervision.py", timeout=5)
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
