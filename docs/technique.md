# Documentation technique

Ce document décrit le fonctionnement interne de la supervision des postes : architecture,
format des échanges, modèle de données et points d'exploitation. Pour l'installation et les
prérequis GPO, voir le [README](../README.md).

## 1. Architecture

```
                       ┌──────────────────────── serveur de supervision (Windows, membre du domaine) ───────────────────────┐
                       │                                                                                                     │
 Active Directory ◄────┤ Collecter-Postes.ps1 (PowerShell 7, gMSA)          supervision.py (Python 3.9+, stdlib)              │
  Get-ADComputer       │   boucle toutes les 30 s                             ThreadingHTTPServer                             │
  Get-ADUser           │   ForEach-Object -Parallel (64)  ── POST /api/releve ──►  Magasin (SQLite, WAL)                      │
                       │            │                       X-Token               Diffuseur (SSE)  ──── GET /api/flux ───────┼──► navigateurs
                       └────────────┼─────────────────────────────────────────────────────────────────────────────────────────┘    (écran mural)
                                    │ TCP 5985 + WS-Man/CIM, RPC (quser, facultatif)
                                    ▼
                              ~400 postes Windows (aucun agent)
```

Trois composants indépendants :

| Composant | Langage | Rôle | Dépendances |
|---|---|---|---|
| `collecteur/Collecter-Postes.ps1` | PowerShell 7 | Interroger les postes et pousser un relevé complet au serveur | Module `ActiveDirectory` (RSAT) |
| `serveur/supervision.py` | Python 3.9+ | Stocker l'état, l'historique, diffuser les changements | Aucune (bibliothèque standard) |
| `serveur/statique/` | HTML/CSS/JS | Afficher l'état en direct | Aucune (pas de framework, pas de CDN) |

Le collecteur et le serveur ne se connaissent que par l'URL du serveur et le token partagé :
on peut les déplacer sur deux machines différentes sans rien changer d'autre.

## 2. Le collecteur

### 2.1 Cycle d'un passage

1. **Liste des postes.** Toutes les `RafraichirADMinutes` (15 min par défaut), `Get-ADComputer`
   est lancé sur chaque OU de `OUs` avec `FiltreAD`. Pour chaque ordinateur on retient le nom,
   le `DNSHostName` (à défaut le nom NetBIOS), la classe et le DN de l'OU parente.
