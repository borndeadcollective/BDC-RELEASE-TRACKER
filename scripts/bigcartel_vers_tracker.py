#!/usr/bin/env python3
"""Ajoute des commandes Bigcartel dans le tracker BDC (base Firestore)
et fait avancer les envois Mondial Relay de l'onglet « À EXPÉDIER ».

Utilisé par la tâche planifiée hebdomadaire : l'agent lit les mails
« You've received a new order! » de Bigcartel et les mails Mondial Relay,
les résume dans un fichier JSON, puis lance ce script qui fait le reste
(doublons, sortie, rapprochement, écriture).

Entrée : un fichier JSON, soit une simple liste de commandes (ancien format),
soit {"commandes": [...], "mondial_relay": [...]}.

Commande :
  {"num": "BZDR-518062", "date": "2026-09-25", "pays": "France",
   "fdp_enc": 4.0,
   "items": [{"texte": "Mozdrill \"Demo 2026\"", "qty": 1}],
   "client": {"prenom": "", "nom": "", "email": "", "tel": "",
              "adr1": "", "adr2": "", "ville": "", "cp": ""}}
  "pays" = le pays tel qu'écrit dans le mail (anglais ou français).

Événements Mondial Relay (un par mail) :
  {"type": "relais_choisi", "date": "2026-10-29", "destinataire": "Ronald Corazza",
   "lien": "https://www.mondialrelay.fr/_mvc/fr-FR/EnvoiColis/Reprendre/?idExpeditionEnAttente=..."}
     mail « Votre destinataire a saisi ses coordonnées - vous pouvez terminer votre envoi »
  {"type": "confirmation", "date": "2026-11-16", "commande": "CV88729219",
   "etiquette": "https://www.mondialrelay.fr/envoi-de-colis/recapitulatif-de-commande/?reference=...",
   "colis": [{"numero": "20671009", "destinataire": "Oliverio Lorenzo", "prix": 6.60,
              "suivi": "https://www.mondialrelay.fr/suivi-de-colis/?numeroExpedition=...", "cp": "37138"}]}
     mail « Confirmation de commande Mondial Relay » (une ligne par colis payé)
  {"type": "depot", "date": "2026-11-18", "colis": "20671009", "destinataire": "..."}
     mail de preuve de dépôt (colis et/ou destinataire, au moins un des deux)
Un événement est rapproché d'une commande par le n° de colis, sinon par le nom
du client (prénom + nom contenus dans le nom du destinataire). Ce qui est
ambigu ou introuvable est seulement signalé, jamais deviné.

Usage :
  ROBOT_PW=... python3 bigcartel_vers_tracker.py commandes.json          # simulation
  ROBOT_PW=... python3 bigcartel_vers_tracker.py commandes.json --ecrire # écrit

Rien d'autre que la base n'est touché. Aucun secret dans ce fichier :
le mot de passe du compte robot arrive par la variable ROBOT_PW.
"""
import json
import os
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

API_KEY = "AIzaSyDCeyG4fRFUL4btxVIxz8LES0dq0rTyDMc"  # clé web publique (pas un secret)
PROJECT = "bdc10-moldy"
ROBOT_EMAIL = "robot@borndeadcollective.com"
DOCS = f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents/bdc"

PAYS_FR = {
    "france": "France", "belgium": "Belgique", "belgique": "Belgique",
    "luxembourg": "Luxembourg", "germany": "Allemagne", "allemagne": "Allemagne",
    "austria": "Autriche", "autriche": "Autriche", "spain": "Espagne", "espagne": "Espagne",
    "italy": "Italie", "italie": "Italie", "netherlands": "Pays-Bas", "the netherlands": "Pays-Bas",
    "pays-bas": "Pays-Bas", "poland": "Pologne", "pologne": "Pologne",
    "sweden": "Suède", "suede": "Suède", "finland": "Finlande", "finlande": "Finlande",
    "denmark": "Danemark", "danemark": "Danemark", "norway": "Norvège", "norvege": "Norvège",
    "portugal": "Portugal", "switzerland": "Suisse", "suisse": "Suisse",
    "united kingdom": "Royaume-Uni", "uk": "Royaume-Uni", "royaume-uni": "Royaume-Uni",
    "ireland": "Irlande", "irlande": "Irlande", "czech republic": "République tchèque",
    "czechia": "République tchèque", "greece": "Grèce", "united states": "États-Unis",
    "usa": "États-Unis", "canada": "Canada",
}


