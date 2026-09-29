# -*- coding: utf-8 -*-
"""
AtelierPro — Plateforme de gestion d'ateliers de couture

Contenu :
  - Landing page publique (/)
  - Paiement Paystack : Mobile Money + carte (/abonnement)
    (désactivé tant que PAIEMENT_ACTIF=0 : phase de test gratuite)
  - Espace de gestion (connexion requise) :
        /app                tableau de bord
        /app/clients        clients + mesures
        /app/commandes      commandes + chronomètre
        /app/stock          tissus & fournitures
  - Reçu par e-mail / WhatsApp après paiement (voir notifications.py)

Lancement :
    pip install -r requirements.txt
    python app.py            ->  http://127.0.0.1:5000

Base de données : SQLite (atelierpro.db), créée automatiquement (voir db.py).
Clés Paystack / SMTP / Twilio : voir .env.example
"""

import os
import re
import hmac
import time
import hashlib
import json
import uuid
import secrets
import datetime
import functools
from urllib.parse import urlparse, quote

import requests
from markupsafe import escape
from flask import (
    Flask, render_template, request, redirect, url_for, session, abort, flash
)

# Charge les variables d'un fichier .env s'il existe
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import db
import notifications

# Préparation de la base. En cas de problème de configuration (variable
# manquante, base injoignable...), l'application affiche une page explicite
# au lieu de planter (sur Vercel : "500 FUNCTION_INVOCATION_FAILED").
ERREUR_DEMARRAGE = db.CONFIG_ERREUR
if not ERREUR_DEMARRAGE:
    try:
        db.init_db()
    except Exception as e:  # le détail complet part dans les logs, pas sur la page
        import traceback
        traceback.print_exc()
        ERREUR_DEMARRAGE = ("Connexion à la base de données impossible (%s). Vérifiez "
                            "l'adresse DATABASE_URL (Neon) et que la base est active."
                            % type(e).__name__)

app = Flask(__name__)


@app.before_request
def verifier_demarrage():
    if ERREUR_DEMARRAGE:
        print("[AtelierPro] " + ERREUR_DEMARRAGE)
        return ("<!doctype html><meta charset='utf-8'><title>AtelierPro — maintenance</title>"
                "<div style='font-family:system-ui;max-width:560px;margin:80px auto;padding:0 16px'>"
                "<h1>Configuration incomplète</h1><p>%s</p></div>"
                % escape(ERREUR_DEMARRAGE)), 503

# --- Sécurité ----------------------------------------------------------------
# Mode debug uniquement si explicitement demandé (jamais en production :
# le débogueur Werkzeug permet d'exécuter du code à distance).
DEBUG = os.environ.get("FLASK_DEBUG", "0").strip().lower() in ("1", "true", "oui", "yes")

# Clé des sessions : sans SECRET_KEY, on génère une clé aléatoire (les sessions
# sont alors perdues à chaque redémarrage) plutôt qu'une valeur connue de tous.
_secret = os.environ.get("SECRET_KEY", "").strip()
if not _secret or _secret == "mettez_une_longue_chaine_aleatoire_ici":
    _secret = secrets.token_hex(32)
    print("[AtelierPro] ATTENTION : SECRET_KEY absente du .env — clé temporaire générée.")
app.secret_key = _secret

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Cookie envoyé uniquement en HTTPS dès que le site est en ligne en https://
    SESSION_COOKIE_SECURE=os.environ.get("BASE_URL", "").startswith("https://"),
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=7),
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)

# --- Coordonnées / paramètres ------------------------------------------------
# Les coordonnées et le tarif sont lus UNIQUEMENT depuis le fichier .env (non
# publié sur GitHub) : rien de personnel n'apparaît dans le code source.
# Un élément non renseigné est simplement masqué sur le site.
NOM_PLATEFORME = "AtelierPro"
WHATSAPP_NUM   = re.sub(r"\D", "", os.environ.get("CONTACT_WHATSAPP", ""))  # chiffres seuls
WHATSAPP_MSG   = "Bonjour, je souhaite essayer AtelierPro pour mon atelier de couture."
EMAIL_PRO      = os.environ.get("CONTACT_EMAIL", "").strip()
TEL_AFFICHE    = os.environ.get("CONTACT_TELEPHONE", "").strip()
SOCIETE        = os.environ.get("SOCIETE", "").strip()
VILLE          = os.environ.get("CONTACT_VILLE", "").strip()
try:
    PRIX_MENSUEL = max(0, int(os.environ.get("PRIX_MENSUEL", "0") or 0))
except ValueError:
    PRIX_MENSUEL = 0
PRIX = "{:,} F".format(PRIX_MENSUEL).replace(",", " ")

# --- Mode d'accès ------------------------------------------------------------
# PAIEMENT_ACTIF=0 (défaut) : phase de test, application en libre utilisation.
# Aucun abonnement n'est exigé ni affiché. Passez à 1 pour ouvrir les paiements.
PAIEMENT_ACTIF = os.environ.get("PAIEMENT_ACTIF", "0").strip().lower() in ("1", "true", "oui", "yes")

# --- Paystack ----------------------------------------------------------------
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY", "")
BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:5000")
PAYSTACK_INIT_URL   = "https://api.paystack.co/transaction/initialize"
PAYSTACK_VERIFY_URL = "https://api.paystack.co/transaction/verify/"
DEVISE = "XOF"
MODE_DEMO = not PAYSTACK_SECRET_KEY

CANAUX = {
    "ALL":          "Mobile Money + carte bancaire",
    "MOBILE_MONEY": "Mobile Money (Orange, MTN, Moov, Wave)",
    "CREDIT_CARD":  "Carte bancaire (Visa / Mastercard)",
}
# Canaux Paystack correspondants (None = tous ceux activés sur le compte)
CANAUX_PAYSTACK = {
    "ALL":          None,
    "MOBILE_MONEY": ["mobile_money"],
    "CREDIT_CARD":  ["card"],
}

