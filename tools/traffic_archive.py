#!/usr/bin/env python3
"""Holt die GitHub-Traffic-Daten (14-Tage-Fenster) und merged sie in eine CSV.

GitHub loescht Traffic-Daten nach 14 Tagen. Dieses Skript laeuft taeglich per
GitHub Actions und schreibt jeden Tag genau einmal fest, sodass ueber die Zeit
eine luekenlose Gesamtstatistik entsteht.

Aufruf:  GITHUB_TOKEN=... GITHUB_REPOSITORY=owner/name python3 tools/traffic_archive.py
"""
import csv
import json
import os
import sys
import urllib.request
from pathlib import Path

REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GITHUB_TOKEN"]
OUT = Path("docs/traffic")


def api(path):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/{path}",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def merge_daily():
    """Views + Clones pro Tag zusammenfuehren, vorhandene Zeilen nicht ueberschreiben."""
    rows = {}
    csv_path = OUT / "daily.csv"
    if csv_path.exists():
        with csv_path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                rows[row["date"]] = row

    for key, endpoint in (("views", "traffic/views"), ("clones", "traffic/clones")):
        for entry in api(endpoint).get(key, []):
            day = entry["timestamp"][:10]
            row = rows.setdefault(day, {"date": day, "views": "0", "unique_views": "0",
                                        "clones": "0", "unique_clones": "0"})
            if key == "views":
                row["views"], row["unique_views"] = str(entry["count"]), str(entry["uniques"])
            else:
                row["clones"], row["unique_clones"] = str(entry["count"]), str(entry["uniques"])

    fields = ["date", "views", "unique_views", "clones", "unique_clones"]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for day in sorted(rows):
            w.writerow({k: rows[day].get(k, "0") for k in fields})
    return rows


def snapshot_lists(today):
    """Referrer und beliebte Pfade als Tages-Snapshot ablegen (nicht mergebar,
    weil GitHub hier nur Summen ueber das Fenster liefert)."""
    for name, endpoint in (("referrers", "traffic/popular/referrers"),
                           ("paths", "traffic/popular/paths")):
        data = api(endpoint)
        if not data:
            continue
        path = OUT / f"{name}.csv"
        exists = path.exists()
        with path.open("a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(["snapshot_date", "name", "views", "uniques"])
            for e in data:
                w.writerow([today, e.get("referrer") or e.get("path"), e["count"], e["uniques"]])


def write_summary(rows):
    """Kumulierte Gesamtzahlen als Markdown, damit man sie ohne Tabelle sieht."""
    total_v = sum(int(r["views"]) for r in rows.values())
    total_uv = sum(int(r["unique_views"]) for r in rows.values())
    total_c = sum(int(r["clones"]) for r in rows.values())
    total_uc = sum(int(r["unique_clones"]) for r in rows.values())
    days = sorted(rows)

    downloads = []
    for rel in api("releases"):
        for asset in rel.get("assets", []):
            downloads.append((rel["tag_name"], asset["name"], asset["download_count"]))
    dl_total = sum(d[2] for d in downloads)

    lines = [
        "# Traffic-Gesamtstatistik",
        "",
        f"Zeitraum: **{days[0]} bis {days[-1]}** ({len(days)} erfasste Tage)",
        "",
        "| Kennzahl | Gesamt |",
        "|---|---:|",
        f"| Seitenaufrufe | {total_v} |",
        f"| Eindeutige Besucher (Summe pro Tag) | {total_uv} |",
        f"| Clones | {total_c} |",
        f"| Eindeutige Cloner (Summe pro Tag) | {total_uc} |",
        f"| Release-Downloads | {dl_total} |",
        "",
        "## Release-Downloads im Detail",
        "",
        "| Release | Datei | Downloads |",
        "|---|---|---:|",
    ]
    for tag, name, count in downloads:
        lines.append(f"| {tag} | {name} | {count} |")
    lines += [
        "",
        "> Die eindeutigen Besucher sind pro Tag gezaehlt und deshalb nur addiert,",
        "> nicht dedupliziert. Ein Besucher an drei Tagen zaehlt hier dreifach.",
        "",
        "_Automatisch erzeugt von `tools/traffic_archive.py`._",
        "",
    ]
    (OUT / "SUMMARY.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rows = merge_daily()
    if not rows:
        print("Keine Traffic-Daten erhalten.", file=sys.stderr)
        return 1
    snapshot_lists(sorted(rows)[-1])
    write_summary(rows)
    print(f"{len(rows)} Tage archiviert.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
