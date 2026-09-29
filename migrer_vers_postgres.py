# -*- coding: utf-8 -*-
"""
Transfert des données de la base locale SQLite (atelierpro.db) vers
PostgreSQL (Neon).

Usage (depuis le dossier atelierpro) :
    python migrer_vers_postgres.py "postgresql://...neon.tech/neondb?sslmode=require"

    (ou sans argument si DATABASE_URL est défini dans l'environnement)

Garanties :
  - les identifiants sont conservés (commandes, clients, couturiers et
    paiements restent liés au bon atelier) ;
  - tout est copié en UNE transaction : en cas d'erreur, rien n'est écrit ;
  - le script refuse de s'exécuter si la base PostgreSQL contient déjà des
    données (aucun écrasement, aucun doublon) ;
  - la base locale n'est jamais modifiée.
"""

import os
import sys
import sqlite3

# Ordre de copie (les tables référencées d'abord)
TABLES = ["ateliers", "couturiers", "clients", "commandes", "paiements_commande",
          "stock", "transactions"]
TABLES_AVEC_ID = {"ateliers", "couturiers", "clients", "commandes", "paiements_commande", "stock"}


def main():
    url = (sys.argv[1] if len(sys.argv) > 1 else "") or os.environ.get("DATABASE_URL", "")
    url = url.strip()
    if not url.startswith(("postgres://", "postgresql://")):
        sys.exit("Indiquez l'URL PostgreSQL (Neon) :\n"
                 '  python migrer_vers_postgres.py "postgresql://..."')

    ici = os.path.dirname(os.path.abspath(__file__))
    source = os.environ.get("DB_PATH") or os.path.join(ici, "atelierpro.db")
    if not os.path.exists(source):
        sys.exit("Base locale introuvable : %s" % source)

    # db.py lit DATABASE_URL à l'import : on le fixe avant, pour que
    # init_db() crée les tables dans PostgreSQL.
    os.environ["DATABASE_URL"] = url
    os.environ.pop("VERCEL", None)
    sys.path.insert(0, ici)
    import db
    import psycopg
    db.init_db()

    src = sqlite3.connect("file:%s?mode=ro" % source, uri=True)  # lecture seule
    src.row_factory = sqlite3.Row

    with psycopg.connect(url) as pg:  # une seule transaction (commit à la fin)
        # 1) La base cible doit être vide
        deja = {t: pg.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in TABLES}
        pleines = {t: n for t, n in deja.items() if n}
        if pleines:
            sys.exit("Transfert annulé : la base PostgreSQL contient déjà des données %s.\n"
                     "Rien n'a été modifié." % pleines)

        bilan = []
        for t in TABLES:
            cols_src = [r["name"] for r in src.execute("PRAGMA table_info(%s)" % t)]
            cols_pg = {r[0] for r in pg.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name=%s", (t,))}
            cols = [c for c in cols_src if c in cols_pg]
            lignes = src.execute("SELECT %s FROM %s" % (", ".join(cols), t)).fetchall()
            if lignes:
                sql = "INSERT INTO %s (%s) VALUES (%s)" % (
                    t, ", ".join(cols), ", ".join(["%s"] * len(cols)))
                with pg.cursor() as cur:
                    cur.executemany(sql, [tuple(r) for r in lignes])
            # 2) Les prochains id créés en ligne doivent suivre les id copiés
            if t in TABLES_AVEC_ID:
                pg.execute("SELECT setval(pg_get_serial_sequence(%s, 'id'), "
                           "COALESCE((SELECT MAX(id) FROM " + t + "), 0) + 1, false)", (t,))
            bilan.append((t, len(lignes)))

        # 3) Vérification avant validation
        for t, n in bilan:
            copie = pg.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            if copie != n:
                raise RuntimeError("Vérification échouée pour %s (%d != %d)" % (t, copie, n))

    src.close()
    print("Transfert terminé :")
    for t, n in bilan:
        print("  %-20s %d ligne(s)" % (t, n))


if __name__ == "__main__":
    main()