# --- Super-admin (supervision de tous les ateliers) --------------------------
# Aucun mot de passe par défaut : sans SUPERADMIN_EMAIL + SUPERADMIN_PASSWORD
# dans le .env, la connexion super-admin est tout simplement désactivée.
SUPERADMIN_EMAIL = os.environ.get("SUPERADMIN_EMAIL", "").strip().lower()
SUPERADMIN_PASSWORD = os.environ.get("SUPERADMIN_PASSWORD", "")
if SUPERADMIN_PASSWORD == "un_mot_de_passe_solide":  # valeur d'exemple de .env.example
    SUPERADMIN_PASSWORD = ""
SUPERADMIN_ACTIF = bool(SUPERADMIN_EMAIL and SUPERADMIN_PASSWORD)
SUPERADMIN_DEFAUT = SUPERADMIN_ACTIF and len(SUPERADMIN_PASSWORD) < 12  # mot de passe trop faible

MDP_MIN = 8  # longueur minimale des mots de passe

# Clé d'accès à /admin/paiements (en plus de la session super-admin)
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
if ADMIN_KEY == "mot_de_passe_admin":  # valeur d'exemple de .env.example
    ADMIN_KEY = ""

# ----------------------------------------------------------------------------
WA_LINK = "https://wa.me/{num}?text={msg}".format(
    num=WHATSAPP_NUM, msg=quote(WHATSAPP_MSG)) if WHATSAPP_NUM else ""
MAILTO = "mailto:{email}?subject={sujet}".format(
    email=quote(EMAIL_PRO, safe="@.+-_"), sujet=quote("Contact " + NOM_PLATEFORME)) if EMAIL_PRO else ""
AVIS_MSG = "Bonjour, voici mon avis sur AtelierPro : "
# Avis : par WhatsApp si configuré, sinon par e-mail, sinon masqué.
if WHATSAPP_NUM:
    AVIS_LINK = "https://wa.me/{num}?text={msg}".format(num=WHATSAPP_NUM, msg=quote(AVIS_MSG))
elif EMAIL_PRO:
    AVIS_LINK = "mailto:{email}?subject={sujet}".format(
        email=quote(EMAIL_PRO, safe="@.+-_"), sujet=quote("Mon avis sur " + NOM_PLATEFORME))
else:
    AVIS_LINK = ""


def contexte_commun():
    return dict(
        plat=NOM_PLATEFORME, wa=WA_LINK, mailto=MAILTO, email=EMAIL_PRO,
        tel=TEL_AFFICHE, societe=SOCIETE, ville=VILLE, prix=PRIX,
        paiement_actif=PAIEMENT_ACTIF, avis_link=AVIS_LINK, jours_essai=db.JOURS_ESSAI,
    )


def parse_entier(valeur):
    """Convertit une saisie de montant en entier, en tolérant les espaces
    (normales ou insécables) et virgules utilisées comme séparateurs de
    milliers (ex. '100 000' ou '100,000' -> 100000)."""
    if valeur is None:
        return 0
    nettoye = re.sub(r"[\s ,]", "", str(valeur))
    try:
        return int(nettoye)
    except ValueError:
        return 0


def parse_decimal(valeur):
    """Équivalent de parse_entier pour les quantités décimales (stock)."""
    if valeur is None:
        return 0.0
    nettoye = re.sub(r"[\s ]", "", str(valeur)).replace(",", ".")
    try:
        return float(nettoye)
    except ValueError:
        return 0.0


# ===========================================================================
#  PROTECTIONS (CSRF, limitation des tentatives, en-têtes HTTP)
# ===========================================================================
# Routes appelées par des serveurs externes (pas de jeton CSRF possible) :
# elles vérifient elles-mêmes l'authenticité (signature Paystack).
CSRF_EXEMPTES = {"paiement_webhook"}


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def verifier_csrf():
    if request.method != "POST" or request.endpoint in CSRF_EXEMPTES:
        return
    attendu = session.get("_csrf")
    recu = request.form.get("csrf_token", "")
    if not attendu or not hmac.compare_digest(attendu, recu):
        abort(400, "Jeton de sécurité invalide ou expiré. Rechargez la page et réessayez.")


# Limitation simple des tentatives (en mémoire, par adresse IP) contre la
# force brute sur la connexion et la réinitialisation de mot de passe.
_TENTATIVES = {}
LIMITE_TENTATIVES = 10          # tentatives autorisées...
FENETRE_TENTATIVES = 15 * 60    # ... par fenêtre de 15 minutes


def trop_de_tentatives(action):
    cle = (action, request.remote_addr)
    maintenant = time.time()
    recentes = [t for t in _TENTATIVES.get(cle, []) if maintenant - t < FENETRE_TENTATIVES]
    recentes.append(maintenant)
    _TENTATIVES[cle] = recentes
    return len(recentes) > LIMITE_TENTATIVES


def reinitialiser_tentatives(action):
    _TENTATIVES.pop((action, request.remote_addr), None)


def url_interne(cible):
    """N'accepte que des chemins internes au site (évite les redirections
    ouvertes vers un site tiers, ex. ?suivant=https://pirate.com)."""
    if not cible:
        return None
    p = urlparse(cible)
    if p.scheme or p.netloc or not cible.startswith("/") or cible.startswith("//") \
            or "\\" in cible:
        return None
    return cible


