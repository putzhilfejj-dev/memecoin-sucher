"""
Memecoin-Sucher  (Version 1)
============================

Findet frisch gelistete / gerade beworbene Memecoins auf Solana,
filtert offensichtlichen Müll heraus, bewertet den Rest mit einem
Punktesystem (0-100) und prüft die besten Kandidaten bei RugCheck.

Datenquellen (kostenlos, kein API-Key nötig):
  - DexScreener  https://api.dexscreener.com
  - RugCheck     https://api.rugcheck.xyz

Start (Eingabeaufforderung / Terminal im Ordner dieser Datei):
    python memecoin_sucher.py                  # einmal suchen
    python memecoin_sucher.py --watch 5        # alle 5 Minuten neu suchen
    python memecoin_sucher.py --min-liq 20000 --max-alter 24

Benötigt nur Python 3.9+ - keine Zusatzpakete.

WICHTIG: Das ist ein Recherche-Werkzeug, keine Kaufempfehlung.
Memecoins sind extrem riskant, die meisten gehen auf null.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

DEX = "https://api.dexscreener.com"
RUG = "https://api.rugcheck.xyz/v1"
CHAIN = "solana"
HEADERS = {"User-Agent": "memecoin-sucher/1.0", "Accept": "application/json"}

# Feeds, aus denen neue Kandidaten kommen
FEEDS = {
    "profil": "/token-profiles/latest/v1",          # neue Token-Seiten
    "boost": "/token-boosts/latest/v1",             # gerade bezahlte Boosts
    "top-boost": "/token-boosts/top/v1",            # meiste aktive Boosts
    "takeover": "/community-takeovers/latest/v1",   # Community-Übernahmen
}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def hole_json(url: str, versuche: int = 3):
    """GET-Anfrage, gibt JSON zurück oder None bei Fehler."""
    for i in range(versuche):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:  # zu viele Anfragen -> kurz warten
                time.sleep(2 * (i + 1))
                continue
            if e.code == 404:
                return None
            time.sleep(1)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            time.sleep(1)
    return None


# --------------------------------------------------------------------------
# 1) Kandidaten sammeln
# --------------------------------------------------------------------------
def sammle_kandidaten() -> dict[str, dict]:
    """Adresse -> {quellen, links} für alle Solana-Tokens aus den Feeds."""
    kandidaten: dict[str, dict] = {}
    for name, pfad in FEEDS.items():
        daten = hole_json(DEX + pfad)
        if isinstance(daten, dict):  # manche Antworten sind verpackt
            daten = daten.get("data") or daten.get("boosts") or []
        for eintrag in daten or []:
            if not isinstance(eintrag, dict) or eintrag.get("chainId") != CHAIN:
                continue
            adr = eintrag.get("tokenAddress")
            if not adr:
                continue
            k = kandidaten.setdefault(adr, {"quellen": set(), "links": 0})
            k["quellen"].add(name)
            k["links"] = max(k["links"], len(eintrag.get("links") or []))
    return kandidaten


# --------------------------------------------------------------------------
# 2) Marktdaten holen (max. 30 Adressen pro Anfrage)
# --------------------------------------------------------------------------
def hole_marktdaten(adressen: list[str]) -> dict[str, dict]:
    """Adresse -> beste Handelspaar-Daten (das mit der meisten Liquidität)."""
    beste: dict[str, dict] = {}
    for i in range(0, len(adressen), 30):
        block = ",".join(adressen[i:i + 30])
        paare = hole_json(f"{DEX}/tokens/v1/{CHAIN}/{block}") or []
        if isinstance(paare, dict):
            paare = paare.get("pairs") or []
        for p in paare:
            adr = (p.get("baseToken") or {}).get("address")
            if not adr:
                continue
            liq = (p.get("liquidity") or {}).get("usd") or 0
            alt = beste.get(adr)
            if alt is None or liq > ((alt.get("liquidity") or {}).get("usd") or 0):
                beste[adr] = p
    return beste


# --------------------------------------------------------------------------
# 3) Filtern und bewerten
# --------------------------------------------------------------------------
def kennzahlen(p: dict, jetzt_ms: int) -> dict:
    tx_h1 = (p.get("txns") or {}).get("h1") or {}
    kaeufe, verkaeufe = tx_h1.get("buys", 0) or 0, tx_h1.get("sells", 0) or 0
    erstellt = p.get("pairCreatedAt") or jetzt_ms
    return {
        "symbol": (p.get("baseToken") or {}).get("symbol", "?"),
        "name": (p.get("baseToken") or {}).get("name", "?"),
        "preis": float(p.get("priceUsd") or 0),
        "liq": (p.get("liquidity") or {}).get("usd") or 0,
        "mcap": p.get("marketCap") or p.get("fdv") or 0,
        "vol24": (p.get("volume") or {}).get("h24") or 0,
        "vol1": (p.get("volume") or {}).get("h1") or 0,
        "chg1": (p.get("priceChange") or {}).get("h1") or 0,
        "chg24": (p.get("priceChange") or {}).get("h24") or 0,
        "kaeufe": kaeufe,
        "verkaeufe": verkaeufe,
        "alter_h": max(0.0, (jetzt_ms - erstellt) / 3_600_000),
        "url": p.get("url", ""),
    }


def besteht_filter(k: dict, a: argparse.Namespace) -> bool:
    return (
        k["liq"] >= a.min_liq
        and k["vol24"] >= a.min_vol
        and k["alter_h"] <= a.max_alter
        and (k["kaeufe"] + k["verkaeufe"]) >= a.min_tx
        and (a.max_mcap <= 0 or k["mcap"] <= a.max_mcap)
    )


def punkte(k: dict, quellen: set, links: int, max_alter: float) -> float:
    """Punktesystem 0-100 (ohne Rug-Abzug)."""
    s = 0.0
    # Kaufdruck letzte Stunde (25)
    tx = k["kaeufe"] + k["verkaeufe"]
    if tx:
        s += 25 * min(1.0, max(0.0, (k["kaeufe"] / tx - 0.4) / 0.3))
    # Umsatz im Verhältnis zur Liquidität (25) - ab 5x voll
    if k["liq"]:
        s += 25 * min(1.0, (k["vol24"] / k["liq"]) / 5)
    # Momentum letzte Stunde (15) - +0 % .. +50 %
    s += 15 * min(1.0, max(0.0, k["chg1"] / 50))
    # Jung = früh (15)
    s += 15 * max(0.0, 1 - k["alter_h"] / max_alter)
    # Socials / Profil vorhanden (10)
    s += 10 * min(1.0, links / 3)
    # In mehreren Feeds gleichzeitig (10)
    s += 10 * min(1.0, (len(quellen) - 1) / 2)
    return s


# --------------------------------------------------------------------------
# 4) RugCheck
# --------------------------------------------------------------------------
def rugcheck(adresse: str) -> dict:
    """Gibt {risiko 0-100, gefahren [...], lp_lock %} zurück (oder leere Werte)."""
    d = hole_json(f"{RUG}/tokens/{adresse}/report/summary")
    if not isinstance(d, dict):
        return {"risiko": None, "gefahren": [], "lp_lock": None}
    gefahren = [
        r.get("name", "?")
        for r in d.get("risks") or []
        if str(r.get("level", "")).lower() == "danger"
    ]
    return {
        "risiko": d.get("score_normalised"),
        "gefahren": gefahren,
        "lp_lock": d.get("lpLockedPct"),
    }


def rug_abzug(r: dict) -> float:
    abzug = 0.0
    if r["risiko"] is not None:
        abzug += 0.5 * float(r["risiko"])  # Risiko 100 -> -50 Punkte
    abzug += 15 * len(r["gefahren"])
    return abzug


# --------------------------------------------------------------------------
# Handy-Benachrichtigung über ntfy (kostenlos, ohne Konto)
# --------------------------------------------------------------------------
NTFY = "https://ntfy.sh/"
GEMELDET_DATEI = "bereits_gemeldet.json"


FOMO = "https://fomo.family/prices/"


def sende_push(thema: str, titel: str, text: str, link: str = "", prio: int = 4,
               knoepfe: list | None = None, tags: list | None = None) -> bool:
    """Schickt eine Push-Nachricht an alle Handys, die das ntfy-Thema abonniert haben."""
    daten = {"topic": thema, "title": titel, "message": text,
             "priority": prio, "tags": tags or ["rocket"]}
    if link:
        daten["click"] = link  # Antippen der Nachricht öffnet diesen Link
    if knoepfe:
        daten["actions"] = [{"action": "view", "label": l, "url": u} for l, u in knoepfe]
    try:
        req = urllib.request.Request(
            NTFY, data=json.dumps(daten).encode("utf-8"),
            headers={**HEADERS, "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15):
            return True
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"  Push fehlgeschlagen: {e}")
        return False


PULS_MINUTEN = 12  # so lange gilt der Laptop nach dem letzten Lebenszeichen als aktiv


def ntfy_verlauf(thema: str, seit: str) -> list[dict]:
    """Liest die Nachrichten eines Themas aus dem ntfy-Zwischenspeicher (max. 12 h)."""
    try:
        req = urllib.request.Request(f"{NTFY}{thema}/json?poll=1&since={seit}", headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as r:
            zeilen = r.read().decode("utf-8").splitlines()
        return [m for m in (json.loads(z) for z in zeilen if z.strip())
                if m.get("event") == "message"]
    except (urllib.error.URLError, TimeoutError, ValueError):
        return []


def sende_puls(thema: str) -> None:
    """Unsichtbares Lebenszeichen vom Laptop (eigenes Thema, das Handy abonniert es nicht)."""
    try:
        req = urllib.request.Request(
            NTFY, data=json.dumps({"topic": thema + "-puls", "message": "laptop aktiv",
                                   "priority": 1}).encode("utf-8"),
            headers={**HEADERS, "Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=15).close()
    except (urllib.error.URLError, TimeoutError):
        pass


def laptop_aktiv(thema: str) -> bool:
    return bool(ntfy_verlauf(thema + "-puls", f"{PULS_MINUTEN}m"))


def lade_gemeldet(thema: str = "") -> set:
    """Bereits gemeldete Coins: lokale Datei + alles, was in den letzten 12 h gepusht wurde
    (so meldet GitHub nichts doppelt, was der Laptop schon geschickt hat - und umgekehrt)."""
    gemeldet: set = set()
    try:
        with open(GEMELDET_DATEI, encoding="utf-8") as f:
            gemeldet = set(json.load(f))
    except (OSError, ValueError):
        pass
    if thema:
        for m in ntfy_verlauf(thema, "12h"):
            for zeile in str(m.get("message", "")).splitlines():
                if zeile.startswith("Adresse: "):
                    gemeldet.add(zeile[9:].strip())
    return gemeldet


def speichere_gemeldet(adressen: set) -> None:
    with open(GEMELDET_DATEI, "w", encoding="utf-8") as f:
        json.dump(sorted(adressen), f)


def melde_treffer(ergebnisse: list[dict], a: argparse.Namespace) -> None:
    """Push nur für NEUE Coins mit genug Punkten und ohne RugCheck-Gefahren."""
    if not a.ntfy:
        return
    gemeldet = lade_gemeldet(a.ntfy)
    neu = 0
    for e in ergebnisse:
        if e["adresse"] in gemeldet or e["score"] < a.alarm_ab or e["rug"]["gefahren"]:
            continue
        k, r = e["k"], e["rug"]
        rug = "nicht geprüft" if r["risiko"] is None else f"{int(r['risiko'])}/100"
        text = (f"{k['name']} - {e['score']:.0f} Punkte\n"
                f"Alter {k['alter_h']:.0f}h | Liq {geld(k['liq'])} | MCap {geld(k['mcap'])}\n"
                f"1h {k['chg1']:+.0f}% | Käufe/Verkäufe {k['kaeufe']}/{k['verkaeufe']}\n"
                f"Rug-Risiko {rug}\n"
                f"Adresse: {e['adresse']}")
        fomo = FOMO + e["adresse"]
        bester = e is ergebnisse[0]  # höchste Punktzahl in diesem Durchlauf
        titel = (f"⭐ BESTER COIN: {k['symbol']}" if bester
                 else f"Neuer Memecoin: {k['symbol']}")
        if sende_push(a.ntfy, titel, text, fomo, prio=5 if bester else 4,
                      knoepfe=[("FOMO", fomo), ("DexScreener", k["url"])],
                      tags=["star", "rocket"] if bester else ["rocket"]):
            gemeldet.add(e["adresse"])
            neu += 1
    speichere_gemeldet(gemeldet)
    if neu:
        print(f"  {neu} Push-Nachricht(en) ans Handy geschickt")


TOP10_MINUTEN = 30  # gleiche Nr. 1 -> Rangliste höchstens alle 30 Minuten neu schicken


def sende_top10(ergebnisse: list[dict], a: argparse.Namespace) -> None:
    """Rangliste 1-10 in ein eigenes ntfy-Thema (= eigene Liste in der App).
    Coins mit RugCheck-Gefahren kommen nicht auf die Liste."""
    if not a.ntfy:
        return
    sauber = [e for e in ergebnisse if not e["rug"]["gefahren"]][:10]
    if not sauber:
        return
    thema = a.ntfy + "-top10"
    nr1 = sauber[0]["adresse"]
    # Nur neu schicken, wenn sich die Nr. 1 geändert hat oder 30 Min. vergangen sind
    for m in ntfy_verlauf(thema, f"{TOP10_MINUTEN}m"):
        if f"Adresse Nr. 1: {nr1}" in str(m.get("message", "")):
            print("  Top 10: Nr. 1 unverändert, keine neue Liste")
            return

    marken = ["🥇", "🥈", "🥉"]
    zeilen = []
    for i, e in enumerate(sauber, 1):
        k, r = e["k"], e["rug"]
        rug = "?" if r["risiko"] is None else str(int(r["risiko"]))
        marke = marken[i - 1] if i <= 3 else f"{i}."
        zeilen.append(f"{marke} {k['symbol'][:12]}  {e['score']:.0f} P | MCap {geld(k['mcap'])}"
                      f" | 1h {k['chg1']:+.0f}% | Rug {rug}")
    zeilen += ["", f"Adresse Nr. 1: {nr1}", "Nur Recherche, keine Kaufempfehlung."]

    knoepfe = [(f"{i}. {e['k']['symbol'][:10]}", FOMO + e["adresse"])
               for i, e in enumerate(sauber[:3], 1)]  # ntfy erlaubt max. 3 Knöpfe
    zeit = datetime.now().strftime("%H:%M")
    if sende_push(thema, f"🏆 Top 10 ({zeit}) – Nr. 1: {sauber[0]['k']['symbol']}",
                  "\n".join(zeilen), FOMO + nr1, prio=2, knoepfe=knoepfe, tags=["trophy"]):
        print(f"  Top-10-Liste ans Handy geschickt (Thema '{thema}')")


# --------------------------------------------------------------------------
# Ausgabe
# --------------------------------------------------------------------------
def geld(x: float) -> str:
    if x >= 1e9:
        return f"${x/1e9:.1f}B"
    if x >= 1e6:
        return f"${x/1e6:.1f}M"
    if x >= 1e3:
        return f"${x/1e3:.0f}k"
    return f"${x:.0f}"


def zeige(ergebnisse: list[dict]) -> None:
    if not ergebnisse:
        print("Keine Kandidaten haben die Filter bestanden. "
              "Versuch es gleich nochmal oder lockere die Filter.")
        return
    kopf = f"{'#':>2}  {'Token':<12}{'Punkte':>7}{'Alter':>7}{'Liq':>8}{'MCap':>8}" \
           f"{'Vol24':>8}{'1h %':>7}{'K/V 1h':>10}{'Rug':>5}  Warnungen"
    print(kopf)
    print("-" * len(kopf) + "-" * 20)
    for i, e in enumerate(ergebnisse, 1):
        k, r = e["k"], e["rug"]
        rug = "-" if r["risiko"] is None else str(int(r["risiko"]))
        warn = ", ".join(r["gefahren"])[:40] or ("ok" if r["risiko"] is not None else "nicht geprüft")
        print(f"{i:>2}  {k['symbol'][:11]:<12}{e['score']:>7.0f}{k['alter_h']:>6.0f}h"
              f"{geld(k['liq']):>8}{geld(k['mcap']):>8}{geld(k['vol24']):>8}"
              f"{k['chg1']:>+7.0f}{str(k['kaeufe'])+'/'+str(k['verkaeufe']):>10}{rug:>5}  {warn}")
    print("\nLinks:")
    for i, e in enumerate(ergebnisse, 1):
        print(f"{i:>2}  {e['k']['symbol'][:11]:<12}{e['k']['url']}")


def speichere_csv(ergebnisse: list[dict], datei: str) -> None:
    neu = not os.path.exists(datei)
    with open(datei, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        if neu:
            w.writerow(["zeit", "symbol", "name", "adresse", "punkte", "alter_h", "preis_usd",
                        "liquiditaet", "marketcap", "vol24", "chg1h", "chg24h",
                        "kaeufe_1h", "verkaeufe_1h", "rug_risiko", "rug_gefahren",
                        "lp_lock", "quellen", "url"])
        zeit = datetime.now().strftime("%Y-%m-%d %H:%M")
        for e in ergebnisse:
            k, r = e["k"], e["rug"]
            w.writerow([zeit, k["symbol"], k["name"], e["adresse"], round(e["score"]),
                        round(k["alter_h"], 1), k["preis"], round(k["liq"]), round(k["mcap"]),
                        round(k["vol24"]), k["chg1"], k["chg24"], k["kaeufe"], k["verkaeufe"],
                        r["risiko"], "|".join(r["gefahren"]), r["lp_lock"],
                        "|".join(sorted(e["quellen"])), k["url"]])


# --------------------------------------------------------------------------
# Hauptablauf
# --------------------------------------------------------------------------
def ein_durchlauf(a: argparse.Namespace) -> list[dict]:
    print(f"\n[{datetime.now():%H:%M:%S}] Suche neue {CHAIN.capitalize()}-Memecoins ...")
    kandidaten = sammle_kandidaten()
    print(f"  {len(kandidaten)} Tokens in den DexScreener-Feeds gefunden")
    if not kandidaten:
        print("  (Keine Daten - Internetverbindung prüfen.)")
        return []

    markt = hole_marktdaten(list(kandidaten))
    jetzt = int(time.time() * 1000)

    vorauswahl = []
    for adr, p in markt.items():
        k = kennzahlen(p, jetzt)
        if not besteht_filter(k, a):
            continue
        info = kandidaten[adr]
        vorauswahl.append({
            "adresse": adr, "k": k, "quellen": info["quellen"],
            "score": punkte(k, info["quellen"], info["links"], a.max_alter),
        })
    print(f"  {len(vorauswahl)} davon bestehen die Filter")

    vorauswahl.sort(key=lambda e: e["score"], reverse=True)
    top = vorauswahl[: a.top]

    if not a.ohne_rugcheck and top:
        print(f"  Prüfe die besten {len(top)} bei RugCheck ...")
    for e in top:
        e["rug"] = {"risiko": None, "gefahren": [], "lp_lock": None}
        if not a.ohne_rugcheck:
            e["rug"] = rugcheck(e["adresse"])
            e["score"] = max(0.0, e["score"] - rug_abzug(e["rug"]))
            time.sleep(0.3)

    top.sort(key=lambda e: e["score"], reverse=True)
    print()
    zeige(top)
    if top:
        speichere_csv(top, a.csv)
        print(f"\nGespeichert in {os.path.abspath(a.csv)}")
        melde_treffer(top, a)
        sende_top10(top, a)
    return top


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Findet frühe Solana-Memecoins (DexScreener + RugCheck).")
    ap.add_argument("--min-liq", type=float, default=10_000, help="Mindest-Liquidität in $ (Standard 10000)")
    ap.add_argument("--min-vol", type=float, default=20_000, help="Mindest-Umsatz 24h in $ (Standard 20000)")
    ap.add_argument("--max-alter", type=float, default=72, help="Höchstalter des Paars in Stunden (Standard 72)")
    ap.add_argument("--min-tx", type=int, default=30, help="Mindest-Transaktionen letzte Stunde (Standard 30)")
    ap.add_argument("--max-mcap", type=float, default=0, help="Höchste Marketcap in $ (0 = egal)")
    ap.add_argument("--top", type=int, default=15, help="Wie viele Treffer anzeigen (Standard 15)")
    ap.add_argument("--watch", type=float, default=0, help="Alle X Minuten wiederholen (0 = nur einmal)")
    ap.add_argument("--csv", default="memecoin_treffer.csv", help="CSV-Datei für die Treffer")
    ap.add_argument("--ohne-rugcheck", action="store_true", help="RugCheck überspringen (schneller)")
    ap.add_argument("--ntfy", default="", help="ntfy-Thema für Handy-Pushs (z. B. memecoins-x7k2p9)")
    ap.add_argument("--alarm-ab", type=float, default=60, help="Push erst ab so vielen Punkten (Standard 60)")
    ap.add_argument("--test-push", action="store_true", help="Nur eine Test-Nachricht ans Handy schicken")
    ap.add_argument("--cloud", action="store_true",
                    help="Cloud-Modus (GitHub): nur suchen, wenn der Laptop gerade NICHT läuft")
    a = ap.parse_args(argv)
    a.ntfy = a.ntfy or os.environ.get("NTFY_THEMA", "")

    if a.cloud:
        if not a.ntfy:
            print("Cloud-Modus braucht ein ntfy-Thema (--ntfy oder NTFY_THEMA).")
            return
        if laptop_aktiv(a.ntfy):
            print(f"Laptop war in den letzten {PULS_MINUTEN} Minuten aktiv - "
                  "er übernimmt die Suche, die Cloud pausiert.")
            return
        print("Laptop ist aus - die Cloud sucht.")
        a.watch = 0

    if a.test_push:
        if not a.ntfy:
            print("Bitte mit --ntfy DEIN-THEMA angeben.")
        elif sende_push(a.ntfy, "Memecoin-Sucher",
                        "Test erfolgreich - Pushs kommen an!\nAntippen öffnet BONK bei FOMO.",
                        FOMO + "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", prio=3,
                        knoepfe=[("FOMO", FOMO + "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263")]):
            print("Test-Nachricht verschickt. Ist sie auf dem Handy angekommen?")
        return

    print("Memecoin-Sucher - nur Recherche, keine Kaufempfehlung. DYOR!")
    if a.ntfy:
        print(f"Handy-Pushs aktiv (Thema '{a.ntfy}', ab {a.alarm_ab:g} Punkten)")
        print(f"Top-10-Rangliste im Thema '{a.ntfy}-top10'")
    try:
        while True:
            if a.ntfy and not a.cloud and a.watch > 0:
                sende_puls(a.ntfy)  # sagt der Cloud: "Laptop läuft, du kannst pausieren"
            ein_durchlauf(a)
            if a.watch <= 0:
                break
            print(f"\nNächste Suche in {a.watch:g} Minuten (Strg+C zum Beenden) ...")
            time.sleep(a.watch * 60)
    except KeyboardInterrupt:
        print("\nBeendet.")

    if os.name == "nt" and argv is None and len(sys.argv) == 1:
        # Bei Doppelklick unter Windows: Fenster nicht sofort schließen
        input("\nEnter drücken zum Schließen ...")


if __name__ == "__main__":
    main()
