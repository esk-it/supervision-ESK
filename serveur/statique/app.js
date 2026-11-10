"use strict";

const ETATS = [
  ["utilise", "Utilisés", "Utilisé"],
  ["verrouille", "Verrouillés", "Verrouillé"],
  ["deconnecte", "Session déconnectée", "Session déconnectée"],
  ["libre", "Libres", "Libre"],
  ["erreur", "Injoignables WinRM", "Erreur WinRM"],
  ["hors_ligne", "Hors ligne", "Hors ligne"],
];
const LIBELLE = Object.fromEntries(ETATS.map(([e, , l]) => [e, l]));
const ORDRE = Object.fromEntries(ETATS.map(([e], i) => [e, i]));

const postes = new Map();
const pref = lirePreferences();
let rendu = 0;
let selection = null;

const $ = (id) => document.getElementById(id);

// --- Préférences (confort local, facultatif) ---------------------------
function lirePreferences() {
  const defaut = { etats: ETATS.map(([e]) => e), groupe: "", masquer: false };
  try {
    return { ...defaut, ...JSON.parse(localStorage.getItem("supervision") || "{}") };
  } catch {
    return defaut;
  }
}
function sauverPreferences() {
  try { localStorage.setItem("supervision", JSON.stringify(pref)); } catch { /* sans importance */ }
}