@app.after_request
def entetes_securite(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if app.config.get("SESSION_COOKIE_SECURE"):
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    # Les pages connectées ne doivent pas rester dans le cache du navigateur.
    if session.get("user_id"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


# ===========================================================================
#  AUTHENTIFICATION
# ===========================================================================
def atelier_courant():
    aid = session.get("atelier_id")
    return db.atelier_par_id(aid) if aid else None


def utilisateur_courant():
    """Renvoie l'utilisateur connecté (patron ou couturier) sous forme de dict,
    ou None."""
    if not session.get("user_id"):
        return None
    return {
        "type": session.get("user_type"),        # 'patron' ou 'couturier'
        "id": session.get("user_id"),
        "nom": session.get("user_nom"),
        "atelier_id": session.get("atelier_id"),
        "est_patron": session.get("user_type") == "patron",
    }


def id_couturier_filtre():
    """Id du couturier connecté à utiliser comme filtre de visibilité
    (clients/commandes non assignés ou assignés à lui), ou None si l'utilisateur
    est le patron (qui voit tout sans restriction)."""
    u = utilisateur_courant()
    return u["id"] if u and not u["est_patron"] else None


def login_required(vue):
    @functools.wraps(vue)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("connexion", suivant=request.path))
        # Le super-admin n'a pas d'atelier : on le renvoie vers sa supervision.
        if session.get("user_type") == "superadmin":
            return redirect(url_for("superadmin_dashboard"))
        return vue(*args, **kwargs)
    return wrapper


def patron_required(vue):
    @functools.wraps(vue)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("connexion", suivant=request.path))
        if session.get("user_type") != "patron":
            abort(403)
        return vue(*args, **kwargs)
    return wrapper


def superadmin_required(vue):
    @functools.wraps(vue)
    def wrapper(*args, **kwargs):
        if session.get("user_type") != "superadmin":
            return redirect(url_for("connexion", suivant=request.path))
        return vue(*args, **kwargs)
    return wrapper


def _connecter(user_type, user):
    session.clear()  # nouvelle session à chaque connexion (anti fixation de session)
    session.permanent = True
    session["user_type"] = user_type
    session["user_id"] = user["id"]
    session["user_nom"] = user.get("responsable") or user.get("nom")
    if user_type == "superadmin":
        session["atelier_id"] = None
    else:
        session["atelier_id"] = user["id"] if user_type == "patron" else user["atelier_id"]


@app.context_processor
def injecter_atelier():
    a = atelier_courant()
    u = utilisateur_courant()
    return dict(
        atelier=a, utilisateur=u,
        est_patron=(u["est_patron"] if u else False),
        # Phase de test : tout le monde est considéré comme actif.
        abo_actif=(db.abo_actif(a) if a else False) if PAIEMENT_ACTIF else True,
        jours_restants=db.jours_restants(a) if a else 0,
        paiement_actif=PAIEMENT_ACTIF, avis_link=AVIS_LINK,
        STATUTS=db.STATUTS, STATUTS_LABEL=db.STATUTS_LABEL,
    )


@app.route("/inscription", methods=["GET", "POST"])
def inscription():
    if request.method == "POST":
        f = request.form
        email = f.get("email", "").strip().lower()
        if db.email_existe(email):
            flash("Un compte existe déjà avec cet e-mail.", "erreur")
        elif not (f.get("nom") and email and f.get("mot_de_passe")):
            flash("Merci de remplir les champs obligatoires.", "erreur")
        elif len(f.get("mot_de_passe")) < MDP_MIN:
            flash("Le mot de passe doit contenir au moins %d caractères." % MDP_MIN, "erreur")
        else:
            aid = db.creer_atelier(
                nom=f.get("nom").strip(), responsable=f.get("responsable", "").strip(),
                email=email, telephone=f.get("telephone", "").strip(),
                mot_de_passe=f.get("mot_de_passe"))
            _connecter("patron", db.atelier_par_id(aid))
            if PAIEMENT_ACTIF and db.JOURS_ESSAI:
                flash("Bienvenue ! Votre essai gratuit de %s jours a commencé." % db.JOURS_ESSAI, "ok")
            elif PAIEMENT_ACTIF:
                flash("Bienvenue ! Réglez votre abonnement pour commencer à utiliser %s."
                      % NOM_PLATEFORME, "ok")
            else:
                flash("Bienvenue ! %s est gratuit pendant la phase de test. "
                      "Vos retours nous aident à l'améliorer." % NOM_PLATEFORME, "ok")
            return redirect(url_for("tableau_bord"))
    return render_template("inscription.html", **contexte_commun())


@app.route("/connexion", methods=["GET", "POST"])
def connexion():
    if request.method == "POST":
        if trop_de_tentatives("connexion"):
            flash("Trop de tentatives. Réessayez dans quelques minutes.", "erreur")
            return render_template("connexion.html", **contexte_commun()), 429
        email = request.form.get("email", "").strip().lower()
        mdp = request.form.get("mot_de_passe", "")
        # 1) super-admin (désactivé si non configuré dans .env)
        if (SUPERADMIN_ACTIF and hmac.compare_digest(email, SUPERADMIN_EMAIL)
                and hmac.compare_digest(mdp.encode(), SUPERADMIN_PASSWORD.encode())):
            reinitialiser_tentatives("connexion")
            _connecter("superadmin", {"id": "super", "nom": SOCIETE or "Super-admin"})
            return redirect(url_for("superadmin_dashboard"))
        # 2) patron ou couturier
        res = db.connexion_unifiee(email, mdp)
        if res:
            reinitialiser_tentatives("connexion")
            _connecter(res[0], res[1])
            return redirect(url_interne(request.args.get("suivant")) or url_for("tableau_bord"))
        flash("E-mail ou mot de passe incorrect.", "erreur")
    return render_template("connexion.html", **contexte_commun())


@app.route("/deconnexion")
def deconnexion():
    session.clear()
    return redirect(url_for("index"))