2. **Classe.** Par défaut, la première OU du DN (l'OU qui contient directement le poste).
   Si `RegexClasse` est renseignée et que le nom du poste y correspond, le groupe nommé
   `classe` l'emporte. `NomsClasses` traduit enfin la valeur en libellé affiché.
3. **Sondage en parallèle.** `ForEach-Object -Parallel -ThrottleLimit <Parallelisme>` exécute
   le bloc `$sondePoste` sur chaque poste (voir 2.2). Le bloc est transmis sous forme de texte
   et recréé dans chaque runspace, car un scriptblock ne peut pas traverser `$using:`.
4. **Enrichissement AD.** Pour chaque poste avec un utilisateur, `Get-ADUser` fournit
   `DisplayName` et `Department`. Le résultat est mis en cache une heure par login ; un compte
   local ou d'un domaine non approuvé reste simplement sans nom complet.
5. **Envoi.** Le relevé complet est envoyé en un seul `POST /api/releve` (JSON UTF-8, délai 30 s).
   Une ligne de journal résume le passage : nombre de postes, durée, répartition par état,
   nombre de changements retenus par le serveur.
6. **Attente.** Le passage suivant démarre `IntervalleSecondes` après le **début** du précédent
   (si un passage dure plus longtemps que l'intervalle, le suivant part immédiatement).

Une exception dans un passage est journalisée en `[ERREUR]` et n'arrête pas la boucle.

### 2.2 Sondage d'un poste

| Étape | Requête | Résultat |
|---|---|---|
| 1 | Connexion TCP au port 5985 (`DelaiSecondes`) | Échec ou délai dépassé : `hors_ligne`, fin. Sinon l'IP est relevée. |
| 2 | `New-CimSession` (WS-Man) | Échec : `erreur` avec la 1re ligne du message, fin. |
| 3 | `Win32_ComputerSystem.UserName` | Utilisateur de la session console. |
| 4 | `Win32_OperatingSystem.LastBootUpTime` | Date de démarrage affichée dans le détail. |
| 5 | `Win32_Process` filtré sur `LogonUI.exe` | Présent avec un utilisateur : `verrouille`, sinon `utilise`. |
| 6 | *Seulement sans utilisateur console :* `Win32_Process` `explorer.exe` puis `GetOwner()` | Propriétaire trouvé : `deconnecte` (session RDP ou changement d'utilisateur). Sinon `libre`. |
| 7 | *Facultatif :* `quser /server:<poste>` | Temps d'inactivité en minutes, le plus faible des sessions du même login. |

Remarques :

- `LogonUI.exe` tourne aussi sur l'écran d'ouverture de session, mais dans ce cas `UserName`
  est vide : le poste est bien vu `libre`.
- À l'étape 6, seul le premier `explorer.exe` est examiné : si plusieurs sessions sont ouvertes
  en arrière-plan, un seul utilisateur est remonté.
- L'analyse de `quser` lit les colonnes depuis la fin de ligne (séparateur : deux espaces ou plus),
  ce qui la rend indépendante de la langue de Windows. Formats d'inactivité reconnus :
  `J+HH:MM`, `HH:MM`, `MM` ; tout le reste (`.`, `aucun`, `none`) vaut 0.
- Une erreur de `quser` est ignorée silencieusement : l'inactivité reste vide.

### 2.3 Charge générée

Par poste allumé et par passage : une connexion TCP, une session WS-Man et 3 à 4 requêtes CIM,
plus un appel RPC si `Quser` est actif. Avec 400 postes, 64 en parallèle et un délai de 5 s,
un passage dure typiquement quelques secondes ; le pire cas (tous les postes muets au délai
maximal) est d'environ `ceil(400 / 64) × 5 s ≈ 35 s`, soit un peu plus que l'intervalle par défaut.
Si le journal montre des passages plus longs que l'intervalle, augmenter `Parallelisme` ou
réduire `DelaiSecondes`.

## 3. Le serveur

### 3.1 Organisation

| Élément | Rôle |
|---|---|
| `Magasin` | Accès SQLite. Une seule connexion partagée, protégée par un verrou ; journal en mode WAL. |
| `normaliser()` | Valide et nettoie chaque poste reçu (voir 3.3). |
| `Diffuseur` | Une file (`queue.Queue`, 100 messages) par navigateur abonné au flux. |
| `Gestionnaire` | Routes HTTP (voir 4). Un thread par connexion (`ThreadingHTTPServer`). |
| `surveiller_peremption()` | Thread de fond : toutes les 30 s, pousse les postes qui viennent de devenir périmés. |

Au démarrage, l'historique plus ancien que `--historique-jours` est purgé. La purge n'a lieu
qu'au démarrage : sur un serveur qui ne redémarre jamais, prévoir un redémarrage périodique de la
tâche planifiée (ou ajouter une purge quotidienne).

### 3.2 Options

| Option | Variable d'environnement | Défaut | Rôle |
|---|---|---|---|
| `--hote` | `SUPERVISION_HOTE` | `0.0.0.0` | Adresse d'écoute |
| `--port` | `SUPERVISION_PORT` | `8080` | Port d'écoute |
| `--base` | `SUPERVISION_BASE` | `supervision.db` | Fichier SQLite |
| `--token` | `SUPERVISION_TOKEN` | *(obligatoire)* | Secret attendu dans `X-Token` |
| `--perime` | | `300` | Secondes sans relevé avant de griser un poste |
| `--historique-jours` | | `90` | Conservation des événements |
| | `SUPERVISION_DEBUG` | | Si définie, journalise chaque requête HTTP |

### 3.3 Intégration d'un relevé

Pour chaque poste du relevé :

1. **Normalisation.** Nom en majuscules, 1 à 64 caractères, sinon le poste est ignoré.
   Un état inconnu devient `erreur`. Les champs texte sont tronqués à 256 caractères, les
   chaînes vides deviennent `NULL`, `inactif_min` est converti en entier positif.
   Pour `libre` et `hors_ligne`, l'utilisateur, le nom complet et le service sont effacés.
2. **Changement d'état.** Il y a changement si le poste est nouveau, si l'état diffère ou si
   l'utilisateur diffère. Dans ce cas `depuis` reprend l'heure courante et une ligne est ajoutée
   à `evenements`. Sinon `depuis` est conservé.
3. **Mise à jour.** La ligne de `postes` est remplacée et `vu_le` reprend l'heure courante.
4. **Diffusion.** Le poste n'est poussé aux navigateurs que si quelque chose change à l'écran :
   état, utilisateur, un autre champ (IP, inactivité, classe…), ou retour d'un poste périmé.
   Un relevé identique au précédent ne génère donc aucun trafic vers les navigateurs.

L'ensemble du relevé est traité dans une seule transaction.

Un poste qui disparaît de l'AD (supprimé, déplacé hors des OU, désactivé) n'est plus relevé :
il devient périmé au bout de `--perime` secondes mais reste en base et à l'écran. Pour le
retirer : `DELETE FROM postes WHERE nom = 'PC-XXX';` serveur arrêté, ou serveur en marche puis
recharger la page.

## 4. API HTTP

Toutes les réponses JSON sont en UTF-8. Aucune route en lecture n'est authentifiée.

### `POST /api/releve`

Réservée au collecteur.

- En-tête `X-Token` obligatoire (comparaison à temps constant). Absent ou faux : `401`.
- Corps : `{"postes": [ ... ]}` ou directement le tableau. Taille maximale 5 Mio.
  `Content-Length` absent, nul ou trop grand, JSON invalide ou sans tableau : `400`.
- Réponse : `{"recus": 400, "modifies": 12}`.

Format d'un poste envoyé :

```json
{
  "nom": "PC-B12-07",
  "etat": "utilise",
  "groupe": "Salle B12 – Informatique",
  "ou": "OU=B12,OU=Postes,DC=esk,DC=local",
  "ip": "10.0.12.57",
  "utilisateur": "ESK\\jdupont",
  "nom_complet": "Julie Dupont",
  "service": "Comptabilité",
  "inactif_min": 3,
  "demarrage": "13/11/2026 07:52",
  "erreur": null
}
```

Seuls `nom` et `etat` sont nécessaires. Valeurs de `etat` : `utilise`, `verrouille`,
`deconnecte`, `libre`, `erreur`, `hors_ligne`.

### `GET /api/postes`

État courant de tous les postes, triés par nom. Chaque objet reprend les champs ci-dessus plus :

| Champ | Type | Sens |
|---|---|---|
| `depuis` | epoch (s) | Début de l'état courant (et de l'utilisateur courant) |
| `vu_le` | epoch (s) | Dernier relevé reçu pour ce poste |
| `perime` | booléen | `true` si `vu_le` date de plus de `--perime` secondes |

### `GET /api/evenements?nom=<poste>&limite=<n>`

Historique des changements d'état, du plus récent au plus ancien. `nom` est facultatif
(tous les postes sinon), `limite` vaut 50 par défaut et est borné entre 1 et 1000.

```json
[{"id": 812, "ts": 1794556800.2, "nom": "PC-B12-07", "etat": "utilise",
  "ancien_etat": "libre", "utilisateur": "ESK\\jdupont"}]
```

### `GET /api/flux`

Flux [Server-Sent Events](https://html.spec.whatwg.org/multipage/server-sent-events.html) :

| Événement | Données | Quand |
|---|---|---|
| `complet` | tableau de tous les postes (format `/api/postes`) | À la connexion |
| `maj` | tableau des postes modifiés | Après un relevé qui change l'affichage, ou quand des postes deviennent périmés |
| commentaire `: battement` | | Toutes les 15 s sans message, pour garder la connexion ouverte à travers les proxys |

Le serveur demande au navigateur de se reconnecter après 3 s (`retry: 3000`). Un navigateur qui
n'absorbe pas ses messages (file pleine à 100) est déconnecté ; à la reconnexion, il reçoit un
nouvel état `complet`, ce qui garantit qu'aucun écran ne reste désynchronisé.

### Autres chemins

Servis depuis `serveur/statique/` (`/` donne `index.html`), avec protection contre la remontée
de répertoire. Tout le reste : `404`.

## 5. Modèle de données (SQLite)

```sql
postes (                       -- une ligne par poste, état courant
  nom TEXT PRIMARY KEY,        -- en majuscules
  etat TEXT NOT NULL,
  groupe, ou, ip, utilisateur, nom_complet, service TEXT,
  inactif_min INTEGER,
  demarrage TEXT,              -- texte déjà formaté par le collecteur
  erreur TEXT,
  depuis REAL NOT NULL,        -- epoch, début de l'état courant
  vu_le  REAL NOT NULL         -- epoch, dernier relevé
)

evenements (                   -- un changement d'état ou d'utilisateur par ligne
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, nom TEXT NOT NULL,
  etat TEXT NOT NULL, ancien_etat TEXT, utilisateur TEXT
)
INDEX evenements_nom_ts ON evenements (nom, ts)
```

Volume : `postes` reste à environ 400 lignes. `evenements` reçoit quelques dizaines de lignes
par poste et par jour (connexions, verrouillages, extinctions), soit de l'ordre de 10 000
lignes par jour et un peu moins d'un million sur 90 jours : quelques dizaines de Mo.

Le fichier peut être lu à chaud avec `sqlite3` (mode WAL), par exemple pour un export :

```sql
-- Taux d'utilisation par classe sur les 7 derniers jours (nombre de sessions ouvertes)
SELECT p.groupe, COUNT(*) AS ouvertures
FROM evenements e JOIN postes p USING (nom)
WHERE e.etat = 'utilise' AND e.ancien_etat <> 'verrouille'
  AND e.ts > strftime('%s','now') - 7*86400
GROUP BY p.groupe ORDER BY ouvertures DESC;
```

Sauvegarde : copier `supervision.db`, `supervision.db-wal` et `supervision.db-shm` serveur
arrêté, ou utiliser `sqlite3 supervision.db ".backup sauvegarde.db"` à chaud.

## 6. La page web

- `index.html`, `style.css`, `app.js` : aucun framework, aucune ressource externe, utilisable
  sur un réseau sans accès internet.
- Au chargement, la page ouvre `/api/flux`. L'événement `complet` remplace tout l'état local,
  `maj` met à jour les postes concernés. Le rendu est regroupé par `requestAnimationFrame`.
- Les durées (« depuis 12 min ») sont recalculées toutes les 30 s côté navigateur, sans requête.
- Le panneau de détail interroge `/api/evenements?nom=…&limite=30` à l'ouverture.
- Les filtres (états cochés, classe, masquage des postes hors ligne) et le thème choisi sont
  gardés dans le `localStorage` du navigateur : chaque écran garde ses propres réglages.
- En cas de coupure du flux, l'indicateur passe à « reconnexion… » et le navigateur se
  reconnecte seul.

## 7. Exploitation et dépannage

| Symptôme | Cause probable | Vérification |
|---|---|---|
| Tous les postes « Hors ligne » | Pare-feu ou GPO WinRM non appliquée, DNS | `Test-NetConnection PC-XXX -Port 5985` depuis le serveur |
| Postes en « Erreur WinRM », *Accès refusé* | Le compte du collecteur n'est pas administrateur local | `Get-CimInstance -ComputerName PC-XXX Win32_ComputerSystem` sous le gMSA |
| Postes en « Erreur WinRM », *WinRM cannot complete the operation* | Service WinRM arrêté ou listener absent | `Test-WSMan PC-XXX` ; `winrm enumerate winrm/config/listener` sur le poste |
| Pas de temps d'inactivité | `AllowRemoteRPC` absent ou RPC filtré | `quser /server:PC-XXX` depuis le serveur |
| Tous les postes grisés en pointillés | Le collecteur ne tourne plus ou n'atteint plus le serveur | Journal du collecteur, état de la tâche planifiée |
| Journal du collecteur : `401` | Token différent entre `config.json` et le serveur | Comparer les deux valeurs |
| La page reste sur « reconnexion… » | Proxy inverse qui bufferise le flux | Désactiver la mise en tampon (IIS/ARR : `responseBufferLimit="0"`) |
| Écran figé après des heures | Mise en veille du poste d'affichage | Désactiver la veille de l'écran mural |

Journaux : le collecteur écrit sur la sortie standard (rediriger vers un fichier dans l'action
de la tâche planifiée si besoin, par ex. `-File ... *>> C:\Supervision\collecteur.log`).
Le serveur n'écrit qu'une ligne au démarrage, sauf avec `SUPERVISION_DEBUG=1`.

## 8. Limites connues

- La page et l'API en lecture ne sont pas authentifiées et le serveur ne parle que HTTP :
  restreindre l'accès au réseau d'administration ou passer par un proxy inverse HTTPS.
- Un thread est ouvert par navigateur connecté au flux : prévu pour quelques écrans, pas
  pour des centaines de clients.
- Une seule session « déconnectée » est détectée par poste (voir 2.2).
- L'état est échantillonné toutes les `IntervalleSecondes` : un verrouillage de quelques
  secondes entre deux passages n'apparaît pas dans l'historique.
- La purge de l'historique n'a lieu qu'au démarrage du serveur (voir 3.1).

## 9. Tests et développement

```bash
python -m unittest discover -s tests          # tests du serveur (Magasin et API HTTP)
python serveur/supervision.py --port 8080 --token test
python outils/simulateur.py --url http://localhost:8080 --token test   # faux parc de 400 postes
```

Les tests couvrent l'intégration des relevés (création, relevé identique, changement
d'utilisateur, péremption, normalisation) et l'API HTTP (token, erreurs de format, flux).
Le collecteur n'a pas de tests automatisés : le tester avec `-UneFois -Afficher` sur une OU
réduite avant de le planifier.