def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return " ".join(s.lower().replace('"', " ").split())


def http(method, url, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"ERREUR HTTP {e.code} sur {method} {url.split('?')[0]} : {e.read().decode()[:300]}")


def login():
    pw = os.environ.get("ROBOT_PW")
    if not pw:
        raise SystemExit("ERREUR : variable ROBOT_PW absente")
    r = http("POST", "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=" + API_KEY,
             {"email": ROBOT_EMAIL, "password": pw, "returnSecureToken": True})
    return r["idToken"]


def lire(nom, token):
    d = http("GET", f"{DOCS}/{nom}", token=token)
    return json.loads(d["fields"]["data"]["stringValue"]), d["updateTime"]


def trouver_release(texte, params):
    t = norm(texte)
    ids = sorted(params, key=lambda i: -int("".join(c for c in i if c.isdigit()) or 0))
    for rid in ids:  # le titre d'abord (un groupe peut avoir plusieurs sorties)
        titre = norm(params[rid].get("titre"))
        if titre and titre in t:
            return rid
    for rid in ids:
        groupe = norm(params[rid].get("groupe"))
        if groupe and groupe in t:
            return rid
    return None


def transporteur_grille(pays, formats, tarifs):
    """Mondial Relay ou La Poste selon la grille du tracker (format le plus lourd)."""
    n = norm(pays)
    t = next((t for t in tarifs or [] if n in (norm(t.get("pays")), norm(t.get("en")))), None)
    if not t:
        return "Mondial Relay"
    tiers = {"TAPE": "100g", "CD": "250g"}
    tier = max((tiers.get(str(f).upper(), "500g") for f in formats), key=["100g", "250g", "500g"].index)
    lbl = str(t.get("transp" + tier) or t.get("transporteur") or "")
    return "La Poste" if any(k in lbl.lower() for k in ("poste", "lettre", "colissimo")) else "Mondial Relay"


# ── Mondial Relay ────────────────────────────────────────────────────
def mots(s):
    return {m for m in norm(s).replace("-", " ").replace(",", " ").split() if len(m) > 1}


def colis_cle(c):
    note = c.get("note") or ""
    i = note.find("GRP-")
    if i >= 0:
        j = i + 4
        while j < len(note) and note[j].isdigit():
            j += 1
        return note[i:j]
    r = str(c.get("ref") or "").strip().lstrip("#").lower()
    return "ref:" + r if r else "id:" + str(c.get("id"))


def colis_en_cours(commandes, params):
    """Commandes Bigcartel regroupées par colis : {cle: [entrées]}."""
    res = {}
    for rid, lst in commandes.items():
        if rid not in params or not isinstance(lst, list):
            continue
        for c in lst:
            if c.get("type") == "bigcartel" and c.get("transporteur") not in ("Main propre", "Autre"):
                res.setdefault(colis_cle(c), []).append(c)
    return res


def nom_client(entrees):
    for c in entrees:
        cl = c.get("client") or {}
        m = mots(f"{cl.get('prenom', '')} {cl.get('nom', '')}")
        if m:
            return m
    note = entrees[0].get("note") or ""
    if "//" in note:
        return mots(note.split("//", 1)[1].split("[")[0])
    return set()


