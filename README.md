# Memecoin-Sucher

Sucht frühe Solana-Memecoins (DexScreener + RugCheck) und schickt Treffer per ntfy aufs Handy.

- Laptop: `memecoin_start.bat` – sucht alle 5 Minuten und sendet ein Lebenszeichen.
- GitHub Actions: läuft alle ~5 Minuten, sucht aber nur, wenn der Laptop seit 12 Minuten kein Lebenszeichen mehr geschickt hat.
- Das ntfy-Thema liegt als Secret `NTFY_THEMA` im Repository.

Nur Recherche, keine Kaufempfehlung.