@app.route("/mot-de-passe-oublie", methods=["GET", "POST"])
def mot_de_passe_oublie():
    lien_demo = None
    if request.method == "POST":
        if trop_de_tentatives("reset"):
            flash("Trop de demandes. Réessayez dans quelques minutes.", "erreur")
            return render_template("mot_de_passe_oublie.html", lien_demo=None,
                                   **contexte_commun()), 429
        email = request.form.get("email", "").strip().lower()
        resultat = db.generer_reset_token(email)
        if resultat:
            type_compte, user, token = resultat
            lien = BASE_URL.rstrip("/") + url_for("reinitialiser_mdp", token=token)
            corps = (
                "Bonjour,\n\nVoici votre lien pour réinitialiser votre mot de passe %s "
                "(valable %d minute(s)) :\n%s\n\n"
                "Si vous n'êtes pas à l'origine de cette demande, ignorez cet e-mail."
            ) % (NOM_PLATEFORME, db.RESET_VALIDITE_MIN, lien)
            notifications.envoyer_email(
                email, "Réinitialisation de votre mot de passe %s" % NOM_PLATEFORME, corps)
            # Sans SMTP, le lien n'est affiché qu'en développement local (FLASK_DEBUG=1) :
            # en ligne, cela permettrait à n'importe qui de prendre le contrôle d'un compte.
            if not notifications.SMTP_HOST and DEBUG:
                lien_demo = lien
        flash("Si un compte existe avec cet e-mail, un lien de réinitialisation vient d'être envoyé.", "ok")
    return render_template("mot_de_passe_oublie.html", lien_demo=lien_demo, **contexte_commun())


@app.route("/reinitialiser/<token>", methods=["GET", "POST"])
def reinitialiser_mdp(token):
    res = db.verifier_reset_token(token)
    if not res:
        flash("Ce lien de réinitialisation est invalide ou a expiré.", "erreur")
        return redirect(url_for("mot_de_passe_oublie"))
    type_compte, user = res
    if request.method == "POST":
        mdp = request.form.get("mot_de_passe", "")
        confirmation = request.form.get("confirmation", "")
        if len(mdp) < MDP_MIN:
            flash("Le mot de passe doit contenir au moins %d caractères." % MDP_MIN, "erreur")
        elif mdp != confirmation:
            flash("Les deux mots de passe ne correspondent pas.", "erreur")
        else:
            db.reinitialiser_mot_de_passe(type_compte, user["id"], mdp)
            flash("Mot de passe mis à jour. Vous pouvez vous connecter.", "ok")
            return redirect(url_for("connexion"))
    return render_template("reinitialiser_mdp.html", token=token, **contexte_commun())


# ===========================================================================
#  LANDING PAGE
# ===========================================================================
@app.route("/")
def index():
    return render_template("index.html", **contexte_commun())


# ===========================================================================
#  ESPACE DE GESTION
# ===========================================================================
@app.route("/app")
@login_required
def tableau_bord():
    a = atelier_courant()
    cid = id_couturier_filtre()
    stats = db.stats_atelier(a["id"], couturier_id=cid)
    en_cours = [c for c in db.lister_commandes(a["id"], visible_pour_couturier=cid)
                if c["statut"] != "LIVREE"]
    # Sur le tableau de bord, priorité aux livraisons les plus proches
    # (indépendamment de l'ordre chronologique d'enregistrement de la liste complète).
    urgentes = sorted(en_cours, key=lambda c: c["date_livraison"] or "9999-99-99")[:6]
    return render_template("app/dashboard.html", stats=stats, urgentes=urgentes,
                           **contexte_commun())


# ---- CLIENTS ----
@app.route("/app/clients")
@login_required
def clients():
    a = atelier_courant()
    u = utilisateur_courant()
    recherche = request.args.get("q", "").strip()
    liste = db.lister_clients(a["id"], recherche or None, visible_pour_couturier=id_couturier_filtre())
    for cl in liste:
        cl["_mesures"] = json.loads(cl.get("mesures") or "{}")
    couturiers_liste = db.lister_couturiers(a["id"]) if u["est_patron"] else []
    return render_template("app/clients.html", clients=liste, recherche=recherche,
                           mesures_def=db.mesures_atelier(a), couturiers=couturiers_liste,
                           **contexte_commun())


@app.route("/app/clients/nouveau", methods=["POST"])
@login_required
def client_nouveau():
    a = atelier_courant()
    u = utilisateur_courant()
    f = request.form
    mesures_def = db.mesures_atelier(a)
    mesures = {k: f.get("m_" + k, "").strip() for k, _ in mesures_def if f.get("m_" + k, "").strip()}
    # Le couturier s'attribue le client ; le patron choisit qui le suit (ou laisse non assigné).
    couturier_id = (f.get("couturier_id") or None) if u["est_patron"] else u["id"]
    db.creer_client(a["id"], f.get("nom", "").strip(), f.get("telephone", "").strip(),
                    f.get("modele", "").strip(), json.dumps(mesures, ensure_ascii=False),
                    f.get("notes", "").strip(), couturier_id)
    flash("Client ajouté.", "ok")
    return redirect(url_for("clients"))


@app.route("/app/clients/<int:client_id>")
@login_required
def client_detail(client_id):
    a = atelier_courant()
    u = utilisateur_courant()
    cl = db.lire_client(a["id"], client_id)
    if not cl:
        abort(404)
    if not u["est_patron"] and cl.get("couturier_id") not in (None, u["id"]):
        abort(403)
    cl["_mesures"] = json.loads(cl.get("mesures") or "{}")
    commandes = db.lister_commandes(a["id"], client_id=client_id, visible_pour_couturier=id_couturier_filtre())
    couturiers_liste = db.lister_couturiers(a["id"]) if u["est_patron"] else []
    return render_template("app/client_detail.html", c=cl, commandes=commandes,
                           mesures_def=db.mesures_atelier(a), couturiers=couturiers_liste,
                           **contexte_commun())