def rapprocher(ev, colis, etape):
    """Clé du colis correspondant à l'événement, ou (None, raison)."""
    num = str(ev.get("colis") or ev.get("numero") or "").strip()
    if num:
        hits = [k for k, es in colis.items() if any(str((c.get("envoi") or {}).get("colis") or "") == num for c in es)]
        if len(hits) == 1:
            return hits[0], None
    dest = mots(ev.get("destinataire"))
    if not dest:
        return None, "ni n° de colis connu ni destinataire"
    def ouvert(es):
        e = {}
        for c in es:
            e.update(c.get("envoi") or {})
        tous_envoyes = all(c.get("statut") == "envoye" for c in es)
        if etape == "relais_choisi":
            return not tous_envoyes and not e.get("colis") and not e.get("paye")
        if etape == "confirmation":
            return not tous_envoyes and not e.get("colis")
        return not e.get("depot")
    hits = [k for k, es in colis.items() if ouvert(es) and nom_client(es) and nom_client(es) <= dest]
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, "aucune commande en cours à ce nom"
    cp = norm(ev.get("cp"))
    if cp:
        fin = [k for k in hits if any(norm((c.get("client") or {}).get("cp")) == cp for c in colis[k])]
        if len(fin) == 1:
            return fin[0], None
    return None, f"{len(hits)} commandes possibles"


def appliquer_mr(evenements, commandes, params, rapport):
    ordre = {"relais_choisi": 0, "confirmation": 1, "depot": 2}
    colis = colis_en_cours(commandes, params)
    change = False
    for ev in sorted(evenements, key=lambda e: (str(e.get("date") or ""), ordre.get(e.get("type"), 9))):
        typ, date = ev.get("type"), (ev.get("date") or time.strftime("%Y-%m-%d"))[:10]
        lignes = ev.get("colis") if typ == "confirmation" else [ev]
        if typ == "confirmation" and not isinstance(lignes, list):
            lignes = [lignes]
        if typ not in ordre:
            rapport["mr_non_rapproches"].append({"type": typ, "raison": "type inconnu"})
            continue
        for ln in lignes or []:
            if typ == "confirmation":
                sub = {"colis": ln.get("numero"), "destinataire": ln.get("destinataire"), "cp": ln.get("cp")}
                k, raison = rapprocher(sub, colis, typ)
            else:
                sub = ln
                k, raison = rapprocher(ln, colis, typ)
            qui = sub.get("destinataire") or sub.get("colis") or "?"
            if not k:
                rapport["mr_non_rapproches"].append({"type": typ, "date": date, "qui": qui, "raison": raison})
                continue
            es = colis[k]
            ref = es[0].get("ref") or k
            if typ == "relais_choisi":
                patch = {"relaisChoisi": date}
                if ev.get("lien"):
                    patch["mrLien"] = ev["lien"]
                for c in es:
                    e = c.setdefault("envoi", {})
                    e.setdefault("relaisDemande", e.get("relaisDemande") or date)
                    e.update(patch)
            elif typ == "confirmation":
                prix = ln.get("prix")
                patch = {"colis": str(ln.get("numero") or ""), "paye": date}
                for key, val in (("suivi", ln.get("suivi")), ("mrCommande", ev.get("commande")), ("etiquette", ev.get("etiquette"))):
                    if val:
                        patch[key] = val
                if prix is not None:
                    patch["coutReel"] = round(float(prix), 2)
                part = round(float(prix) / len(es), 2) if prix is not None else None
                for c in es:
                    c.setdefault("envoi", {}).update({k2: v for k2, v in patch.items() if v != ""})
                    if part is not None:
                        c["fdpPayOverride"] = part
                        c["fdpPayReel"] = True
            else:  # depot
                for c in es:
                    e = c.setdefault("envoi", {})
                    e["depot"] = date
                    e.setdefault("envoye", date)
                    c["statut"] = "envoye"
            change = True
            rapport["mr_appliques"].append({"type": typ, "commande": ref, "qui": qui})
    return change