// --- Outils -------------------------------------------------------------
function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
function duree(depuis) {
  const s = Math.max(0, Date.now() / 1000 - depuis);
  if (s < 60) return "à l'instant";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} h ${String(m % 60).padStart(2, "0")}`;
  return `${Math.floor(h / 24)} j ${h % 24} h`;
}
function heure(ts) {
  return new Date(ts * 1000).toLocaleString("fr-FR", {
    day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}
function login(u) {
  return u ? String(u).split("\\").pop() : "";
}

// --- Rendu ----------------------------------------------------------------
function planifierRendu() {
  if (!rendu) rendu = requestAnimationFrame(() => { rendu = 0; afficher(); });
}

function filtres() {
  const q = $("recherche").value.trim().toLowerCase();
  return (p) =>
    pref.etats.includes(p.etat) &&
    !(pref.masquer && p.etat === "hors_ligne") &&
    (!pref.groupe || (p.groupe || "Sans groupe") === pref.groupe) &&
    (!q || [p.nom, p.utilisateur, p.nom_complet, p.service, p.ip, p.groupe]
      .some((v) => v && String(v).toLowerCase().includes(q)));
}

function afficher() {
  const tous = [...postes.values()];
  afficherCompteurs(tous);
  afficherGroupes(tous);

  const garder = filtres();
  const visibles = tous.filter(garder);
  const contenu = $("contenu");
  if (!tous.length) {
    contenu.innerHTML = '<p class="vide">En attente des premières données du collecteur…</p>';
    return;
  }
  if (!visibles.length) {
    contenu.innerHTML = '<p class="vide">Aucun poste ne correspond aux filtres.</p>';
    return;
  }

  const parGroupe = new Map();
  for (const p of visibles) {
    const g = p.groupe || "Sans groupe";
    if (!parGroupe.has(g)) parGroupe.set(g, []);
    parGroupe.get(g).push(p);
  }
  const maintenant = Date.now() / 1000;
  const html = [...parGroupe.keys()].sort((a, b) => a.localeCompare(b, "fr")).map((g) => {
    const liste = parGroupe.get(g).sort((a, b) => a.nom.localeCompare(b.nom, "fr", { numeric: true }));
    const actifs = liste.filter((p) => p.etat === "utilise").length;
    return `<section class="groupe-poste">
      <h2>${esc(g)} <small>${actifs} utilisé${actifs > 1 ? "s" : ""} sur ${liste.length}</small></h2>
      <div class="grille">${liste.map((p) => tuile(p, maintenant)).join("")}</div>
    </section>`;
  }).join("");
  contenu.innerHTML = html;
}

function tuile(p, maintenant) {
  const qui = p.nom_complet || login(p.utilisateur) ||
    (p.etat === "erreur" ? "Erreur WinRM" : LIBELLE[p.etat]);
  const inactif = p.etat === "utilise" && p.inactif_min >= 5 ? ` · inactif ${p.inactif_min} min` : "";
  const classes = ["tuile", p.etat, `c-${p.etat}`];
  if (p.perime) classes.push("perime");
  if (maintenant - p.depuis < 10) classes.push("recent");
  const titre = [p.nom, LIBELLE[p.etat], p.utilisateur, p.service, p.perime ? "Pas de nouvelles du collecteur" : ""]
    .filter(Boolean).join(" · ");
  return `<button class="${classes.join(" ")}" data-nom="${esc(p.nom)}" title="${esc(titre)}">
    <span class="nom">${esc(p.nom)}</span>
    <span class="qui">${esc(qui)}</span>
    <span class="info"><span>${esc(p.utilisateur ? (p.service || login(p.utilisateur)) : (p.ip || ""))}</span>
      <span>${esc(duree(p.depuis))}${esc(inactif)}</span></span>
  </button>`;
}

function afficherCompteurs(tous) {
  const n = Object.fromEntries(ETATS.map(([e]) => [e, 0]));
  for (const p of tous) n[p.etat] = (n[p.etat] || 0) + 1;
  $("compteurs").innerHTML = ETATS.map(([e, libelle]) =>
    `<button class="compteur c-${e}" data-etat="${e}" aria-pressed="${pref.etats.includes(e)}">
      <strong>${n[e]}</strong><span>${libelle}</span></button>`).join("");
}

function afficherGroupes(tous) {
  const groupes = [...new Set(tous.map((p) => p.groupe || "Sans groupe"))].sort((a, b) => a.localeCompare(b, "fr"));
  const sel = $("groupe");
  const cle = groupes.join("|");
  if (sel.dataset.cle === cle) return;
  sel.dataset.cle = cle;
  sel.innerHTML = `<option value="">Tous les groupes</option>` +
    groupes.map((g) => `<option${g === pref.groupe ? " selected" : ""}>${esc(g)}</option>`).join("");
}

// --- Panneau de détail -------------------------------------------------------
async function ouvrirDetail(nom) {
  selection = nom;
  afficherDetail();
  $("detail").hidden = false;
  try {
    const r = await fetch(`/api/evenements?nom=${encodeURIComponent(nom)}&limite=30`);
    const evts = await r.json();
    if (selection !== nom) return;
    $("historique").innerHTML = evts.length ? evts.map((e) =>
      `<li class="c-${esc(e.etat)}"><span class="pastille"></span><time>${heure(e.ts)}</time>
        <span>${esc(LIBELLE[e.etat] || e.etat)}${e.utilisateur ? " · " + esc(login(e.utilisateur)) : ""}</span></li>`).join("")
      : "<li>Aucun changement enregistré.</li>";
  } catch {
    $("historique").innerHTML = "<li>Historique indisponible.</li>";
  }
}

function afficherDetail() {
  const p = postes.get(selection);
  if (!p) return;
  const lignes = [
    ["Utilisateur", p.nom_complet ? `${p.nom_complet} (${p.utilisateur})` : p.utilisateur],
    ["Service", p.service],
    ["Depuis", `${heure(p.depuis)} (${duree(p.depuis)})`],
    ["Inactivité", p.inactif_min != null ? `${p.inactif_min} min` : null],
    ["Adresse IP", p.ip],
    ["Groupe", p.groupe],
    ["OU", p.ou],
    ["Démarré le", p.demarrage],
    ["Dernier relevé", heure(p.vu_le) + (p.perime ? " (ancien)" : "")],
    ["Erreur", p.erreur],
  ].filter(([, v]) => v);
  const histo = $("historique");
  $("detail-corps").innerHTML = `
    <h2>${esc(p.nom)}</h2>
    <span class="etiquette c-${p.etat}">${esc(LIBELLE[p.etat])}</span>
    <dl>${lignes.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
    <h3>Historique</h3>
    <ul id="historique" class="historique">${histo ? histo.innerHTML : "<li>Chargement…</li>"}</ul>`;
}

// --- Flux temps réel ----------------------------------------------------------
function connecter() {
  const flux = new EventSource("/api/flux");
  const etat = $("connexion");
  flux.onopen = () => { etat.textContent = "en direct"; etat.classList.add("ok"); };
  flux.onerror = () => { etat.textContent = "reconnexion…"; etat.classList.remove("ok"); };
  flux.addEventListener("complet", (e) => {
    postes.clear();
    for (const p of JSON.parse(e.data)) postes.set(p.nom, p);
    planifierRendu();
  });
  flux.addEventListener("maj", (e) => {
    for (const p of JSON.parse(e.data)) postes.set(p.nom, p);
    planifierRendu();
    if (selection && !$("detail").hidden) {
      const change = JSON.parse(e.data).some((p) => p.nom === selection);
      if (change) ouvrirDetail(selection);
    }
  });
}

// --- Évènements de l'interface ------------------------------------------------
$("compteurs").addEventListener("click", (e) => {
  const b = e.target.closest("[data-etat]");
  if (!b) return;
  const etat = b.dataset.etat;
  // Clic simple : n'afficher que cet état ; reclic : tout réafficher.
  const seul = pref.etats.length === 1 && pref.etats[0] === etat;
  pref.etats = seul ? ETATS.map(([x]) => x) : [etat];
  sauverPreferences();
  afficher();
});
$("groupe").addEventListener("change", (e) => { pref.groupe = e.target.value; sauverPreferences(); afficher(); });
$("masquer").checked = pref.masquer;
$("masquer").addEventListener("change", (e) => { pref.masquer = e.target.checked; sauverPreferences(); afficher(); });
$("recherche").addEventListener("input", planifierRendu);
$("contenu").addEventListener("click", (e) => {
  const t = e.target.closest(".tuile");
  if (t) ouvrirDetail(t.dataset.nom);
});
$("fermer").addEventListener("click", () => { $("detail").hidden = true; selection = null; });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { $("detail").hidden = true; selection = null; }
});

function horloge() {
  $("horloge").textContent = new Date().toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
}
horloge();
setInterval(horloge, 10_000);
setInterval(planifierRendu, 30_000); // fait avancer les durées affichées

connecter();