@app.route("/app/clients/<int:client_id>/modifier", methods=["POST"])
@login_required
def client_modifier(client_id):
    a = atelier_courant()
    u = utilisateur_courant()
    actuel = db.lire_client(a["id"], client_id)
    if not actuel:
        abort(404)
    if not u["est_patron"] and actuel.get("couturier_id") not in (None, u["id"]):
        abort(403)
    f = request.form
    mesures_def = db.mesures_atelier(a)
    mesures = {k: f.get("m_" + k, "").strip() for k, _ in mesures_def if f.get("m_" + k, "").strip()}
    # Seul le patron peut réattribuer un client ; un couturier garde l'attribution en l'état.
    couturier_id = (f.get("couturier_id") or None) if u["est_patron"] else actuel.get("couturier_id")
    db.maj_client(a["id"], client_id, f.get("nom", "").strip(), f.get("telephone", "").strip(),
                  f.get("modele", "").strip(), json.dumps(mesures, ensure_ascii=False),
                  f.get("notes", "").strip(), couturier_id)
    flash("Fiche client mise à jour.", "ok")
    return redirect(url_for("client_detail", client_id=client_id))


@app.route("/app/clients/<int:client_id>/supprimer", methods=["POST"])
@patron_required
def client_supprimer(client_id):
    a = atelier_courant()
    db.supprimer_client(a["id"], client_id)
    flash("Client supprimé.", "ok")
    return redirect(url_for("clients"))


# ---- PARAMÈTRES : mesures personnalisables (réservé au patron) ----
@app.route("/app/parametres/mesures")
@patron_required
def parametres_mesures():
    a = atelier_courant()
    return render_template("app/parametres_mesures.html", mesures=db.mesures_atelier(a),
                           **contexte_commun())


@app.route("/app/parametres/mesures/renommer", methods=["POST"])
@patron_required
def parametres_mesures_renommer():
    a = atelier_courant()
    cles = request.form.getlist("cle")
    libelles = request.form.getlist("libelle")
    mesures = [(k, lib.strip()) for k, lib in zip(cles, libelles) if lib.strip()]
    db.maj_mesures_atelier(a["id"], mesures)
    flash("Libellés des mesures mis à jour.", "ok")
    return redirect(url_for("parametres_mesures"))


@app.route("/app/parametres/mesures/ajouter", methods=["POST"])
@patron_required
def parametres_mesures_ajouter():
    a = atelier_courant()
    libelle = request.form.get("libelle", "").strip()
    if libelle:
        db.ajouter_mesure(a["id"], a, libelle)
        flash("Mesure « %s » ajoutée." % libelle, "ok")
    return redirect(url_for("parametres_mesures"))


@app.route("/app/parametres/mesures/<cle>/supprimer", methods=["POST"])
@patron_required
def parametres_mesures_supprimer(cle):
    a = atelier_courant()
    db.supprimer_mesure(a["id"], a, cle)
    flash("Mesure supprimée.", "ok")
    return redirect(url_for("parametres_mesures"))


@app.route("/app/parametres/mesures/reinitialiser", methods=["POST"])
@patron_required
def parametres_mesures_reinitialiser():
    a = atelier_courant()
    db.reinitialiser_mesures(a["id"])
    flash("Liste des mesures réinitialisée par défaut.", "ok")
    return redirect(url_for("parametres_mesures"))


# ---- COMMANDES ----
def _commande_accessible(a, u, commande_id):
    """Récupère la commande et vérifie qu'elle est visible par l'utilisateur
    (patron : toujours ; couturier : non assignée ou assignée à lui).
    Interrompt la requête (404/403) sinon."""
    cmd = db.lire_commande(a["id"], commande_id)
    if not cmd:
        abort(404)
    if not u["est_patron"] and cmd.get("couturier_id") not in (None, u["id"]):
        abort(403)
    return cmd


@app.route("/app/commandes")
@login_required
def commandes():
    a = atelier_courant()
    cid = id_couturier_filtre()
    statut = request.args.get("statut") or None
    liste = db.lister_commandes(a["id"], statut=statut, visible_pour_couturier=cid)
    maintenant = datetime.datetime.now()
    for c in liste:
        base = int(c.get("chrono_secondes") or 0)
        c["_en_cours"] = bool(c.get("chrono_debut"))
        if c["_en_cours"]:
            try:
                debut = datetime.datetime.fromisoformat(c["chrono_debut"])
                base += max(0, int((maintenant - debut).total_seconds()))
            except ValueError:
                pass
        c["_base"] = base
    clients_liste = db.lister_clients(a["id"], visible_pour_couturier=cid)
    couturiers_liste = db.lister_couturiers(a["id"])
    totaux = db.totaux_commandes(liste)
    return render_template("app/commandes.html", commandes=liste, clients=clients_liste,
                           couturiers=couturiers_liste, totaux=totaux,
                           statut_filtre=statut, **contexte_commun())


@app.route("/app/commandes/<int:commande_id>")
@login_required
def commande_detail(commande_id):
    a = atelier_courant()
    u = utilisateur_courant()
    cmd = _commande_accessible(a, u, commande_id)
    versements = db.lister_versements(a["id"], commande_id)
    couturiers_liste = db.lister_couturiers(a["id"])
    return render_template("app/commande_detail.html", c=cmd, versements=versements,
                           couturiers=couturiers_liste, **contexte_commun())


@app.route("/app/commandes/<int:commande_id>/versement", methods=["POST"])
@login_required
def commande_versement(commande_id):
    a = atelier_courant()
    u = utilisateur_courant()
    _commande_accessible(a, u, commande_id)
    montant = parse_entier(request.form.get("montant"))
    if montant > 0:
        db.ajouter_versement(a["id"], commande_id, montant,
                             request.form.get("moyen", "").strip(),
                             request.form.get("note", "").strip(), u["nom"])
        flash("Versement enregistré : %s F." % ("{:,}".format(montant).replace(",", " ")), "ok")
    return redirect(url_for("commande_detail", commande_id=commande_id))


@app.route("/app/commandes/<int:commande_id>/versement/<int:versement_id>/supprimer", methods=["POST"])
@patron_required
def versement_supprimer(commande_id, versement_id):
    a = atelier_courant()
    db.supprimer_versement(a["id"], versement_id)
    flash("Versement supprimé.", "ok")
    return redirect(url_for("commande_detail", commande_id=commande_id))