def deja_la(num, commandes):
    n = num.lstrip("#").lower()
    for lst in commandes.values():
        for c in lst if isinstance(lst, list) else []:
            if n in (c.get("note") or "").lower() or n in (c.get("ref") or "").lower():
                return True
    return False


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    ecrire = "--ecrire" in sys.argv
    donnees = json.load(open(sys.argv[1], encoding="utf-8"))
    if isinstance(donnees, list):
        entrees, evenements = donnees, []
    else:
        entrees, evenements = donnees.get("commandes") or [], donnees.get("mondial_relay") or []
    token = login()
    params_bruts, _ = lire("params", token)
    tarifs = params_bruts.get("_tarifs") or []
    params = {k: v for k, v in params_bruts.items() if not k.startswith("_") and isinstance(v, dict)}  # ignore _tarifs etc.
    commandes, update_time = lire("commandes", token)

    rapport = {"ajoutees": [], "deja_presentes": [], "non_reconnues": [], "mr_appliques": [], "mr_non_rapproches": []}
    aujourdhui = time.strftime("%Y-%m-%d")
    base_id = int(time.time() * 1000)
    k = 0
    for o in entrees:
        num = "#" + str(o["num"]).lstrip("#").strip()
        if deja_la(num, commandes):
            rapport["deja_presentes"].append(num)
            continue
        lignes, inconnus = [], []
        for it in o.get("items", []):
            rid = trouver_release(it.get("texte", ""), params)
            (lignes if rid else inconnus).append((rid, it))
        if inconnus or not lignes:
            rapport["non_reconnues"].append({"num": num, "articles": [i.get("texte") for _, i in inconnus] or "aucun article"})
            continue
        cl = o.get("client") or {}
        nom_client = " ".join(x for x in [cl.get("prenom"), cl.get("nom")] if x).strip()
        pays = PAYS_FR.get(norm(o.get("pays")), o.get("pays") or "")
        fdp = o.get("fdp_enc")
        groupe = len(lignes) > 1
        grp = f" [commande groupée GRP-{base_id + k + 1}]" if groupe else ""
        transporteur = transporteur_grille(pays, [params[rid].get("format", "LP") for rid, _ in lignes], tarifs)
        for rid, it in lignes:
            k += 1
            entry = {
                "id": base_id + k,
                "type": "bigcartel",
                "ref": num,
                "qty": int(it.get("qty") or 1),
                "pays": pays,
                "transporteur": transporteur,
                "statut": "en_attente",
                "fdpEncOverride": (round(fdp / len(lignes), 2) if fdp is not None else None),
                "fdpPayOverride": None,
                "note": f"{num} // {nom_client}{grp}" if nom_client else f"{num}{grp}",
                "date": o.get("date") or time.strftime("%Y-%m-%d"),
                "client": {key: cl.get(key, "") for key in ["prenom", "nom", "email", "tel", "adr1", "adr2", "ville", "cp"]},
                "importeLe": aujourdhui,
            }
            commandes.setdefault(rid, []).append(entry)
            rapport["ajoutees"].append({"num": num, "sortie": params[rid].get("nom", rid),
                                        "groupe": params[rid].get("groupe"), "qty": entry["qty"], "pays": pays})

    mr_change = appliquer_mr(evenements, commandes, params, rapport) if evenements else False
    a_ecrire = bool(rapport["ajoutees"]) or mr_change

    if a_ecrire and ecrire:
        # Écrit seulement si personne n'a modifié la base entre-temps (sinon : erreur, rien n'est écrasé)
        url = f"{DOCS}/commandes?currentDocument.updateTime={urllib.parse.quote(update_time)}"
        http("PATCH", url, {"fields": {"data": {"stringValue": json.dumps(commandes, ensure_ascii=False)}}}, token)
    rapport["mode"] = "ECRIT" if (ecrire and a_ecrire) else ("SIMULATION" if not ecrire else "RIEN A ECRIRE")
    print(json.dumps(rapport, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
