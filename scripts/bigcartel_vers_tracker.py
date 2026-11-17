#!/usr/bin/env python3
"""Ajoute des commandes Bigcartel dans le tracker BDC (base Firestore).

Utilisé par la tâche planifiée hebdomadaire : l'agent lit les mails
« You've received a new order! » de Bigcartel, les résume dans un fichier
JSON, puis lance ce script qui fait le reste (doublons, sortie, écriture).

Entrée : un fichier JSON = liste de commandes :
  [{"num": "BZDR-518062", "date": "2026-09-25", "pays": "France",
    "fdp_enc": 4.0,
    "items": [{"texte": "Mozdrill \"Demo 2026\"", "qty": 1}],
    "client": {"prenom": "", "nom": "", "email": "", "tel": "",
               "adr1": "", "adr2": "", "ville": "", "cp": ""}}]
  "pays" = le pays tel qu'écrit dans le mail (anglais ou français).

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
    entrees = json.load(open(sys.argv[1], encoding="utf-8"))
    token = login()
    params, _ = lire("params", token)
    params = {k: v for k, v in params.items() if not k.startswith("_") and isinstance(v, dict)}  # ignore _tarifs etc.
    commandes, update_time = lire("commandes", token)

    rapport = {"ajoutees": [], "deja_presentes": [], "non_reconnues": []}
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
        grp = f" [commande groupée GRP-{base_id}]" if groupe else ""
        for rid, it in lignes:
            k += 1
            entry = {
                "id": base_id + k,
                "type": "bigcartel",
                "ref": num,
                "qty": int(it.get("qty") or 1),
                "pays": pays,
                "transporteur": "Mondial Relay",
                "statut": "en_attente",
                "fdpEncOverride": (round(fdp / len(lignes), 2) if fdp is not None else None),
                "fdpPayOverride": None,
                "note": f"{num} // {nom_client}{grp}" if nom_client else f"{num}{grp}",
                "date": o.get("date") or time.strftime("%Y-%m-%d"),
                "client": {key: cl.get(key, "") for key in ["prenom", "nom", "email", "tel", "adr1", "adr2", "ville", "cp"]},
            }
            commandes.setdefault(rid, []).append(entry)
            rapport["ajoutees"].append({"num": num, "sortie": params[rid].get("nom", rid),
                                        "groupe": params[rid].get("groupe"), "qty": entry["qty"], "pays": pays})

    if rapport["ajoutees"] and ecrire:
        # Écrit seulement si personne n'a modifié la base entre-temps (sinon : erreur, rien n'est écrasé)
        url = f"{DOCS}/commandes?currentDocument.updateTime={urllib.parse.quote(update_time)}"
        http("PATCH", url, {"fields": {"data": {"stringValue": json.dumps(commandes, ensure_ascii=False)}}}, token)
    rapport["mode"] = "ECRIT" if (ecrire and rapport["ajoutees"]) else ("SIMULATION" if not ecrire else "RIEN A ECRIRE")
    print(json.dumps(rapport, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