@app.route("/app/commandes/<int:commande_id>/modifier", methods=["POST"])
@login_required
def commande_modifier(commande_id):
    a = atelier_courant()
    u = utilisateur_courant()
    actuelle = _commande_accessible(a, u, commande_id)
    f = request.form
    prix = parse_entier(f.get("prix"))
    couturier_id = (f.get("couturier_id") or None) if u["est_patron"] else actuelle.get("couturier_id")
    db.maj_commande(a["id"], commande_id, f.get("description", "").strip(), prix,
                    f.get("date_livraison", "").strip(), couturier_id)
    flash("Commande mise à jour.", "ok")
    return redirect(url_for("commande_detail", commande_id=commande_id))


@app.route("/app/commandes/nouvelle", methods=["POST"])
@login_required
def commande_nouvelle():
    a = atelier_courant()
    f = request.form
    prix = parse_entier(f.get("prix"))
    acompte = parse_entier(f.get("acompte"))
    client_id = f.get("client_id") or None
    u = utilisateur_courant()
    # Le couturier s'attribue la commande ; le patron choisit qui la gère.
    if u["est_patron"]:
        couturier_id = f.get("couturier_id") or None
    else:
        couturier_id = u["id"]
        if client_id:
            cl = db.lire_client(a["id"], client_id)
            if not cl or cl.get("couturier_id") not in (None, u["id"]):
                abort(403)
    db.creer_commande(a["id"], client_id, f.get("description", "").strip(),
                      prix, acompte, f.get("date_livraison", "").strip(), couturier_id)
    flash("Commande créée.", "ok")
    return redirect(url_for("commandes"))


@app.route("/app/commandes/<int:commande_id>/statut", methods=["POST"])
@login_required
def commande_statut(commande_id):
    a = atelier_courant()
    u = utilisateur_courant()
    _commande_accessible(a, u, commande_id)
    db.maj_statut_commande(a["id"], commande_id, request.form.get("statut", ""))
    return redirect(request.referrer or url_for("commandes"))


@app.route("/app/commandes/<int:commande_id>/chrono", methods=["POST"])
@login_required
def commande_chrono(commande_id):
    a = atelier_courant()
    u = utilisateur_courant()
    _commande_accessible(a, u, commande_id)
    if request.form.get("action") == "start":
        db.demarrer_chrono(a["id"], commande_id)
    else:
        db.arreter_chrono(a["id"], commande_id)
    return redirect(request.referrer or url_for("commandes"))


@app.route("/app/commandes/<int:commande_id>/supprimer", methods=["POST"])
@patron_required
def commande_supprimer(commande_id):
    a = atelier_courant()
    db.supprimer_commande(a["id"], commande_id)
    flash("Commande supprimée.", "ok")
    return redirect(url_for("commandes"))


# ---- STOCK ----
@app.route("/app/stock")
@login_required
def stock():
    a = atelier_courant()
    liste = db.lister_stock(a["id"])
    return render_template("app/stock.html", stock=liste, **contexte_commun())


@app.route("/app/stock/nouveau", methods=["POST"])
@login_required
def stock_nouveau():
    a = atelier_courant()
    f = request.form
    quantite = parse_decimal(f.get("quantite"))
    seuil = parse_decimal(f.get("seuil"))
    db.creer_article(a["id"], f.get("article", "").strip(), f.get("categorie", "").strip(),
                     quantite, f.get("unite", "").strip(), seuil)
    flash("Article ajouté au stock.", "ok")
    return redirect(url_for("stock"))


@app.route("/app/stock/<int:article_id>/ajuster", methods=["POST"])
@login_required
def stock_ajuster(article_id):
    a = atelier_courant()
    delta = parse_decimal(request.form.get("delta"))
    db.ajuster_quantite(a["id"], article_id, delta)
    return redirect(url_for("stock"))


@app.route("/app/stock/<int:article_id>/supprimer", methods=["POST"])
@patron_required
def stock_supprimer(article_id):
    a = atelier_courant()
    db.supprimer_article(a["id"], article_id)
    return redirect(url_for("stock"))


# ---- COUTURIERS (réservé au patron) ----
@app.route("/app/couturiers")
@patron_required
def couturiers():
    a = atelier_courant()
    liste = db.lister_couturiers(a["id"])
    for co in liste:
        co["_stats"] = db.stats_couturier(a["id"], co["id"])
    return render_template("app/couturiers.html", couturiers=liste, **contexte_commun())


@app.route("/app/couturiers/nouveau", methods=["POST"])
@patron_required
def couturier_nouveau():
    a = atelier_courant()
    f = request.form
    email = f.get("email", "").strip().lower()
    if db.email_existe(email):
        flash("Cet e-mail est déjà utilisé.", "erreur")
    elif not (f.get("nom") and email and f.get("mot_de_passe")):
        flash("Nom, e-mail et mot de passe sont obligatoires.", "erreur")
    elif len(f.get("mot_de_passe")) < MDP_MIN:
        flash("Le mot de passe doit contenir au moins %d caractères." % MDP_MIN, "erreur")
    else:
        db.creer_couturier(a["id"], f.get("nom").strip(), email,
                           f.get("telephone", "").strip(), f.get("mot_de_passe"))
        flash("Compte couturier créé. Communiquez-lui son e-mail et son mot de passe.", "ok")
    return redirect(url_for("couturiers"))


@app.route("/app/couturiers/<int:couturier_id>")
@patron_required
def couturier_detail(couturier_id):
    a = atelier_courant()
    co = db.couturier_par_id(couturier_id)
    if not co or co["atelier_id"] != a["id"]:
        abort(404)
    activites = db.lister_commandes(a["id"], couturier_id=couturier_id)
    stats = db.stats_couturier(a["id"], couturier_id)
    totaux = db.totaux_commandes(activites)
    return render_template("app/couturier_detail.html", co=co, activites=activites,
                           stats=stats, totaux=totaux, **contexte_commun())


