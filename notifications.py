# -*- coding: utf-8 -*-
"""
Envoi de reçus après paiement : par e-mail (SMTP) et/ou WhatsApp (Twilio).

Tout est optionnel et se configure par variables d'environnement (.env).
Si rien n'est configuré, les fonctions ne plantent pas : elles renvoient
simplement un statut "non configuré", et un lien "click-to-send" WhatsApp
reste toujours disponible en repli.
"""

import os
import ssl
import smtplib
from email.message import EmailMessage
from urllib.parse import quote

import requests


# --- Configuration e-mail (SMTP) --------------------------------------------
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT") or "465")
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_FROM = os.environ.get("MAIL_FROM") or SMTP_USER

# --- Configuration WhatsApp (Twilio) ----------------------------------------
TWILIO_SID   = os.environ.get("TWILIO_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_TOKEN", "")
TWILIO_FROM  = os.environ.get("TWILIO_WHATSAPP_FROM", "")  # ex. whatsapp:+14155238886

NOM_PLATEFORME = os.environ.get("NOM_PLATEFORME", "AtelierPro")
SOCIETE = os.environ.get("SOCIETE", "")


def _fmt(n):
    try:
        return "{:,}".format(int(n)).replace(",", " ")
    except (ValueError, TypeError):
        return str(n)


def texte_recu(infos):
    """Construit le texte du reçu à partir d'une transaction."""
    lignes = [
        "Reçu de paiement — %s" % NOM_PLATEFORME,
        "",
        "Atelier    : %s" % (infos.get("atelier") or "-"),
        "Client     : %s %s" % (infos.get("prenom") or "", infos.get("nom") or ""),
        "Abonnement : %s mois" % (infos.get("mois") or 1),
        "Montant    : %s F CFA" % _fmt(infos.get("montant")),
        "Référence  : %s" % (infos.get("transaction_id") or "-"),
        "Date       : %s" % (infos.get("paye_le") or infos.get("cree_le") or "-"),
        "",
        "Merci pour votre confiance.",
        "%s" % SOCIETE,
    ]
    return "\n".join(lignes)


# --- E-mail -----------------------------------------------------------------
def envoyer_email(destinataire, sujet, corps):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and destinataire):
        return {"ok": False, "raison": "non configuré ou pas d'adresse e-mail"}
    msg = EmailMessage()
    msg["Subject"] = sujet
    msg["From"] = MAIL_FROM
    msg["To"] = destinataire
    msg.set_content(corps)
    try:
        contexte = ssl.create_default_context()
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=contexte, timeout=20) as s:
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        else:  # 587 STARTTLS
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
                s.starttls(context=contexte)
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "raison": str(e)}


# --- WhatsApp (Twilio) ------------------------------------------------------
def _normaliser_wa(numero):
    """Transforme un numéro en identifiant WhatsApp Twilio (whatsapp:+225...)."""
    if not numero:
        return ""
    n = numero.strip().replace(" ", "").replace("-", "")
    if n.startswith("00"):
        n = "+" + n[2:]
    if not n.startswith("+"):
        # numéro local ivoirien : préfixer +225 par défaut
        n = "+225" + n.lstrip("0") if len(n) <= 10 else "+" + n
    return "whatsapp:" + n


def envoyer_whatsapp(numero, corps):
    if not (TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM and numero):
        return {"ok": False, "raison": "non configuré ou pas de numéro"}
    url = "https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json" % TWILIO_SID
    data = {"From": TWILIO_FROM, "To": _normaliser_wa(numero), "Body": corps}
    try:
        r = requests.post(url, data=data, auth=(TWILIO_SID, TWILIO_TOKEN), timeout=20)
        if r.status_code in (200, 201):
            return {"ok": True}
        return {"ok": False, "raison": "Twilio %s: %s" % (r.status_code, r.text[:200])}
    except Exception as e:
        return {"ok": False, "raison": str(e)}


def lien_whatsapp(numero, corps):
    """Lien 'click-to-send' — repli qui marche toujours, sans compte Twilio.
    À ouvrir pour envoyer manuellement le reçu au client."""
    n = (numero or "").strip().replace(" ", "").replace("-", "").replace("+", "")
    return "https://wa.me/%s?text=%s" % (n, quote(corps))


# --- Point d'entrée : envoie le reçu par les canaux configurés ---------------
def envoyer_recu(infos):
    """Envoie le reçu par e-mail et WhatsApp (selon ce qui est configuré).
    Renvoie un récapitulatif de ce qui a été fait."""
    corps = texte_recu(infos)
    resultat = {
        "email": envoyer_email(
            infos.get("email"), "Reçu %s — %s F CFA" % (
                NOM_PLATEFORME, _fmt(infos.get("montant"))), corps),
        "whatsapp": envoyer_whatsapp(infos.get("telephone"), corps),
        "lien_whatsapp": lien_whatsapp(infos.get("telephone"), corps),
    }
    return resultat
