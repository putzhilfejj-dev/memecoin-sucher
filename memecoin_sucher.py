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


GECKO = "https://api.geckoterminal.com/api/v2/networks/solana"
GECKO_FEEDS = {
    "gecko-trend": ["/trending_pools?page=1", "/trending_pools?page=2"],
    "gecko-neu": ["/new_pools?page=1", "/new_pools?page=2"],
}


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
    # GeckoTerminal: Trend- und Neu-Listen (findet auch Coins ohne DexScreener-Profil)
    for name, pfade in GECKO_FEEDS.items():
        for pfad in pfade:
            d = hole_json(GECKO + pfad)
            for pool in (d or {}).get("data", []) if isinstance(d, dict) else []:
                tid = (((pool.get("relationships") or {}).get("base_token") or {})
                       .get("data") or {}).get("id", "")
                if not tid.startswith("solana_"):
                    continue
                adr = tid[7:]
                if adr in STABIL:
                    continue
                k = kandidaten.setdefault(adr, {"quellen": set(), "links": 0})
                k["quellen"].add(name)
    return kandidaten


# Keine Memecoins - nie melden
STABIL = {
    "So11111111111111111111111111111111111111112",   # SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


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
    tx_m5 = (p.get("txns") or {}).get("m5") or {}
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
        "chg5": (p.get("priceChange") or {}).get("m5") or 0,
        "vol5": (p.get("volume") or {}).get("m5") or 0,
        "kaeufe5": tx_m5.get("buys", 0) or 0,
        "verkaeufe5": tx_m5.get("sells", 0) or 0,
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


PULS_MINUTEN = 25        # so lange gilt der Laptop nach dem letzten Lebenszeichen als aktiv
PULS_ALLE_MINUTEN = 15   # Laptop sendet höchstens alle 15 Min ein Lebenszeichen
                         # (ntfy erlaubt kostenlos ca. 250 Nachrichten pro Tag)


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


def lade_zustand() -> dict:
    """Gespeicherter Zustand: gemeldete Coins + Alarm-Zustände der Beobachtungsliste."""
    try:
        with open(GEMELDET_DATEI, encoding="utf-8") as f:
            z = json.load(f)
    except (OSError, ValueError):
        z = {}
    if isinstance(z, list):  # altes Format
        z = {"gemeldet": z}
    z.setdefault("gemeldet", [])
    z.setdefault("alarme", {})
    return z


def speichere_zustand(z: dict) -> None:
    with open(GEMELDET_DATEI, "w", encoding="utf-8") as f:
        json.dump(z, f, indent=1)


def lade_gemeldet(thema: str = "") -> set:
    """Bereits gemeldete Coins: lokale Datei + alles, was in den letzten 12 h gepusht wurde
    (so meldet GitHub nichts doppelt, was der Laptop schon geschickt hat - und umgekehrt)."""
    gemeldet = set(lade_zustand()["gemeldet"])
    if thema:
        for m in ntfy_verlauf(thema, "12h"):
            for zeile in str(m.get("message", "")).splitlines():
                if zeile.startswith("Adresse: "):
                    gemeldet.add(zeile[9:].strip())
                elif zeile.startswith("Kaufrausch-ID: "):
                    gemeldet.add(zeile[15:].strip())
                elif zeile.startswith("Symbol: "):
                    gemeldet.add("sym:" + zeile[8:].strip().upper())
    return gemeldet


def speichere_gemeldet(adressen: set) -> None:
    z = lade_zustand()
    z["gemeldet"] = sorted(adressen)
    speichere_zustand(z)


def melde_treffer(ergebnisse: list[dict], a: argparse.Namespace) -> None:
    """Push nur für NEUE Coins mit genug Punkten und ohne RugCheck-Gefahren."""
    if not a.ntfy:
        return
    gemeldet = lade_gemeldet(a.ntfy)
    neu = 0
    for e in ergebnisse:
        sym_key = "sym:" + str(e["k"]["symbol"]).strip().upper()
        if (e["adresse"] in gemeldet or sym_key in gemeldet
                or e["score"] < a.alarm_ab or e["rug"]["gefahren"]):
            continue
        k, r = e["k"], e["rug"]
        rug = "nicht geprüft" if r["risiko"] is None else f"{int(r['risiko'])}/100"
        text = (f"{k['name']} - {e['score']:.0f} Punkte\n"
                f"Alter {k['alter_h']:.0f}h | Liq {geld(k['liq'])} | MCap {geld(k['mcap'])}\n"
                f"1h {k['chg1']:+.0f}% | Käufe/Verkäufe {k['kaeufe']}/{k['verkaeufe']}\n"
                f"Rug-Risiko {rug}\n"
                f"Symbol: {str(k['symbol']).strip().upper()}\n"
                f"Adresse: {e['adresse']}")
        fomo = FOMO + e["adresse"]
        if sende_push(a.ntfy, f"🚨 Neuer Memecoin: {k['symbol']}", text, fomo,
                      knoepfe=[("FOMO", fomo), ("DexScreener", k["url"])]):
            gemeldet.add(e["adresse"])
            gemeldet.add(sym_key)
            neu += 1
    speichere_gemeldet(gemeldet)
    if neu:
        print(f"  {neu} Push-Nachricht(en) ans Handy geschickt")


# --------------------------------------------------------------------------
# KAUFRAUSCH: junger Coin wird gerade massiv gekauft
# --------------------------------------------------------------------------
KAUFRAUSCH = {
    "max_alter_h": 24,      # nur Coins jünger als 24 Stunden
    "min_liq": 8_000,       # mindestens 8.000 $ Liquidität
    "min_kaeufe_1h": 200,   # mindestens 200 Käufe in der letzten Stunde
    "kauf_verhaeltnis": 1.5,  # mindestens 1,5x so viele Käufe wie Verkäufe
    "min_vol_1h": 20_000,   # mindestens 20.000 $ Umsatz in der letzten Stunde
    "min_anstieg_1h": 50,   # Kurs +50 % in 1 Stunde ...
    "min_anstieg_5m": 15,   # ... ODER +15 % in 5 Minuten
}


def ist_kaufrausch(k: dict) -> bool:
    c = KAUFRAUSCH
    return (k["alter_h"] <= c["max_alter_h"]
            and k["liq"] >= c["min_liq"]
            and k["kaeufe"] >= c["min_kaeufe_1h"]
            and k["kaeufe"] >= c["kauf_verhaeltnis"] * max(k["verkaeufe"], 1)
            and k["vol1"] >= c["min_vol_1h"]
            and (k["chg1"] >= c["min_anstieg_1h"] or k["chg5"] >= c["min_anstieg_5m"]))


def melde_kaufrausch(markt: dict, jetzt_ms: int, a: argparse.Namespace) -> None:
    """Lauter Alarm, wenn ein junger Coin gerade massiv gekauft wird (pro Coin nur einmal)."""
    treffer = []
    for adr, p in markt.items():
        if adr in STABIL:
            continue
        k = kennzahlen(p, jetzt_ms)
        if ist_kaufrausch(k):
            treffer.append((adr, k))
    if not treffer:
        return
    zustand = lade_zustand()
    schon = set(zustand.setdefault("kaufrausch", []))
    if a.ntfy:
        for m in ntfy_verlauf(a.ntfy, "12h"):
            for z in str(m.get("message", "")).splitlines():
                if z.startswith("Kaufrausch-ID: "):
                    schon.add(z[15:].strip())
    treffer.sort(key=lambda t: t[1]["chg1"], reverse=True)
    for adr, k in treffer[:5]:
        sym = str(k["symbol"]).strip().upper()
        if adr in schon or "sym:" + sym in schon:
            continue
        r = rugcheck(adr)
        print(f"  KAUFRAUSCH: {k['symbol']} 1h {k['chg1']:+.0f}% | "
              f"Käufe/Verkäufe {k['kaeufe']}/{k['verkaeufe']} | Rug {r['risiko']}")
        if r["gefahren"]:
            print(f"    (kein Alarm - RugCheck-Gefahr: {', '.join(r['gefahren'])[:60]})")
            schon.update({adr, "sym:" + sym})
            continue
        rug = "nicht geprüft" if r["risiko"] is None else f"{int(r['risiko'])}/100"
        text = (f"{k['name']} wird gerade massiv gekauft!\n"
                f"1h {k['chg1']:+.0f}% | 5min {k['chg5']:+.0f}% | Alter {k['alter_h']:.0f}h\n"
                f"MCap {geld(k['mcap'])} | Liq {geld(k['liq'])} | Umsatz 1h {geld(k['vol1'])}\n"
                f"Käufe/Verkäufe 1h {k['kaeufe']}/{k['verkaeufe']}\n"
                f"Rug-Risiko {rug}\n"
                f"Kaufrausch-ID: {adr}")
        fomo = FOMO + adr
        if a.ntfy and sende_push(a.ntfy, f"🔥🚨 KAUFRAUSCH: {k['symbol']} "
                                 f"{k['chg1']:+.0f}% in 1h", text, fomo, prio=5,
                                 knoepfe=[("FOMO", fomo), ("DexScreener", k["url"])],
                                 tags=["fire"]):
            print("    -> Kaufrausch-Alarm geschickt")
        schon.update({adr, "sym:" + sym})
    zustand = lade_zustand()
    zustand["kaufrausch"] = sorted(schon)
    # Wer als Kaufrausch gemeldet wurde, kommt nicht nochmal als "Neuer Memecoin"
    zustand["gemeldet"] = sorted(set(zustand["gemeldet"]) | schon)
    speichere_zustand(zustand)


# --------------------------------------------------------------------------
# Beobachtungsliste: Alarm bei bestimmter Market Cap
# --------------------------------------------------------------------------
# Die Liste liegt im GitHub-Projekt. Der Laptop liest sie von dort, damit
# Laptop und Cloud immer dieselbe Liste benutzen. Fallback: lokale Datei.
BEOBACHTUNG_DATEI = "beobachtung.json"
BEOBACHTUNG_URL = ("https://raw.githubusercontent.com/putzhilfejj-dev/"
                   "memecoin-sucher/main/beobachtung.json")


def lade_beobachtung(cloud: bool) -> list[dict]:
    if not cloud:
        liste = hole_json(BEOBACHTUNG_URL, versuche=2)
        if isinstance(liste, list):
            return liste
    try:
        with open(BEOBACHTUNG_DATEI, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def aktuelle_mcap(chain: str, adressen: list[str]) -> dict[str, dict]:
    """Adresse -> liquidestes Handelspaar (mit marketCap, priceChange, url)."""
    beste: dict[str, dict] = {}
    for i in range(0, len(adressen), 30):
        paare = hole_json(f"{DEX}/tokens/v1/{chain}/{','.join(adressen[i:i + 30])}") or []
        for p in paare if isinstance(paare, list) else []:
            adr = (p.get("baseToken") or {}).get("address")
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if adr and liq >= ((beste.get(adr, {}).get("liquidity") or {}).get("usd") or 0):
                beste[adr] = p
    return beste


RUECKSETZ_ABSTAND = 0.03   # Grenze wird wieder "scharf", wenn MCap 3 % zurück auf der anderen Seite ist
SPRUNG_PAUSE_MIN = 60      # Abverkauf-/Anstieg-Alarm höchstens einmal pro Stunde je Richtung
TAGESBERICHT_STUNDE = 9    # täglicher Bericht ab 9 Uhr (deutsche Zeit)


def berlin_jetzt() -> datetime:
    """Deutsche Uhrzeit inkl. Sommerzeit, ohne Zusatzpakete."""
    from datetime import timedelta, timezone
    utc = datetime.now(timezone.utc)

    def letzter_sonntag(jahr, monat):
        d = datetime(jahr, monat, 31, 1, tzinfo=timezone.utc)
        return d - timedelta(days=(d.weekday() + 1) % 7)

    sommer = letzter_sonntag(utc.year, 3) <= utc < letzter_sonntag(utc.year, 10)
    return (utc + timedelta(hours=2 if sommer else 1)).replace(tzinfo=None)


def stufen_von(c: dict) -> list[dict]:
    """Alle Preis-Grenzen eines Coins (neues Format 'stufen' oder altes 'unter'/'ueber')."""
    stufen = list(c.get("stufen") or [])
    if c.get("unter"):
        stufen.append({"art": "unter", "mcap": c["unter"]})
    if c.get("ueber"):
        stufen.append({"art": "ueber", "mcap": c["ueber"]})
    return stufen


def letzte_alarme(thema: str) -> dict[str, float]:
    """Alarm-ID -> Zeitpunkt (Unix) des letzten Pushs, aus dem ntfy-Verlauf (12 h)."""
    zeiten: dict[str, float] = {}
    for m in ntfy_verlauf(thema, "12h"):
        for z in str(m.get("message", "")).splitlines():
            if z.startswith("Alarm-ID: "):
                k = z[10:].strip()
                zeiten[k] = max(zeiten.get(k, 0), float(m.get("time", 0)))
    return zeiten


def pruefe_beobachtung(a: argparse.Namespace) -> None:
    """Alarme für beobachtete Coins:
    - Preis-Grenzen (z. B. unter 15 Mio., unter 10 Mio. = ACHTUNG, über 20 Mio.)
      Jede Grenze meldet sich bei JEDEM neuen Durchbruch: fällt der Coin unter 15 Mio.,
      erholt sich auf 17 Mio. und fällt wieder, kommt erneut ein Alarm.
    - Starker Abverkauf / starker Anstieg in der letzten Stunde
    - Täglicher Bericht ab 9 Uhr"""
    liste = lade_beobachtung(a.cloud)
    if not liste:
        return
    print(f"  Beobachtungsliste: {len(liste)} Coin(s) prüfen ...")
    zustand = lade_zustand()
    alarme = zustand["alarme"]           # Alarm-ID -> {"scharf": bool, "seit": unix}
    verlauf = letzte_alarme(a.ntfy) if a.ntfy else {}
    jetzt = time.time()
    bericht_zeilen = []

    def ausloesen(alarm_id, titel, text, adresse, url, prio, tags):
        """Push senden, außer der andere Rechner hat genau diesen Alarm schon geschickt."""
        st = alarme.setdefault(alarm_id, {"scharf": True, "seit": 0})
        if not st["scharf"]:
            return
        if verlauf.get(alarm_id, 0) > st["seit"]:  # Laptop/Cloud hat schon gemeldet
            st["scharf"] = False
            return
        if not a.ntfy:
            return
        fomo = FOMO + adresse
        voll = text + f"\nAlarm-ID: {alarm_id}"
        daten_ok = sende_push(a.ntfy, titel, voll, fomo, prio=prio,
                              knoepfe=[("FOMO", fomo), ("DexScreener", url)], tags=tags)
        if daten_ok:
            st["scharf"] = False
            print(f"    -> Alarm: {titel}")

    def entschaerfen(alarm_id):
        st = alarme.setdefault(alarm_id, {"scharf": True, "seit": 0})
        if not st["scharf"]:
            st["scharf"] = True
            st["seit"] = jetzt

    nach_chain: dict[str, list[dict]] = {}
    for c in liste:
        if c.get("adresse"):
            nach_chain.setdefault(c.get("chain", CHAIN), []).append(c)

    for chain, coins in nach_chain.items():
        daten = aktuelle_mcap(chain, [c["adresse"] for c in coins])
        for c in coins:
            adr = c["adresse"]
            p = daten.get(adr)
            if not p:
                print(f"    {c.get('name', adr)}: keine Daten")
                continue
            mcap = p.get("marketCap") or p.get("fdv") or 0
            sym = (p.get("baseToken") or {}).get("symbol", c.get("name", "?"))
            chg = p.get("priceChange") or {}
            tx = ((p.get("txns") or {}).get("h1") or {})
            k1, v1 = tx.get("buys", 0) or 0, tx.get("sells", 0) or 0
            url = p.get("url", "")
            info = (f"1h {chg.get('h1', 0):+.0f}% | 24h {chg.get('h24', 0):+.0f}% | "
                    f"Käufe/Verkäufe 1h {k1}/{v1}")
            print(f"    {sym}: Market Cap {geld(mcap)} | {info}")
            bericht_zeilen.append(f"{sym}: {geld(mcap)} ({chg.get('h24', 0):+.0f}% 24h)")

            # --- Preis-Grenzen ---
            for s in stufen_von(c):
                grenze, art = float(s["mcap"]), s["art"]
                aid = f"{adr}:{art}:{int(grenze)}"
                achtung = bool(s.get("achtung"))
                if art == "unter":
                    erreicht = mcap <= grenze
                    zurueck = mcap >= grenze * (1 + RUECKSETZ_ABSTAND)
                    titel = (f"🚨🚨🚨 ACHTUNG: {sym} unter {geld(grenze)} MCap! 🚨🚨🚨" if achtung
                             else f"⚠️ {sym} unter {geld(grenze)} MCap")
                    text = f"Market Cap gefallen auf {geld(mcap)}"
                    tags = ["rotating_light"] if achtung else ["warning"]
                else:
                    erreicht = mcap >= grenze
                    zurueck = mcap <= grenze * (1 - RUECKSETZ_ABSTAND)
                    titel = (f"🚨🚨🚨 ACHTUNG: {sym} über {geld(grenze)} MCap! 🚨🚨🚨" if achtung
                             else f"📈 {sym} über {geld(grenze)} MCap")
                    text = f"Market Cap gestiegen auf {geld(mcap)}"
                    tags = ["chart_with_upwards_trend"]
                if s.get("hinweis"):
                    text += f"\n{s['hinweis']}"
                if erreicht:
                    ausloesen(aid, titel, f"{text}\n{info}", adr, url, 5 if achtung else 4, tags)
                elif zurueck:
                    entschaerfen(aid)

            # --- Plötzliche Bewegung (alle verkaufen / alle kaufen) ---
            abv = float(c.get("abverkauf_1h", -15))
            anst = float(c.get("anstieg_1h", 20))
            h1 = float(chg.get("h1", 0) or 0)
            for art, aktiv, titel, tags in [
                ("abverkauf", h1 <= abv or (v1 >= 2 * max(k1, 1) and k1 + v1 >= 60),
                 f"🚨🚨🚨 ACHTUNG: {sym} wird stark verkauft! 🚨🚨🚨", ["rotating_light", "chart_with_downwards_trend"]),
                ("anstieg", h1 >= anst or (k1 >= 2 * max(v1, 1) and k1 + v1 >= 60),
                 f"🚀 {sym} wird stark gekauft!", ["rocket"]),
            ]:
                aid = f"{adr}:{art}"
                st = alarme.setdefault(aid, {"scharf": True, "seit": 0})
                if not st["scharf"] and jetzt - st.get("ausgeloest", 0) >= SPRUNG_PAUSE_MIN * 60:
                    st["scharf"], st["seit"] = True, jetzt  # nach 1 Stunde wieder bereit
                if aktiv and st["scharf"]:
                    if verlauf.get(aid, 0) > jetzt - SPRUNG_PAUSE_MIN * 60:
                        st["scharf"], st["ausgeloest"] = False, verlauf[aid]
                        continue
                    ausloesen(aid, titel, f"Market Cap jetzt {geld(mcap)}\n{info}",
                              adr, url, 5, tags)
                    if not st["scharf"]:
                        st["ausgeloest"] = jetzt

    # --- Täglicher Bericht ---
    heute = berlin_jetzt()
    if a.ntfy and bericht_zeilen and heute.hour >= TAGESBERICHT_STUNDE:
        tag = heute.strftime("%Y-%m-%d")
        aid = f"tagesbericht:{tag}"
        if zustand.get("letzter_bericht") != tag and aid not in verlauf:
            if sende_push(a.ntfy, f"Tagesbericht {heute:%d.%m.}",
                          "\n".join(bericht_zeilen) + f"\nAlarm-ID: {aid}", prio=3,
                          tags=["bar_chart"]):
                print("    -> Tagesbericht geschickt")
                zustand["letzter_bericht"] = tag
        elif aid in verlauf:
            zustand["letzter_bericht"] = tag

    speichere_zustand(zustand)


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
    pruefe_beobachtung(a)
    kandidaten = sammle_kandidaten()
    print(f"  {len(kandidaten)} Tokens in den Feeds gefunden (DexScreener + GeckoTerminal)")
    if not kandidaten:
        print("  (Keine Daten - Internetverbindung prüfen.)")
        return []

    markt = hole_marktdaten(list(kandidaten))
    jetzt = int(time.time() * 1000)
    melde_kaufrausch(markt, jetzt, a)

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
    return top


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Findet frühe Solana-Memecoins (DexScreener + RugCheck).")
    ap.add_argument("--min-liq", type=float, default=10_000, help="Mindest-Liquidität in $ (Standard 10000)")
    ap.add_argument("--min-vol", type=float, default=20_000, help="Mindest-Umsatz 24h in $ (Standard 20000)")
    ap.add_argument("--max-alter", type=float, default=72, help="Höchstalter des Paars in Stunden (Standard 72)")
    ap.add_argument("--min-tx", type=int, default=30, help="Mindest-Transaktionen letzte Stunde (Standard 30)")
    ap.add_argument("--max-mcap", type=float, default=0, help="Höchste Marketcap in $ (0 = egal)")
    ap.add_argument("--top", type=int, default=10, help="Wie viele Treffer anzeigen (Standard 10)")
    ap.add_argument("--watch", type=float, default=0, help="Alle X Minuten wiederholen (0 = nur einmal)")
    ap.add_argument("--csv", default="memecoin_treffer.csv", help="CSV-Datei für die Treffer")
    ap.add_argument("--ohne-rugcheck", action="store_true", help="RugCheck überspringen (schneller)")
    ap.add_argument("--ntfy", default="", help="ntfy-Thema für Handy-Pushs (z. B. memecoins-x7k2p9)")
    ap.add_argument("--alarm-ab", type=float, default=60, help="Push erst ab so vielen Punkten (Standard 60)")
    ap.add_argument("--test-push", action="store_true", help="Nur eine Test-Nachricht ans Handy schicken")
    ap.add_argument("--cloud", action="store_true",
                    help="Cloud-Modus (GitHub): nur suchen, wenn der Laptop gerade NICHT läuft")
    ap.add_argument("--laufzeit", type=float, default=0,
                    help="Cloud: so viele Minuten lang wiederholt suchen (mit --watch)")
    a = ap.parse_args(argv)
    a.ntfy = a.ntfy or os.environ.get("NTFY_THEMA", "")

    if a.cloud:
        if not a.ntfy:
            print("Cloud-Modus braucht ein ntfy-Thema (--ntfy oder NTFY_THEMA).")
            return
        if a.laufzeit <= 0:
            a.watch = 0
            if laptop_aktiv(a.ntfy):
                print(f"Laptop war in den letzten {PULS_MINUTEN} Minuten aktiv - "
                      "er übernimmt die Suche, die Cloud pausiert.")
                return

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
    try:
        letzter_puls = 0.0
        ende = time.time() + a.laufzeit * 60 if a.laufzeit > 0 else None
        while True:
            if a.cloud and a.laufzeit > 0 and laptop_aktiv(a.ntfy):
                # Laptop läuft: Cloud wartet im Hintergrund und übernimmt sofort,
                # sobald das Lebenszeichen ausbleibt
                print(f"[{datetime.now():%H:%M}] Laptop ist an - Cloud wartet.")
                if ende and time.time() + a.watch * 60 > ende:
                    print("Laufzeit vorbei - der nächste GitHub-Lauf übernimmt.")
                    break
                time.sleep(a.watch * 60)
                continue
            if a.cloud:
                print("Laptop ist aus - die Cloud sucht.")
            if (a.ntfy and not a.cloud and a.watch > 0
                    and time.time() - letzter_puls >= PULS_ALLE_MINUTEN * 60 - 30):
                sende_puls(a.ntfy)  # sagt der Cloud: "Laptop läuft, du kannst pausieren"
                letzter_puls = time.time()
            ein_durchlauf(a)
            if a.watch <= 0:
                break
            if ende and time.time() + a.watch * 60 > ende:
                print("\nLaufzeit vorbei - der nächste GitHub-Lauf übernimmt.")
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