@app.route("/app/couturiers/<int:couturier_id>/modifier", methods=["POST"])
@patron_required
def couturier_modifier(couturier_id):
    a = atelier_courant()
    f = request.form
    db.maj_couturier(a["id"], couturier_id, f.get("nom", "").strip(),
                     f.get("telephone", "").strip(), f.get("actif") == "1")
    if f.get("mot_de_passe") and len(f.get("mot_de_passe")) < MDP_MIN:
        flash("Compte mis à jour, mais le mot de passe n'a pas été changé : "
              "au moins %d caractères requis." % MDP_MIN, "erreur")
    elif f.get("mot_de_passe"):
        db.reinitialiser_mdp_couturier(a["id"], couturier_id, f.get("mot_de_passe"))
        flash("Compte mis à jour (nouveau mot de passe défini).", "ok")
    else:
        flash("Compte mis à jour.", "ok")
    return redirect(url_for("couturier_detail", couturier_id=couturier_id))


@app.route("/app/couturiers/<int:couturier_id>/supprimer", methods=["POST"])
@patron_required
def couturier_supprimer(couturier_id):
    a = atelier_courant()
    db.supprimer_couturier(a["id"], couturier_id)
    flash("Compte couturier supprimé.", "ok")
    return redirect(url_for("couturiers"))


# ===========================================================================
#  SUPER-ADMIN — supervision de tous les ateliers
# ===========================================================================
@app.route("/superadmin")
@superadmin_required
def superadmin_dashboard():
    return render_template("superadmin/dashboard.html",
                           stats=db.stats_globales(),
                           ateliers=db.lister_ateliers(),
                           paiements=db.lister_transactions(limit=12),
                           mdp_defaut=SUPERADMIN_DEFAUT,
                           **contexte_commun())


@app.route("/superadmin/atelier/<int:atelier_id>")
@superadmin_required
def superadmin_atelier(atelier_id):
    a = db.atelier_par_id(atelier_id)
    if not a:
        abort(404)
    a["actif"] = db.abo_actif(a)
    a["jours_restants"] = db.jours_restants(a)
    return render_template("superadmin/atelier.html", a=a,
                           stats=db.stats_atelier(atelier_id),
                           couturiers=db.lister_couturiers(atelier_id),
                           paiements=db.lister_transactions(limit=30, atelier_id=atelier_id),
                           **contexte_commun())


@app.route("/superadmin/atelier/<int:atelier_id>/abonnement", methods=["POST"])
@superadmin_required
def superadmin_abonnement(atelier_id):
    if not db.atelier_par_id(atelier_id):
        abort(404)
    action = request.form.get("action")
    if action == "prolonger":
        try:
            mois = max(1, min(24, int(request.form.get("mois", "1"))))
        except ValueError:
            mois = 1
        db.prolonger_abonnement(atelier_id, mois)
        flash("Abonnement prolongé de %s mois." % mois, "ok")
    elif action == "suspendre":
        db.suspendre_abonnement(atelier_id)
        flash("Accès suspendu (abonnement expiré immédiatement).", "ok")
    elif action == "date" and request.form.get("date_fin"):
        db.definir_abo_fin(atelier_id, request.form.get("date_fin"))
        flash("Date de fin d'abonnement mise à jour.", "ok")
    return redirect(url_for("superadmin_atelier", atelier_id=atelier_id))


# ===========================================================================
#  PAIEMENT (Paystack) — Mobile Money + carte
# ===========================================================================
def _paystack_headers():
    return {"Authorization": "Bearer %s" % PAYSTACK_SECRET_KEY,
            "Content-Type": "application/json"}


@app.route("/abonnement")
def abonnement():
    return render_template("abonnement.html", prix_mensuel=PRIX_MENSUEL, canaux=CANAUX,
                           mode_demo=MODE_DEMO, **contexte_commun())


@app.route("/payer", methods=["POST"])
def payer():
    if not PAIEMENT_ACTIF:
        return redirect(url_for("abonnement"))
    f = request.form
    try:
        mois = max(1, min(12, int(f.get("mois", "1"))))
    except ValueError:
        mois = 1
    canal = f.get("canal", "ALL")
    if canal not in CANAUX:
        canal = "ALL"

    montant = PRIX_MENSUEL * mois
    transaction_id = "APRO-" + uuid.uuid4().hex[:16].upper()

    a = atelier_courant()  # si connecté, le paiement prolonge SON abonnement
    infos = {
        "transaction_id": transaction_id,
        "atelier_id": a["id"] if a else None,
        "atelier": (a["nom"] if a else f.get("atelier", "").strip()),
        "nom": f.get("nom", "").strip(), "prenom": f.get("prenom", "").strip(),
        "email": f.get("email", "").strip(), "telephone": f.get("telephone", "").strip(),
        "mois": mois, "montant": montant, "canal": canal, "statut": "EN_ATTENTE",
        "recu_envoye": 0, "cree_le": datetime.datetime.now().isoformat(timespec="seconds"),
        "paye_le": None,
    }
    db.enregistrer_transaction(infos)
    session["transaction_id"] = transaction_id

    if MODE_DEMO:
        return render_template("paiement_retour.html", demo=True, infos=infos, statut="DEMO",
                               notif=None, **contexte_commun())

    payload = {
        "email": infos["email"],
        "amount": montant * 100,  # Paystack attend le montant en sous-unité
        "currency": DEVISE,
        "reference": transaction_id,
        "callback_url": BASE_URL.rstrip("/") + "/paiement/retour",
        "metadata": {
            "atelier": infos["atelier"], "atelier_id": infos["atelier_id"], "mois": mois,
            "telephone": infos["telephone"],
            "custom_fields": [
                {"display_name": "Atelier", "variable_name": "atelier", "value": infos["atelier"]},
                {"display_name": "Durée", "variable_name": "mois", "value": "%s mois" % mois},
            ],
        },
    }
    if CANAUX_PAYSTACK[canal]:
        payload["channels"] = CANAUX_PAYSTACK[canal]
    try:
        r = requests.post(PAYSTACK_INIT_URL, json=payload, headers=_paystack_headers(), timeout=20)
        data = r.json()
    except Exception as e:
        return render_template("paiement_retour.html", demo=False, infos=infos, statut="ERREUR",
                               message="Impossible de contacter Paystack : %s" % e,
                               notif=None, **contexte_commun())

    if data.get("status") and (data.get("data") or {}).get("authorization_url"):
        infos["payment_token"] = data["data"].get("access_code", "")
        db.enregistrer_transaction(infos)
        return redirect(data["data"]["authorization_url"])

    db.maj_statut(transaction_id, "ECHOUE")
    return render_template("paiement_retour.html", demo=False, infos=infos, statut="ERREUR",
                           message="Paystack a refusé l'initialisation : %s" % data.get("message"),
                           notif=None, **contexte_commun())


