"use strict";

const ETATS = [
  ["utilise", "Utilisés", "Utilisé"],
  ["verrouille", "Verrouillés", "Verrouillé"],
  ["deconnecte", "Déconnectés", "Session déconnectée"],
  ["libre", "Libres", "Libre"],
  ["erreur", "Erreurs WinRM", "Erreur WinRM"],
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
const trier = (a, b) => a.localeCompare(b, "fr", { numeric: true, sensitivity: "base" });

function planifierRendu() {
  if (!rendu) rendu = requestAnimationFrame(() => { rendu = 0; afficher(); });
}

function filtres() {
  const q = $("recherche").value.trim().toLowerCase();
  return (p) =>
    pref.etats.includes(p.etat) &&
    !(pref.masquer && p.etat === "hors_ligne") &&
    (!pref.groupe || classe(p) === pref.groupe) &&
    (!q || [p.nom, p.utilisateur, p.nom_complet, p.service, p.ip, p.groupe]
      .some((v) => v && String(v).toLowerCase().includes(q)));
}

const classe = (p) => p.groupe || "Sans classe";

function compter(liste) {
  const n = Object.fromEntries(ETATS.map(([e]) => [e, 0]));
  for (const p of liste) n[p.etat] = (n[p.etat] || 0) + 1;
  return n;
}

function jauge(n) {
  return ETATS.filter(([e]) => n[e]).map(([e]) =>
    `<span class="c-${e}" style="flex-grow:${n[e]}"></span>`).join("");
}

function afficher() {
  const tous = [...postes.values()];
  afficherSynthese(tous);
  afficherClasses(tous);

  const contenu = $("contenu");
  if (!tous.length) {
    contenu.innerHTML = '<p class="vide">En attente des premières données du collecteur…</p>';
    return;
  }
  const garder = filtres();
  const parClasse = new Map();
  for (const p of tous) {
    const c = classe(p);
    if (!parClasse.has(c)) parClasse.set(c, { tous: [], visibles: [] });
    const g = parClasse.get(c);
    g.tous.push(p);
    if (garder(p)) g.visibles.push(p);
  }
  const maintenant = Date.now() / 1000;
  const cartes = [...parClasse.keys()].sort(trier)
    .filter((c) => parClasse.get(c).visibles.length)
    .map((c) => carte(c, parClasse.get(c), maintenant));
  contenu.innerHTML = cartes.length ? cartes.join("")
    : '<p class="vide">Aucun poste ne correspond aux filtres.</p>';
}

function carte(nom, { tous, visibles }, maintenant) {
  const n = compter(tous);
  const allumes = tous.length - n.hors_ligne;
  return `<section class="classe">
    <header class="classe-tete">
      <h2>${esc(nom)}</h2>
      <span class="occupation"><b>${n.utilise}</b> utilisé${n.utilise > 1 ? "s" : ""}${n.verrouille ? ` · ${n.verrouille} verrouillé${n.verrouille > 1 ? "s" : ""}` : ""} · ${allumes} allumé${allumes > 1 ? "s" : ""} sur ${tous.length}</span>
      <div class="jauge" aria-hidden="true">${jauge(n)}</div>
    </header>
    <div class="postes">${visibles.sort((a, b) => trier(a.nom, b.nom)).map((p) => tuile(p, maintenant)).join("")}</div>
  </section>`;
}

function prenomNom(p) {
  if (p.nom_complet) return p.nom_complet;
  return login(p.utilisateur);
}

function tuile(p, maintenant) {
  const qui = p.utilisateur ? prenomNom(p) : LIBELLE[p.etat];
  let sous = p.etat === "hors_ligne" ? "" : duree(p.depuis);
  if (p.etat === "utilise" && p.inactif_min >= 5) sous = `inactif ${p.inactif_min} min`;
  const classes = ["poste", p.etat, `c-${p.etat}`];
  if (p.perime) classes.push("perime");
  if (maintenant - p.depuis < 10) classes.push("recent");
  const titre = [p.nom, LIBELLE[p.etat], p.nom_complet, p.utilisateur, p.service,
    p.perime ? "Pas de nouvelles du collecteur" : ""].filter(Boolean).join(" · ");
  return `<button class="${classes.join(" ")}" data-nom="${esc(p.nom)}" title="${esc(titre)}">
    <span class="poste-nom"><span>${esc(p.nom)}</span></span>
    <span class="poste-qui">${esc(qui)}</span>
    ${sous ? `<span class="poste-duree">${esc(sous)}</span>` : ""}
  </button>`;
}

function afficherSynthese(tous) {
  const n = compter(tous);
  const allumes = tous.length - n.hors_ligne;
  $("taux").textContent = allumes ? `${Math.round((100 * n.utilise) / allumes)} %` : "–";
  $("taux-detail").textContent = `d'occupation · ${allumes} postes allumés sur ${tous.length}`;
  $("parc-barre").innerHTML = jauge(n);
  $("compteurs").innerHTML = ETATS.map(([e, libelle]) =>
    `<button class="filtre c-${e}" data-etat="${e}" aria-pressed="${pref.etats.includes(e)}"
      title="Afficher seulement : ${esc(libelle.toLowerCase())}"><strong>${n[e]}</strong><span>${libelle}</span></button>`).join("");
}

function afficherClasses(tous) {
  const liste = [...new Set(tous.map(classe))].sort(trier);
  const sel = $("groupe");
  const cle = liste.join("|");
  if (sel.dataset.cle === cle) return;
  sel.dataset.cle = cle;
  sel.innerHTML = `<option value="">Toutes les classes</option>` +
    liste.map((g) => `<option${g === pref.groupe ? " selected" : ""}>${esc(g)}</option>`).join("");
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
      `<li class="c-${esc(e.etat)}"><time>${heure(e.ts)}</time>
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
    ["Classe", p.groupe],
    ["Depuis", `${heure(p.depuis)} (${duree(p.depuis)})`],
    ["Inactivité", p.inactif_min != null ? `${p.inactif_min} min` : null],
    ["Adresse IP", p.ip],
    ["Démarré le", p.demarrage],
    ["Dernier relevé", heure(p.vu_le) + (p.perime ? " (ancien)" : "")],
    ["OU", p.ou],
    ["Erreur", p.erreur],
  ].filter(([, v]) => v);
  const histo = $("historique");
  $("detail-corps").innerHTML = `
    <h2>${esc(p.nom)}</h2>
    <span class="etiquette c-${p.etat}">${esc(LIBELLE[p.etat])}</span>
    ${p.utilisateur ? `<p class="personne">${esc(prenomNom(p))}
      <small>${esc(p.utilisateur)}${p.service ? " · " + esc(p.service) : ""}</small></p>` : ""}
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
  const t = e.target.closest(".poste");
  if (t) ouvrirDetail(t.dataset.nom);
});
$("fermer").addEventListener("click", () => { $("detail").hidden = true; selection = null; });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { $("detail").hidden = true; selection = null; }
});

$("theme").addEventListener("click", () => {
  const sombre = getComputedStyle(document.documentElement).colorScheme === "dark";
  const t = sombre ? "light" : "dark";
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("supervision-theme", t); } catch { /* sans importance */ }
});

function horloge() {
  $("horloge").textContent = new Date().toLocaleString("fr-FR", {
    weekday: "long", day: "numeric", month: "long", hour: "2-digit", minute: "2-digit",
  });
}
horloge();
setInterval(horloge, 10_000);
setInterval(planifierRendu, 30_000); // fait avancer les durées affichées

connecter();
