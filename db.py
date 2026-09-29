# -*- coding: utf-8 -*-
"""
Base de données SQLite pour AtelierPro.

Un simple fichier (atelierpro.db), aucun serveur à installer.
Contient tout le cœur de la plateforme :
  - ateliers      : les comptes (patrons), avec abonnement
  - clients       : fiches clients + mesures (par atelier)
  - commandes     : suivi de production + chronomètre (par atelier)
  - stock         : tissus & fournitures (par atelier)
  - transactions  : paiements Paystack

Chaque donnée est rattachée à un atelier (atelier_id) : un patron ne voit
que SON atelier. Pour passer plus tard à PostgreSQL/MySQL, seul ce fichier
serait à adapter.
"""

import os
import re
import json
import secrets
import sqlite3
import datetime
import unicodedata

from werkzeug.security import generate_password_hash, check_password_hash

DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "atelierpro.db"),
)

# Durée de l'essai gratuit à l'inscription (0 = pas d'essai, désactivé par défaut).
try:
    JOURS_ESSAI = max(0, int(os.environ.get("JOURS_ESSAI", "0")))
except ValueError:
    JOURS_ESSAI = 0
RESET_VALIDITE_MIN = 60  # durée de validité d'un lien de réinitialisation (minutes)

# Mesures de couture par défaut (clé technique -> libellé affiché).
# Chaque atelier peut personnaliser cette liste (voir mesures_atelier/maj_mesures_atelier).
DEFAULT_MESURES = [
    ("tour_poitrine", "Tour de poitrine"), ("tour_taille", "Tour de taille"),
    ("tour_bassin", "Tour de bassin"), ("longueur_robe", "Longueur robe / boubou"),
    ("epaule", "Épaule"), ("longueur_manche", "Longueur manche"),
    ("tour_manche", "Tour de manche"), ("tour_cou", "Tour de cou"),
    ("hauteur_poitrine", "Hauteur poitrine"), ("longueur_taille", "Longueur taille dos"),
    ("tour_cuisse", "Tour de cuisse"), ("longueur_pantalon", "Longueur pantalon"),
    ("tour_genou", "Tour de genou"), ("bas_pantalon", "Bas du pantalon"),
]


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Le mode WAL nécessite un verrouillage par mémoire partagée (fichier -shm)
    # non fiable sur les supports amovibles / FAT32 (clé USB, carte SD...),
    # d'où des "disk I/O error". Le mode DELETE (journal classique) fonctionne
    # partout, y compris sur ces supports.
    conn.execute("PRAGMA journal_mode=DELETE;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _maintenant():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _aujourdhui():
    return datetime.date.today().isoformat()


# ---------------------------------------------------------------------------
#  Création / migration des tables
# ---------------------------------------------------------------------------
def init_db():
    with get_conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS ateliers (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                nom           TEXT NOT NULL,
                responsable   TEXT,
                email         TEXT UNIQUE NOT NULL,
                telephone     TEXT,
                mot_de_passe  TEXT NOT NULL,
                role          TEXT DEFAULT 'patron',
                abo_fin       TEXT,
                cree_le       TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                atelier_id  INTEGER NOT NULL,
                nom         TEXT NOT NULL,
                telephone   TEXT,
                modele      TEXT,
                mesures     TEXT,          -- JSON des mesures
                notes       TEXT,
                cree_le     TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS commandes (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                atelier_id      INTEGER NOT NULL,
                client_id       INTEGER,
                description     TEXT,
                statut          TEXT DEFAULT 'EN_ATTENTE',
                prix            INTEGER DEFAULT 0,
                acompte         INTEGER DEFAULT 0,
                date_commande   TEXT,
                date_livraison  TEXT,
                chrono_secondes INTEGER DEFAULT 0,
                chrono_debut    TEXT,
                cree_le         TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS stock (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                atelier_id  INTEGER NOT NULL,
                article     TEXT NOT NULL,
                categorie   TEXT,
                quantite    REAL DEFAULT 0,
                unite       TEXT,
                seuil       REAL DEFAULT 0,
                cree_le     TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                transaction_id TEXT PRIMARY KEY,
                atelier_id     INTEGER,
                atelier        TEXT,
                nom            TEXT,
                prenom         TEXT,
                email          TEXT,
                telephone      TEXT,
                mois           INTEGER,
                montant        INTEGER,
                canal          TEXT,
                statut         TEXT,
                payment_token  TEXT,
                recu_envoye    INTEGER DEFAULT 0,
                cree_le        TEXT,
                paye_le        TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS couturiers (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                atelier_id    INTEGER NOT NULL,
                nom           TEXT NOT NULL,
                email         TEXT UNIQUE NOT NULL,
                telephone     TEXT,
                mot_de_passe  TEXT NOT NULL,
                actif         INTEGER DEFAULT 1,
                cree_le       TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS paiements_commande (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                atelier_id   INTEGER NOT NULL,
                commande_id  INTEGER NOT NULL,
                montant      INTEGER DEFAULT 0,
                moyen        TEXT,
                note         TEXT,
                par          TEXT,
                cree_le      TEXT
            )
        """)
        # Petites migrations pour les bases déjà existantes
        _ajouter_colonne(c, "transactions", "atelier_id", "INTEGER")
        _ajouter_colonne(c, "transactions", "recu_envoye", "INTEGER DEFAULT 0")
        _ajouter_colonne(c, "commandes", "couturier_id", "INTEGER")
        _ajouter_colonne(c, "ateliers", "mesures_config", "TEXT")
        _ajouter_colonne(c, "ateliers", "reset_token", "TEXT")
        _ajouter_colonne(c, "ateliers", "reset_expire", "TEXT")
        _ajouter_colonne(c, "couturiers", "reset_token", "TEXT")
        _ajouter_colonne(c, "couturiers", "reset_expire", "TEXT")
        _ajouter_colonne(c, "clients", "couturier_id", "INTEGER")


def _ajouter_colonne(c, table, colonne, definition):
    cols = [r["name"] for r in c.execute("PRAGMA table_info(%s)" % table).fetchall()]
    if colonne not in cols:
        c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, colonne, definition))


# ---------------------------------------------------------------------------
#  ATELIERS (comptes + abonnement)
# ---------------------------------------------------------------------------
def creer_atelier(nom, responsable, email, telephone, mot_de_passe):
    # Sans essai (JOURS_ESSAI=0), aucune date de fin : l'atelier n'a pas d'abonnement.
    abo_fin = ((datetime.date.today() + datetime.timedelta(days=JOURS_ESSAI)).isoformat()
               if JOURS_ESSAI > 0 else None)
    with get_conn() as c:
        cur = c.execute("""
            INSERT INTO ateliers (nom, responsable, email, telephone, mot_de_passe, abo_fin, cree_le)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (nom, responsable, email.lower().strip(), telephone,
              generate_password_hash(mot_de_passe), abo_fin, _maintenant()))
        return cur.lastrowid


def atelier_par_email(email):
    with get_conn() as c:
        r = c.execute("SELECT * FROM ateliers WHERE email = ?", (email.lower().strip(),)).fetchone()
    return dict(r) if r else None


def atelier_par_id(atelier_id):
    with get_conn() as c:
        r = c.execute("SELECT * FROM ateliers WHERE id = ?", (atelier_id,)).fetchone()
    return dict(r) if r else None


def verifier_connexion(email, mot_de_passe):
    a = atelier_par_email(email)
    if a and check_password_hash(a["mot_de_passe"], mot_de_passe):
        return a
    return None


def prolonger_abonnement(atelier_id, mois):
    """Prolonge l'abonnement de `mois` mois (à partir d'aujourd'hui ou de la fin
    en cours si elle est postérieure)."""
    a = atelier_par_id(atelier_id)
    if not a:
        return
    base = datetime.date.today()
    if a.get("abo_fin"):
        try:
            fin = datetime.date.fromisoformat(a["abo_fin"])
            if fin > base:
                base = fin
        except ValueError:
            pass
    nouvelle_fin = (base + datetime.timedelta(days=30 * int(mois))).isoformat()
    with get_conn() as c:
        c.execute("UPDATE ateliers SET abo_fin = ? WHERE id = ?", (nouvelle_fin, atelier_id))
    return nouvelle_fin


def abo_actif(atelier):
    """True si l'abonnement (ou l'essai) est encore valable."""
    if not atelier or not atelier.get("abo_fin"):
        return False
    try:
        return datetime.date.fromisoformat(atelier["abo_fin"]) >= datetime.date.today()
    except ValueError:
        return False


def suspendre_abonnement(atelier_id):
    """Coupe l'accès immédiatement (fin d'abonnement = hier)."""
    hier = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    with get_conn() as c:
        c.execute("UPDATE ateliers SET abo_fin=? WHERE id=?", (hier, atelier_id))


def definir_abo_fin(atelier_id, date_iso):
    with get_conn() as c:
        c.execute("UPDATE ateliers SET abo_fin=? WHERE id=?", (date_iso, atelier_id))


# ---------------------------------------------------------------------------
#  MESURES (personnalisables par atelier)
# ---------------------------------------------------------------------------
def _slugifier(texte, existants):
    base = unicodedata.normalize("NFKD", texte or "").encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^a-zA-Z0-9]+", "_", base).strip("_").lower() or "mesure"
    cle, i = base, 2
    while cle in existants:
        cle = "%s_%d" % (base, i)
        i += 1
    return cle


def mesures_atelier(atelier):
    """Liste [(cle, libellé), ...] des champs de mesures de l'atelier
    (personnalisée si définie, sinon la liste par défaut)."""
    if atelier and atelier.get("mesures_config"):
        try:
            data = json.loads(atelier["mesures_config"])
            if data:
                return [(d["cle"], d["libelle"]) for d in data]
        except (ValueError, KeyError, TypeError):
            pass
    return list(DEFAULT_MESURES)


def maj_mesures_atelier(atelier_id, mesures):
    """Enregistre la liste complète [(cle, libellé), ...] pour l'atelier."""
    data = [{"cle": k, "libelle": lib} for k, lib in mesures]
    with get_conn() as c:
        c.execute("UPDATE ateliers SET mesures_config=? WHERE id=?",
                  (json.dumps(data, ensure_ascii=False), atelier_id))


def ajouter_mesure(atelier_id, atelier, libelle):
    """Ajoute un nouveau champ de mesure (clé générée depuis le libellé)."""
    mesures = mesures_atelier(atelier)
    cle = _slugifier(libelle, {k for k, _ in mesures})
    mesures.append((cle, libelle.strip()))
    maj_mesures_atelier(atelier_id, mesures)
    return cle


def supprimer_mesure(atelier_id, atelier, cle):
    mesures = [m for m in mesures_atelier(atelier) if m[0] != cle]
    maj_mesures_atelier(atelier_id, mesures)


def reinitialiser_mesures(atelier_id):
    """Revient à la liste de mesures par défaut."""
    with get_conn() as c:
        c.execute("UPDATE ateliers SET mesures_config=NULL WHERE id=?", (atelier_id,))


# ---------------------------------------------------------------------------
#  RÉINITIALISATION DE MOT DE PASSE (patron ou couturier)
# ---------------------------------------------------------------------------
def generer_reset_token(email):
    """Attribue un jeton de réinitialisation (valable RESET_VALIDITE_MIN minutes)
    au compte (patron ou couturier) correspondant à cet e-mail.
    Renvoie (type, utilisateur, token) ou None si l'e-mail est inconnu."""
    email = (email or "").strip().lower()
    if not email:
        return None
    token = secrets.token_urlsafe(32)
    expire = (datetime.datetime.now()
              + datetime.timedelta(minutes=RESET_VALIDITE_MIN)).isoformat(timespec="seconds")
    a = atelier_par_email(email)
    if a:
        with get_conn() as c:
            c.execute("UPDATE ateliers SET reset_token=?, reset_expire=? WHERE id=?",
                      (token, expire, a["id"]))
        return ("patron", a, token)
    co = couturier_par_email(email)
    if co:
        with get_conn() as c:
            c.execute("UPDATE couturiers SET reset_token=?, reset_expire=? WHERE id=?",
                      (token, expire, co["id"]))
        return ("couturier", co, token)
    return None


def verifier_reset_token(token):
    """Renvoie (type, utilisateur) si le jeton est valide et non expiré, sinon None."""
    if not token:
        return None
    maintenant = _maintenant()
    with get_conn() as c:
        r = c.execute("SELECT * FROM ateliers WHERE reset_token=? AND reset_expire>=?",
                      (token, maintenant)).fetchone()
        if r:
            return ("patron", dict(r))
        r = c.execute("SELECT * FROM couturiers WHERE reset_token=? AND reset_expire>=?",
                      (token, maintenant)).fetchone()
        if r:
            return ("couturier", dict(r))
    return None


def reinitialiser_mot_de_passe(type_compte, user_id, nouveau_mdp):
    table = "ateliers" if type_compte == "patron" else "couturiers"
    with get_conn() as c:
        c.execute("UPDATE %s SET mot_de_passe=?, reset_token=NULL, reset_expire=NULL WHERE id=?" % table,
                  (generate_password_hash(nouveau_mdp), user_id))


# ---------------------------------------------------------------------------
#  SUPERVISION (super-admin — tous les ateliers)
# ---------------------------------------------------------------------------
def lister_ateliers():
    with get_conn() as c:
        rows = c.execute("""
            SELECT a.*,
              (SELECT COUNT(*) FROM couturiers cu WHERE cu.atelier_id=a.id)  AS nb_couturiers,
              (SELECT COUNT(*) FROM clients cl WHERE cl.atelier_id=a.id)      AS nb_clients,
              (SELECT COUNT(*) FROM commandes co WHERE co.atelier_id=a.id)    AS nb_commandes,
              (SELECT COALESCE(SUM(montant),0) FROM transactions t
                 WHERE t.atelier_id=a.id AND t.statut='PAYE')                AS recette,
              (SELECT COUNT(*) FROM transactions t
                 WHERE t.atelier_id=a.id AND t.statut='PAYE')                AS nb_paiements
            FROM ateliers a ORDER BY a.cree_le DESC
        """).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["actif"] = abo_actif(d)
        d["jours_restants"] = jours_restants(d)
        d["a_deja_paye"] = d["nb_paiements"] > 0
        out.append(d)
    return out


def stats_globales():
    ajd = _aujourdhui()
    with get_conn() as c:
        row = c.execute("""
            SELECT
              (SELECT COUNT(*) FROM ateliers)                                   AS nb_ateliers,
              (SELECT COUNT(*) FROM ateliers WHERE abo_fin >= ?)                AS actifs,
              (SELECT COUNT(*) FROM couturiers)                                 AS nb_couturiers,
              (SELECT COUNT(*) FROM commandes)                                  AS nb_commandes,
              (SELECT COALESCE(SUM(montant),0) FROM transactions WHERE statut='PAYE') AS recette,
              (SELECT COUNT(*) FROM transactions WHERE statut='PAYE')           AS nb_paiements
        """, (ajd,)).fetchone()
    d = dict(row)
    d["expires"] = d["nb_ateliers"] - d["actifs"]
    return d


def jours_restants(atelier):
    if not atelier or not atelier.get("abo_fin"):
        return 0
    try:
        return (datetime.date.fromisoformat(atelier["abo_fin"]) - datetime.date.today()).days
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
#  COUTURIERS (comptes créés par le patron)
# ---------------------------------------------------------------------------
def creer_couturier(atelier_id, nom, email, telephone, mot_de_passe):
    with get_conn() as c:
        cur = c.execute("""
            INSERT INTO couturiers (atelier_id, nom, email, telephone, mot_de_passe, actif, cree_le)
            VALUES (?, ?, ?, ?, ?, 1, ?)
        """, (atelier_id, nom, email.lower().strip(), telephone,
              generate_password_hash(mot_de_passe), _maintenant()))
        return cur.lastrowid


def couturier_par_email(email):
    with get_conn() as c:
        r = c.execute("SELECT * FROM couturiers WHERE email = ?", (email.lower().strip(),)).fetchone()
    return dict(r) if r else None


def couturier_par_id(couturier_id):
    with get_conn() as c:
        r = c.execute("SELECT * FROM couturiers WHERE id = ?", (couturier_id,)).fetchone()
    return dict(r) if r else None


def lister_couturiers(atelier_id):
    with get_conn() as c:
        rows = c.execute(
            "SELECT * FROM couturiers WHERE atelier_id = ? ORDER BY nom", (atelier_id,)).fetchall()
    return [dict(r) for r in rows]


def maj_couturier(atelier_id, couturier_id, nom, telephone, actif):
    with get_conn() as c:
        c.execute("UPDATE couturiers SET nom=?, telephone=?, actif=? WHERE id=? AND atelier_id=?",
                  (nom, telephone, 1 if actif else 0, couturier_id, atelier_id))


def reinitialiser_mdp_couturier(atelier_id, couturier_id, mot_de_passe):
    with get_conn() as c:
        c.execute("UPDATE couturiers SET mot_de_passe=? WHERE id=? AND atelier_id=?",
                  (generate_password_hash(mot_de_passe), couturier_id, atelier_id))


def supprimer_couturier(atelier_id, couturier_id):
    with get_conn() as c:
        c.execute("DELETE FROM couturiers WHERE id=? AND atelier_id=?", (couturier_id, atelier_id))


def email_existe(email):
    """True si l'e-mail est déjà pris (patron OU couturier)."""
    return bool(atelier_par_email(email) or couturier_par_email(email))


def connexion_unifiee(email, mot_de_passe):
    """Renvoie (type, utilisateur) où type = 'patron' ou 'couturier', sinon None."""
    a = atelier_par_email(email)
    if a and check_password_hash(a["mot_de_passe"], mot_de_passe):
        return ("patron", a)
    co = couturier_par_email(email)
    if co and co.get("actif") and check_password_hash(co["mot_de_passe"], mot_de_passe):
        return ("couturier", co)
    return None


# ---------------------------------------------------------------------------
#  CLIENTS
# ---------------------------------------------------------------------------
_SELECT_CLIENT = """
    SELECT cl.*, cu.nom AS couturier_nom
    FROM clients cl
    LEFT JOIN couturiers cu ON cu.id = cl.couturier_id
"""


def creer_client(atelier_id, nom, telephone, modele, mesures, notes, couturier_id=None):
    with get_conn() as c:
        cur = c.execute("""
            INSERT INTO clients (atelier_id, nom, telephone, modele, mesures, notes, couturier_id, cree_le)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (atelier_id, nom, telephone, modele, mesures, notes, couturier_id, _maintenant()))
        return cur.lastrowid


def lister_clients(atelier_id, recherche=None, visible_pour_couturier=None):
    """Liste les clients par ordre chronologique d'enregistrement
    (le plus récemment ajouté en premier).
    Si visible_pour_couturier est fourni, ne renvoie que les clients non
    assignés ou assignés à ce couturier (le patron voit toujours tout)."""
    q = _SELECT_CLIENT + " WHERE cl.atelier_id = ?"
    params = [atelier_id]
    if recherche:
        like = "%" + recherche + "%"
        q += " AND (cl.nom LIKE ? OR cl.telephone LIKE ?)"
        params += [like, like]
    if visible_pour_couturier:
        q += " AND (cl.couturier_id IS NULL OR cl.couturier_id = ?)"
        params.append(visible_pour_couturier)
    q += " ORDER BY cl.cree_le DESC, cl.id DESC"
    with get_conn() as c:
        rows = c.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def lire_client(atelier_id, client_id):
    with get_conn() as c:
        r = c.execute(_SELECT_CLIENT + " WHERE cl.id = ? AND cl.atelier_id = ?",
                      (client_id, atelier_id)).fetchone()
    return dict(r) if r else None


def maj_client(atelier_id, client_id, nom, telephone, modele, mesures, notes, couturier_id=None):
    with get_conn() as c:
        c.execute("""
            UPDATE clients SET nom=?, telephone=?, modele=?, mesures=?, notes=?, couturier_id=?
            WHERE id=? AND atelier_id=?
        """, (nom, telephone, modele, mesures, notes, couturier_id, client_id, atelier_id))


def supprimer_client(atelier_id, client_id):
    with get_conn() as c:
        c.execute("DELETE FROM clients WHERE id=? AND atelier_id=?", (client_id, atelier_id))


# ---------------------------------------------------------------------------
#  COMMANDES (+ chronomètre)
# ---------------------------------------------------------------------------
STATUTS = ["EN_ATTENTE", "COUPE", "COUTURE", "PRETE", "LIVREE"]
STATUTS_LABEL = {
    "EN_ATTENTE": "En attente", "COUPE": "Coupe", "COUTURE": "Couture",
    "PRETE": "Prête", "LIVREE": "Livrée",
}


_SELECT_CMD = """
    SELECT c.*, cl.nom AS client_nom, cl.telephone AS client_tel,
           cu.nom AS couturier_nom,
           COALESCE((SELECT SUM(p.montant) FROM paiements_commande p
                     WHERE p.commande_id = c.id), 0) AS verse
    FROM commandes c
    LEFT JOIN clients cl ON cl.id = c.client_id
    LEFT JOIN couturiers cu ON cu.id = c.couturier_id
"""


def _enrichir(row):
    d = dict(row)
    d["reste"] = max(0, int(d.get("prix") or 0) - int(d.get("verse") or 0))
    return d


def creer_commande(atelier_id, client_id, description, prix, acompte, date_livraison, couturier_id=None):
    with get_conn() as c:
        cur = c.execute("""
            INSERT INTO commandes
              (atelier_id, client_id, couturier_id, description, statut, prix, acompte,
               date_commande, date_livraison, chrono_secondes, cree_le)
            VALUES (?, ?, ?, ?, 'EN_ATTENTE', ?, ?, ?, ?, 0, ?)
        """, (atelier_id, client_id, couturier_id, description, prix, acompte,
              _aujourdhui(), date_livraison, _maintenant()))
        cmd_id = cur.lastrowid
        # L'acompte initial est enregistré comme premier versement
        if acompte and int(acompte) > 0:
            c.execute("""INSERT INTO paiements_commande
                         (atelier_id, commande_id, montant, moyen, note, par, cree_le)
                         VALUES (?, ?, ?, 'Acompte', 'Acompte à la commande', ?, ?)""",
                      (atelier_id, cmd_id, int(acompte), "", _maintenant()))
        return cmd_id


def lister_commandes(atelier_id, statut=None, client_id=None, couturier_id=None,
                     visible_pour_couturier=None):
    """couturier_id filtre strictement sur ce couturier (vue patron d'un
    couturier précis). visible_pour_couturier renvoie les commandes non
    assignées OU assignées à ce couturier (vue restreinte du couturier
    lui-même, qui partage le pool des commandes non assignées)."""
    q = _SELECT_CMD + " WHERE c.atelier_id = ?"
    params = [atelier_id]
    if statut:
        q += " AND c.statut = ?"; params.append(statut)
    if client_id:
        q += " AND c.client_id = ?"; params.append(client_id)
    if couturier_id:
        q += " AND c.couturier_id = ?"; params.append(couturier_id)
    if visible_pour_couturier:
        q += " AND (c.couturier_id IS NULL OR c.couturier_id = ?)"; params.append(visible_pour_couturier)
    # Les commandes non livrées restent prioritaires ; au sein de chaque groupe,
    # ordre chronologique d'enregistrement (la plus récente en premier).
    # id DESC départage les commandes enregistrées à la même seconde.
    q += " ORDER BY (c.statut='LIVREE'), c.cree_le DESC, c.id DESC"
    with get_conn() as c:
        rows = c.execute(q, params).fetchall()
    return [_enrichir(r) for r in rows]


def lire_commande(atelier_id, commande_id):
    with get_conn() as c:
        r = c.execute(_SELECT_CMD + " WHERE c.id = ? AND c.atelier_id = ?",
                      (commande_id, atelier_id)).fetchone()
    return _enrichir(r) if r else None


def maj_commande(atelier_id, commande_id, description, prix, date_livraison, couturier_id=None):
    with get_conn() as c:
        c.execute("""
            UPDATE commandes SET description=?, prix=?, date_livraison=?, couturier_id=?
            WHERE id=? AND atelier_id=?
        """, (description, prix, date_livraison, couturier_id, commande_id, atelier_id))


# --- Versements (paiements client sur une commande) ---
def ajouter_versement(atelier_id, commande_id, montant, moyen, note, par):
    with get_conn() as c:
        c.execute("""INSERT INTO paiements_commande
                     (atelier_id, commande_id, montant, moyen, note, par, cree_le)
                     VALUES (?, ?, ?, ?, ?, ?, ?)""",
                  (atelier_id, commande_id, int(montant), moyen, note, par, _maintenant()))


def lister_versements(atelier_id, commande_id):
    with get_conn() as c:
        rows = c.execute("""SELECT * FROM paiements_commande
                            WHERE atelier_id=? AND commande_id=? ORDER BY cree_le""",
                         (atelier_id, commande_id)).fetchall()
    return [dict(r) for r in rows]


def supprimer_versement(atelier_id, versement_id):
    with get_conn() as c:
        c.execute("DELETE FROM paiements_commande WHERE id=? AND atelier_id=?",
                  (versement_id, atelier_id))


def maj_statut_commande(atelier_id, commande_id, statut):
    if statut not in STATUTS:
        return
    with get_conn() as c:
        c.execute("UPDATE commandes SET statut=? WHERE id=? AND atelier_id=?",
                  (statut, commande_id, atelier_id))


def supprimer_commande(atelier_id, commande_id):
    with get_conn() as c:
        c.execute("DELETE FROM commandes WHERE id=? AND atelier_id=?", (commande_id, atelier_id))


def demarrer_chrono(atelier_id, commande_id):
    with get_conn() as c:
        c.execute("UPDATE commandes SET chrono_debut=? WHERE id=? AND atelier_id=? AND chrono_debut IS NULL",
                  (_maintenant(), commande_id, atelier_id))


def arreter_chrono(atelier_id, commande_id):
    """Ajoute le temps écoulé depuis le démarrage au total et arrête le chrono."""
    cmd = lire_commande(atelier_id, commande_id)
    if not cmd or not cmd.get("chrono_debut"):
        return
    try:
        debut = datetime.datetime.fromisoformat(cmd["chrono_debut"])
    except ValueError:
        debut = datetime.datetime.now()
    ecoule = int((datetime.datetime.now() - debut).total_seconds())
    total = int(cmd.get("chrono_secondes") or 0) + max(0, ecoule)
    with get_conn() as c:
        c.execute("UPDATE commandes SET chrono_secondes=?, chrono_debut=NULL WHERE id=? AND atelier_id=?",
                  (total, commande_id, atelier_id))


# ---------------------------------------------------------------------------
#  STOCK
# ---------------------------------------------------------------------------
def creer_article(atelier_id, article, categorie, quantite, unite, seuil):
    with get_conn() as c:
        cur = c.execute("""
            INSERT INTO stock (atelier_id, article, categorie, quantite, unite, seuil, cree_le)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (atelier_id, article, categorie, quantite, unite, seuil, _maintenant()))
        return cur.lastrowid


def lister_stock(atelier_id):
    with get_conn() as c:
        rows = c.execute(
            "SELECT * FROM stock WHERE atelier_id=? ORDER BY categorie, article", (atelier_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def ajuster_quantite(atelier_id, article_id, delta):
    with get_conn() as c:
        c.execute("""UPDATE stock SET quantite = MAX(0, quantite + ?)
                     WHERE id=? AND atelier_id=?""", (delta, article_id, atelier_id))


def maj_article(atelier_id, article_id, article, categorie, quantite, unite, seuil):
    with get_conn() as c:
        c.execute("""UPDATE stock SET article=?, categorie=?, quantite=?, unite=?, seuil=?
                     WHERE id=? AND atelier_id=?""",
                  (article, categorie, quantite, unite, seuil, article_id, atelier_id))


def supprimer_article(atelier_id, article_id):
    with get_conn() as c:
        c.execute("DELETE FROM stock WHERE id=? AND atelier_id=?", (article_id, atelier_id))


# ---------------------------------------------------------------------------
#  STATISTIQUES (tableau de bord)
# ---------------------------------------------------------------------------
def stats_atelier(atelier_id, couturier_id=None):
    """Si couturier_id est fourni, les statistiques liées aux commandes/clients
    sont limitées à ce qui est visible par ce couturier (non assigné ou
    assigné à lui). Le stock reste toujours global (visible par tous)."""
    debut_mois = datetime.date.today().replace(day=1).isoformat()
    dans_7j = (datetime.date.today() + datetime.timedelta(days=7)).isoformat()
    aujourdhui = _aujourdhui()
    cond = ""
    extra = []
    if couturier_id:
        cond = " AND (couturier_id IS NULL OR couturier_id = ?)"
        extra = [couturier_id]
    with get_conn() as c:
        actives = c.execute(
            "SELECT COUNT(*) n FROM commandes WHERE atelier_id=? AND statut!='LIVREE'" + cond,
            [atelier_id] + extra).fetchone()["n"]
        a_livrer = c.execute("""
            SELECT COUNT(*) n FROM commandes WHERE atelier_id=? AND statut!='LIVREE'
              AND date_livraison IS NOT NULL AND date_livraison!='' AND date_livraison <= ?
        """ + cond, [atelier_id, dans_7j] + extra).fetchone()["n"]
        en_retard = c.execute("""
            SELECT COUNT(*) n FROM commandes WHERE atelier_id=? AND statut!='LIVREE'
              AND date_livraison IS NOT NULL AND date_livraison!='' AND date_livraison < ?
        """ + cond, [atelier_id, aujourdhui] + extra).fetchone()["n"]
        ca_mois = c.execute("""
            SELECT COALESCE(SUM(prix),0) s FROM commandes
            WHERE atelier_id=? AND statut='LIVREE' AND date_commande >= ?
        """ + cond, [atelier_id, debut_mois] + extra).fetchone()["s"]
        nb_clients = c.execute(
            "SELECT COUNT(*) n FROM clients WHERE atelier_id=?" + cond,
            [atelier_id] + extra).fetchone()["n"]
        alertes = c.execute(
            "SELECT COUNT(*) n FROM stock WHERE atelier_id=? AND seuil>0 AND quantite<=seuil",
            (atelier_id,)).fetchone()["n"]
        reste = c.execute("""
            SELECT COALESCE(SUM(
                prix - COALESCE((SELECT SUM(montant) FROM paiements_commande p
                                 WHERE p.commande_id = commandes.id), 0)
            ), 0) AS r
            FROM commandes WHERE atelier_id=? AND statut!='LIVREE'
        """ + cond, [atelier_id] + extra).fetchone()["r"]
    return {
        "actives": actives, "a_livrer": a_livrer, "en_retard": en_retard,
        "ca_mois": ca_mois, "nb_clients": nb_clients, "alertes_stock": alertes,
        "reste_encaisser": max(0, int(reste or 0)),
    }


def totaux_commandes(commandes):
    """Somme (total à payer, versé, reste) sur une liste de commandes déjà enrichies."""
    total = sum(int(c.get("prix") or 0) for c in commandes)
    verse = sum(int(c.get("verse") or 0) for c in commandes)
    return {"total": total, "verse": verse, "reste": max(0, total - verse)}


def stats_couturier(atelier_id, couturier_id):
    with get_conn() as c:
        row = c.execute("""
            SELECT COUNT(*) AS nb,
                   COALESCE(SUM(CASE WHEN statut!='LIVREE' THEN 1 END),0) AS en_cours,
                   COALESCE(SUM(chrono_secondes),0) AS temps,
                   COALESCE(SUM(prix),0) AS ca
            FROM commandes WHERE atelier_id=? AND couturier_id=?
        """, (atelier_id, couturier_id)).fetchone()
    return dict(row)


# ---------------------------------------------------------------------------
#  TRANSACTIONS (paiements) — inchangé pour l'essentiel, + atelier_id / reçu
# ---------------------------------------------------------------------------
def enregistrer_transaction(infos):
    with get_conn() as c:
        c.execute("""
            INSERT OR REPLACE INTO transactions
              (transaction_id, atelier_id, atelier, nom, prenom, email, telephone,
               mois, montant, canal, statut, payment_token, recu_envoye, cree_le, paye_le)
            VALUES (:transaction_id, :atelier_id, :atelier, :nom, :prenom, :email, :telephone,
                    :mois, :montant, :canal, :statut, :payment_token, :recu_envoye, :cree_le, :paye_le)
        """, {
            "transaction_id": infos.get("transaction_id"),
            "atelier_id": infos.get("atelier_id"),
            "atelier": infos.get("atelier", ""),
            "nom": infos.get("nom", ""), "prenom": infos.get("prenom", ""),
            "email": infos.get("email", ""), "telephone": infos.get("telephone", ""),
            "mois": infos.get("mois", 1), "montant": infos.get("montant", 0),
            "canal": infos.get("canal", ""), "statut": infos.get("statut", "EN_ATTENTE"),
            "payment_token": infos.get("payment_token", ""),
            "recu_envoye": infos.get("recu_envoye", 0),
            "cree_le": infos.get("cree_le", _maintenant()),
            "paye_le": infos.get("paye_le"),
        })


def maj_statut(transaction_id, statut):
    paye_le = _maintenant() if statut == "PAYE" else None
    with get_conn() as c:
        c.execute("UPDATE transactions SET statut=?, paye_le=COALESCE(?, paye_le) WHERE transaction_id=?",
                  (statut, paye_le, transaction_id))


def marquer_recu_envoye(transaction_id):
    with get_conn() as c:
        c.execute("UPDATE transactions SET recu_envoye=1 WHERE transaction_id=?", (transaction_id,))


def lire_transaction(transaction_id):
    if not transaction_id:
        return None
    with get_conn() as c:
        r = c.execute("SELECT * FROM transactions WHERE transaction_id=?", (transaction_id,)).fetchone()
    return dict(r) if r else None


def lister_transactions(limit=200, atelier_id=None):
    with get_conn() as c:
        if atelier_id:
            rows = c.execute(
                "SELECT * FROM transactions WHERE atelier_id=? ORDER BY cree_le DESC LIMIT ?",
                (atelier_id, limit)).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM transactions ORDER BY cree_le DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def stats():
    with get_conn() as c:
        row = c.execute("""
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(CASE WHEN statut='PAYE' THEN 1 END),0) AS payes,
                   COALESCE(SUM(CASE WHEN statut='PAYE' THEN montant END),0) AS recette
            FROM transactions
        """).fetchone()
    return dict(row)