def verifier_transaction(transaction_id):
    """Interroge Paystack et renvoie 'REUSSI', 'EN_ATTENTE', 'ECHOUE' ou None
    (indisponible). Le montant et la devise sont contrôlés côté serveur."""
    if MODE_DEMO or not transaction_id:
        return None
    try:
        r = requests.get(PAYSTACK_VERIFY_URL + transaction_id,
                         headers=_paystack_headers(), timeout=20)
        data = r.json()
    except Exception:
        return None
    if not data.get("status"):
        return None
    d = data.get("data") or {}
    etat = d.get("status", "")
    if etat == "success":
        infos = db.lire_transaction(transaction_id)
        attendu = (infos or {}).get("montant", 0) * 100
        if not infos or d.get("amount") != attendu or d.get("currency") != DEVISE:
            return "ECHOUE"
        return "REUSSI"
    if etat in ("ongoing", "pending", "processing", "queued"):
        return "EN_ATTENTE"
    return "ECHOUE"


def traiter_paiement_reussi(transaction_id):
    """Actions à faire UNE SEULE FOIS quand un paiement est confirmé :
    prolonger l'abonnement + envoyer le reçu (e-mail / WhatsApp)."""
    infos = db.lire_transaction(transaction_id)
    if not infos or infos.get("recu_envoye"):
        return None
    db.maj_statut(transaction_id, "PAYE")
    if infos.get("atelier_id"):
        db.prolonger_abonnement(infos["atelier_id"], infos.get("mois") or 1)
    infos["paye_le"] = datetime.datetime.now().isoformat(timespec="seconds")
    notif = notifications.envoyer_recu(infos)
    db.marquer_recu_envoye(transaction_id)
    return notif


@app.route("/paiement/retour", methods=["GET", "POST"])
def paiement_retour():
    # Paystack renvoie ?reference=...&trxref=... sur la callback_url
    transaction_id = (request.values.get("reference")
                      or request.values.get("trxref")
                      or session.get("transaction_id"))
    infos = db.lire_transaction(transaction_id) or {"transaction_id": transaction_id}
    statut, notif = "INCONNU", None
    if MODE_DEMO:
        statut = "DEMO"
    else:
        resultat = verifier_transaction(transaction_id)
        if resultat == "REUSSI":
            statut = "REUSSI"
            notif = traiter_paiement_reussi(transaction_id)
            infos = db.lire_transaction(transaction_id) or infos
        elif resultat == "EN_ATTENTE":
            statut = "EN_ATTENTE"
        elif resultat == "ECHOUE":
            statut = "ECHOUE"
            if infos.get("statut") != "PAYE":
                db.maj_statut(transaction_id, "ECHOUE")
    return render_template("paiement_retour.html", demo=MODE_DEMO, infos=infos,
                           statut=statut, message=None, notif=notif, **contexte_commun())


@app.route("/paiement/webhook", methods=["POST"])
def paiement_webhook():
    """Webhook Paystack (à déclarer dans Paystack > Settings > API Keys & Webhooks).
    La signature HMAC-SHA512 du corps brut est vérifiée avec la clé secrète."""
    if MODE_DEMO:
        return "demo", 200
    brut = request.get_data()
    attendue = hmac.new(PAYSTACK_SECRET_KEY.encode(), brut, hashlib.sha512).hexdigest()
    if not hmac.compare_digest(attendue, request.headers.get("X-Paystack-Signature", "")):
        return "signature invalide", 401
    evt = request.get_json(silent=True) or {}
    if evt.get("event") == "charge.success":
        reference = (evt.get("data") or {}).get("reference")
        # On re-vérifie auprès de l'API plutôt que de faire confiance au contenu.
        if reference and verifier_transaction(reference) == "REUSSI":
            traiter_paiement_reussi(reference)
    return "ok", 200


# ===========================================================================
#  ADMINISTRATION (paiements)
# ===========================================================================
@app.route("/admin/paiements")
def admin_paiements():
    # Accès : session super-admin, OU clé ADMIN_KEY (si elle est définie).
    # Sans ADMIN_KEY, seule la session super-admin ouvre cette page.
    par_cle = bool(ADMIN_KEY) and hmac.compare_digest(
        request.args.get("cle", "").encode(), ADMIN_KEY.encode())
    if session.get("user_type") != "superadmin" and not par_cle:
        abort(404)
    return render_template("admin.html", transactions=db.lister_transactions(),
                           totaux=db.stats(), **contexte_commun())


# --- Filtres d'affichage -----------------------------------------------------
@app.template_filter("fcfa")
def fcfa(n):
    try:
        return "{:,}".format(int(n)).replace(",", " ")
    except (ValueError, TypeError):
        return n


@app.template_filter("duree")
def duree(secondes):
    try:
        s = int(secondes or 0)
    except (ValueError, TypeError):
        s = 0
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    if h:
        return "%d h %02d" % (h, m)
    if m:
        return "%d min" % m
    return "%d s" % s


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=DEBUG)
