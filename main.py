import os
import json
import base64
import hmac
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path
import psycopg2
import psycopg2.extras
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI, OpenAIError
from dotenv import load_dotenv

load_dotenv()

# =========================
# CONFIG
# =========================

APP_VERSION = "1.3.0"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

DEFAULT_MODEL_PLAN_1 = "gpt-4.1-mini"
DEFAULT_MODEL_PLAN_2 = "gpt-4.1-mini"
DEFAULT_MODEL_PLAN_3 = "gpt-4.1"

MAX_TOKENS_BY_PLAN = {1: 2000, 2: 3000, 3: 3500}

SHOPIFY_WEBHOOK_SECRET = os.getenv("SHOPIFY_WEBHOOK_SECRET", "")

DATABASE_URL = os.getenv("DATABASE_URL")

# Variant IDs des produits Ads Shopify
VARIANT_ADS_PLAN_1 = "58089123873116"  # Plan Essentielle Ads 3,90€
VARIANT_ADS_PLAN_2 = "58089137897820"  # Plan Ciblée Plateforme Ads 7,90€
VARIANT_ADS_PLAN_3 = "58089147138396"  # Plan Avancée Persona Ads 14,90€
# CORRIGÉ (24/07/2026) : l'ancien ID "15681853358428" était erroné, ce qui
# empêchait le webhook et la génération de codes de reconnaître les achats
# vidéo. Le bon ID a été retrouvé via /products/{handle}.js le 23/07/2026.
VARIANT_ADS_VIDEO = "58317837205852"  # Plan Vidéo Ads 14,90€

# =========================
# BASE DE DONNÉES
# =========================

def get_db_connection():
    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="DATABASE_URL manquante.")
    conn = psycopg2.connect(DATABASE_URL)
    return conn


def init_db():
    """Crée la table commandes_ads si elle n'existe pas encore.

    NOTE (nettoyage juillet 2026) : cette table sert désormais uniquement
    d'historique/traçabilité des commandes reçues via webhook. Le quota
    quantite/analyses_utilisees n'est plus consommé nulle part (l'ancien
    système de vérification par numéro de commande a été retiré, remplacé
    par les codes d'activation Ads par unité). On garde l'écriture pour ne
    pas perdre l'historique, mais ces colonnes ne pilotent plus rien.
    """
    if not DATABASE_URL:
        print("DATABASE_URL manquante, base de données non initialisée.")
        return
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS commandes_ads (
                order_number       TEXT PRIMARY KEY,
                email              TEXT NOT NULL,
                plan               INTEGER NOT NULL,
                quantite           INTEGER NOT NULL DEFAULT 1,
                analyses_utilisees INTEGER NOT NULL DEFAULT 0,
                customer_id        TEXT
            )
        """)
        # La base existe déjà en prod avec l'ancien schéma (sans customer_id) :
        # ADD COLUMN IF NOT EXISTS permet d'ajouter la colonne sans casser les
        # lignes existantes (customer_id restera NULL pour les vieilles commandes
        # et pour tout achat effectué sans être connecté à un compte client).
        cur.execute("ALTER TABLE commandes_ads ADD COLUMN IF NOT EXISTS customer_id TEXT")
        cur.execute("ALTER TABLE codes_ads_activation ADD COLUMN IF NOT EXISTS customer_id TEXT")
        conn.commit()
        cur.close()
        conn.close()
        print("Table commandes_ads prête.")
    except Exception as e:
        print(f"Erreur initialisation base de données Ads : {e}")

# =========================
# CODES D'ACTIVATION ADS
# =========================

import random
import string


def generer_code_ads() -> str:
    """Génère un code lisible du type MNA-A3F92K (pas de 0/O ni 1/I pour éviter la confusion)."""
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    suffixe = "".join(random.choices(alphabet, k=6))
    return f"MNA-{suffixe}"


def generer_codes_ads_pour_commande(order_number: str, email: str, line_items: list, customer_id: Optional[str] = None) -> None:
    if not DATABASE_URL:
        print("DATABASE_URL manquante, génération de codes Ads ignorée.")
        return

    conn = get_db_connection()
    cur = conn.cursor()

    # Protection anti-duplication : Shopify peut renvoyer le même webhook
    # plusieurs fois (retry). Si des codes existent déjà pour cette commande,
    # on ne régénère rien.
    cur.execute("SELECT COUNT(*) FROM codes_ads_activation WHERE order_number = %s", (order_number,))
    (nb_codes_existants,) = cur.fetchone()
    if nb_codes_existants > 0:
        print(f"Commande #{order_number} : {nb_codes_existants} code(s) déjà généré(s), webhook dupliqué ignoré.")
        cur.close()
        conn.close()
        return

    codes_generes = []
    try:
        for item in line_items:
            variant_id = str(item.get("variant_id", ""))
            quantite = int(item.get("quantity", 1))

            if variant_id == VARIANT_ADS_VIDEO:
                plan_item = 4
            elif variant_id == VARIANT_ADS_PLAN_2:
                plan_item = 2
            elif variant_id == VARIANT_ADS_PLAN_3:
                plan_item = 3
            elif variant_id == VARIANT_ADS_PLAN_1:
                plan_item = 1
            else:
                continue

            for _ in range(max(quantite, 1)):
                code = generer_code_ads()
                cur.execute(
                    """
                    INSERT INTO codes_ads_activation (code, order_number, plan, email_client, utilise, customer_id)
                    VALUES (%s, %s, %s, %s, FALSE, %s)
                    ON CONFLICT (code) DO NOTHING;
                    """,
                    (code, order_number, plan_item, email, customer_id),
                )
                codes_generes.append({"code": code, "plan": plan_item})

        conn.commit()
        cur.close()
        conn.close()
        print(f"Codes d'activation Ads générés pour la commande #{order_number}.")

        if codes_generes:
            send_codes_ads_by_email(email, order_number, codes_generes)

    except Exception as e:
        cur.close()
        conn.close()
        print(f"Erreur génération codes Ads pour commande #{order_number} : {e}")
def send_codes_ads_by_email(email: str, order_number: str, codes: list) -> None:
    plan_names = {
        1: "Plan Essentielle Ads",
        2: "Plan Ciblée Plateforme Ads",
        3: "Plan Avancée Persona Ads",
        4: "Plan Vidéo Ads",  # CORRIGÉ (24/07/2026) : manquait, le plan 4 s'affichait comme "Plan 4"
    }
    RESEND_API_KEY = os.getenv("RESEND_API_KEY")
    if not RESEND_API_KEY:
        print("RESEND_API_KEY manquante, email de codes Ads non envoyé.")
        return

    import resend as resend_client
    resend_client.api_key = RESEND_API_KEY

    lignes_codes = ""
    for item in codes:
        nom_plan = plan_names.get(item["plan"], f"Plan {item['plan']}")
        lignes_codes += f"""
        <tr>
          <td style="background-color:#f2f2f2;border-radius:10px;padding:14px 16px;">
            <p style="margin:0;font-size:12px;font-weight:700;color:#f4a261;text-transform:uppercase;">{nom_plan}</p>
            <p style="margin:4px 0 0 0;font-size:22px;font-weight:900;color:#1d3557;letter-spacing:0.04em;">{item['code']}</p>
          </td>
        </tr>
        <tr><td style="height:10px;"></td></tr>
        """

    try:
        resend_client.Emails.send({
            "from": "MayNov <rapport@maynov.fr>",
            "to": email,
            "subject": f"Vos codes d'activation MayNov Ads — Commande #{order_number}",
            "html": f"""
<div style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto;padding:20px;">
  <div style="background:#1d3557;padding:16px 24px;border-radius:12px;margin-bottom:24px;">
    <span style="color:white;font-size:20px;font-weight:900;">MAY<span style="color:#8fd19e;">NOV</span> <span style="color:#f4a261;font-size:14px;">ADS</span></span>
  </div>

  <table width="100%" cellpadding="0" cellspacing="0" border="0">
    <tr>
      <td align="center" style="padding-bottom:8px;">
        <div style="font-size:36px;line-height:1;">✅</div>
      </td>
    </tr>
    <tr>
      <td align="center" style="padding-bottom:8px;">
        <h2 style="margin:0;font-size:24px;font-weight:900;color:#1d3557;">Votre commande est confirmée</h2>
      </td>
    </tr>
    <tr>
      <td align="center" style="padding-bottom:24px;">
        <p style="margin:0;font-size:14px;color:#475569;line-height:1.6;">Merci pour votre achat ! Voici {'vos codes' if len(codes) > 1 else 'votre code'} d'activation, un par analyse commandée.<br>Utilisez chacun d'eux pour lancer l'analyse correspondante.</p>
      </td>
    </tr>
  </table>

  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px;">
    {lignes_codes}
  </table>

  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:16px;">
    <tr>
      <td align="center">
        <a href="https://maynov.fr/pages/analyse-ads"
           style="display:inline-block;background-color:#f19450;color:#ffffff;text-decoration:none;font-size:16px;font-weight:900;padding:16px 40px;border-radius:14px;">
          Lancer mon analyse →
        </a>
      </td>
    </tr>
  </table>

  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:28px;">
    <tr>
      <td align="center">
        <p style="margin:0;font-size:12px;color:#94a3b8;">Conservez cet email — vos codes vous seront demandés pour lancer vos analyses</p>
      </td>
    </tr>
  </table>

  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px;">
    <tr>
      <td style="border-top:1px solid #e5e7eb;"></td>
    </tr>
  </table>

  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:24px;">
    <tr>
      <td width="33%" align="center" style="padding:16px 8px;background-color:#f2f2f2;border-radius:12px;">
        <p style="margin:0 0 4px 0;font-size:18px;">🔒</p>
        <p style="margin:0;font-size:11px;font-weight:700;color:#1d3557;">Paiement sécurisé</p>
      </td>
      <td width="4px"></td>
      <td width="33%" align="center" style="padding:16px 8px;background-color:#f2f2f2;border-radius:12px;">
        <p style="margin:0 0 4px 0;font-size:18px;">⚡</p>
        <p style="margin:0;font-size:11px;font-weight:700;color:#1d3557;">Analyse en 30 secondes</p>
      </td>
      <td width="4px"></td>
      <td width="33%" align="center" style="padding:16px 8px;background-color:#f2f2f2;border-radius:12px;">
        <p style="margin:0 0 4px 0;font-size:18px;">🛡️</p>
        <p style="margin:0;font-size:11px;font-weight:700;color:#1d3557;">Sans accès admin</p>
      </td>
    </tr>
  </table>

  <p style="color:#475569;font-size:13px;text-align:center;">Des questions ? Contactez-nous à <a href="mailto:contact@maynov.fr">contact@maynov.fr</a></p>
  <p style="color:#94a3b8;font-size:11px;text-align:center;">© 2026 MayNov · maynov.fr</p>
</div>
            """,
        })
        print(f"Email de codes Ads envoyé à {email} pour la commande #{order_number}")
    except Exception as e:
        print(f"Erreur envoi email codes Ads Resend : {e}")

client: Optional[OpenAI] = None
if OPENAI_API_KEY:
    client = OpenAI(api_key=OPENAI_API_KEY)

# =========================
# FASTAPI APP
# =========================

app = FastAPI(title="MayNov Ads Backend", version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup():
    init_db()

# =========================
# REQUEST MODEL
# =========================

class AdsAnalyseRequest(BaseModel):
    image_base64: str
    image_type: str = "image/jpeg"
    plateforme: Optional[str] = None  # "meta" ou "tiktok"
    persona: Optional[str] = None

# =========================
# CONFIG UPLOAD (IMAGE)
# =========================

MAX_FILE_SIZE_MB = 10
ALLOWED_CONTENT_TYPES = ["image/jpeg", "image/jpg", "image/png"]


async def read_and_encode_image(file: UploadFile) -> tuple[str, str]:
    """
    Reçoit un fichier uploadé, vérifie son type et sa taille,
    puis l'encode en base64 pour l'envoyer à GPT-4o Vision.
    Retourne (image_base64, image_type)
    """
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Format non supporté ({file.content_type}). Formats acceptés : JPG, PNG."
        )

    contents = await file.read()
    size_mb = len(contents) / (1024 * 1024)

    if size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(
            status_code=400,
            detail=f"Image trop lourde ({size_mb:.1f} Mo). Taille max autorisée : {MAX_FILE_SIZE_MB} Mo."
        )

    if size_mb == 0:
        raise HTTPException(status_code=400, detail="Le fichier envoyé est vide.")

    image_base64 = base64.b64encode(contents).decode("utf-8")
    return image_base64, file.content_type

# =========================
# BLOC COMMUN — VÉRIFICATION STRUCTURELLE + VOIX D'EXPERT
# (injecté dans les 6 prompts d'analyse image ci-dessous)
# =========================

BLOC_VERIFICATION_STRUCTURELLE = """
VÉRIFICATION STRUCTURELLE OBLIGATOIRE — À FAIRE EN PREMIER, AVANT TOUTE ANALYSE PSYCHOLOGIQUE OU DE TON

Avant de rédiger la moindre observation, identifie objectivement si les éléments suivants sont visibles sur CE visuel précis :
- Un CTA visuellement distinct (bouton, forme cliquable, contraste marqué avec le reste)
- Le prix ou une indication de valeur
- Un élément de réassurance (garantie, avis, certification, preuve sociale, retour)
- Un texte principal réellement lisible en un coup d'œil (taille, contraste, hiérarchie)

Si un de ces éléments est ABSENT, tu ne dois jamais l'interpréter à la place de la cliente ni le noyer dans une reformulation de ton ou de message. Formule-le comme une question ouverte, dans le ton d'un consultant senior qui ne prétend pas connaître la stratégie qu'il n'a pas sous les yeux. Exemple de formulation à adapter à chaque cas concret :
"Le prix n'apparaît pas sur ce visuel — est-ce un choix volontaire (stratégie de curiosité, prix révélé sur la landing page) ou un oubli ? Si c'est involontaire, c'est un frein direct à la conversion."
Cette question doit apparaître dans la section concernée (cta_analyse pour le CTA, clarte_message pour la lisibilité, etc.) ET, si l'absence constitue un frein potentiellement bloquant pour la conversion, être reprise explicitement en priorité 1 des recommandations.

EXIGENCE DE VOIX D'EXPERT — NON NÉGOCIABLE

Tu écris comme un consultant senior d'une grande agence, avec une connaissance approfondie et concrète de la conversion publicitaire — pas comme un générateur de texte marketing. Chaque section doit apporter une observation qu'aucune autre section du rapport ne fait. Il est interdit de reformuler un même constat sous un angle légèrement différent dans plusieurs sections pour donner une impression de volume : une section courte et tranchante vaut toujours mieux qu'une section qui délaye une idée déjà énoncée ailleurs. Avant de valider une phrase, vérifie qu'elle n'est pas une reformulation d'une observation déjà faite dans une section précédente.

L'objectif final de toute l'analyse, à chaque section, est unique : déterminer si ce visuel donne envie de s'arrêter, d'en savoir plus, et d'acquérir le produit. Toute observation doit être reliée explicitement à cet objectif de conversion, pas traitée comme un critère isolé.
""".strip()

# =========================
# PROMPTS — ANALYSE IMAGE
# =========================

PROMPT_ADS_PLAN_1 = f"""
Tu es un expert en création publicitaire et en optimisation de visuels e-commerce.

IMPORTANT : tu dois répondre au format JSON STRICT (et rien d'autre).
Le JSON doit contenir une clé "rapport_sections".

Ton objectif : analyser ce visuel publicitaire point par point et identifier ce qui fonctionne ou freine sa performance.
Ton : direct, concret, orienté action. Jamais condescendant.

{BLOC_VERIFICATION_STRUCTURELLE}

RÈGLES ABSOLUES :
- Zéro invention : chaque observation cite un élément RÉEL visible sur l'image
- Zéro conseil générique : chaque recommandation est liée à un élément identifié
- Pas de markdown, pas d'emojis, pas de hashtags dans le JSON
- Minimum 2 phrases réelles et spécifiques par clé texte

Structure — 6 sections dans cet ordre exact :

1) accroche_visuelle
Ce qui capte l'œil en moins d'une seconde.
- Quel est l'élément dominant ? (produit, texte, visage, couleur, mouvement)
- Cet élément dominant sert-il le message ou le dilue-t-il ?
- Le contraste, la composition et le point focal sont-ils efficaces ?

2) clarte_message
Le message est-il compris sans effort ?
- La promesse est-elle lisible en 3 secondes ?
- Y a-t-il trop d'informations en compétition ?
- La hiérarchie texte/visuel guide-t-elle la lecture ou la brouille ?

3) cta_analyse
Analyse du call-to-action.
- Le CTA est-il visible et lisible ?
- Son positionnement dans la composition est-il efficace ?
- Sa formulation est-elle claire et incitative ?
- Si absent : pose la question ouverte prévue dans la vérification structurelle, n'affirme jamais un choix à la place de la cliente.

4) coherence_marque
Cohérence de l'identité visuelle.
- Les couleurs, la typographie et le style sont-ils cohérents entre eux ?
- La pub dégage-t-elle une identité claire ou un mélange confus ?
- L'impression générale est-elle professionnelle, amateur ou confuse ?

5) recommandations
3 priorités d'amélioration classées par impact réel sur la conversion.

INTERDICTION ABSOLUE DE DÉFAUT : ne propose "ajouter un témoignage/preuve sociale", "rassurer sur le confort/la praticité" ou "ajouter un visuel avant/après" QUE si tu as explicitement identifié dans les sections précédentes que cet élément précis manque ET que c'est le frein principal pour CE produit. Ces 3 idées sont interdites par défaut car trop génériques.

À la place, cherche en priorité des leviers spécifiques à ce visuel et ce produit : composition, contraste, choix typographique, ordre de lecture, formulation exacte du texte, choix de l'image principale, couleur du CTA, longueur du message, élément structurel manquant identifié dans la vérification structurelle.

Format OBLIGATOIRE :
"Quoi: [action précise et spécifique à ce visuel]\\nPourquoi: [impact sur la conversion]\\nComment: [étapes]\\nOù: [emplacement]\\nExemple: [concret]"

6) resume_rapide
"Points forts: ...\\nPoints faibles: ...\\nPar où commencer: ..."

JSON attendu :
{{
  "rapport_sections": {{
    "accroche_visuelle": "...",
    "clarte_message": "...",
    "cta_analyse": "...",
    "coherence_marque": "...",
    "recommandations": {{
      "priorite_1": "Quoi: ...\\nPourquoi: ...\\nComment: ...\\nOù: ...\\nExemple: ...",
      "priorite_2": "Quoi: ...\\nPourquoi: ...\\nComment: ...\\nOù: ...\\nExemple: ...",
      "priorite_3": "Quoi: ...\\nPourquoi: ...\\nComment: ...\\nOù: ...\\nExemple: ..."
    }},
    "resume_rapide": "Points forts: ...\\nPoints faibles: ...\\nPar où commencer: ..."
  }}
}}
"""

PROMPT_ADS_PLAN_2_META = f"""
Tu es un expert en création publicitaire et en performance des publicités Meta (Facebook et Instagram).

IMPORTANT : tu dois répondre au format JSON STRICT (et rien d'autre).
Le JSON doit contenir une clé "rapport_sections".

Ton objectif : analyser ce visuel publicitaire en tenant compte des codes créatifs spécifiques à Meta.
Ton : stratégique, direct, ancré dans les réalités de la plateforme. Jamais condescendant.

{BLOC_VERIFICATION_STRUCTURELLE}

RÈGLES ABSOLUES :
- Chaque section doit être ancrée dans les codes et pratiques de Meta
- Zéro invention : chaque observation cite un élément RÉEL visible sur l'image
- Pas de markdown, pas d'emojis, pas de hashtags dans le JSON
- Minimum 2 phrases réelles et spécifiques par clé texte

Structure — 7 sections dans cet ordre exact :

1) accroche_visuelle
Ce qui capte l'œil en moins d'une seconde dans un fil Meta.
- L'élément dominant stoppe-t-il le scroll sur Meta ?
- Le contraste et la composition sont-ils adaptés à un environnement de fil d'actualité chargé ?

2) clarte_message
Le message est-il compris sans effort sur Meta ?
- La promesse est-elle lisible en 3 secondes sur mobile ?
- La hiérarchie texte/visuel est-elle adaptée à la lecture rapide sur Meta ?

3) cta_analyse
Analyse du CTA dans le contexte Meta.
- Le CTA est-il visible et lisible sur mobile ?
- Est-il formulé dans un registre qui performe sur Meta (direct, bénéfice immédiat) ?
- Si absent : pose la question ouverte prévue dans la vérification structurelle, n'affirme jamais un choix à la place de la cliente.

4) coherence_marque
Cohérence de l'identité visuelle.
- Les couleurs, la typographie et le style sont-ils cohérents ?
- L'impression générale est-elle professionnelle et digne de confiance sur Meta ?

5) codes_meta
Codes créatifs spécifiques à Meta.
- Ce visuel respecte-t-il les codes qui performent sur Meta (authenticité, preuve sociale, bénéfice immédiat) ?
- Le style est-il adapté au format Feed, Reels ou Stories ?
- Quels signaux de confiance sont présents ou manquants pour ce contexte Meta ?
- Le visuel présente-t-il un risque de modération Meta (formulations santé/beauté trop affirmatives type "anti-âge", "réduit", "traite") ? Si oui, cite précisément la formulation à risque.

6) recommandations
3 priorités adaptées à Meta, classées par impact réel sur la conversion.

INTERDICTION ABSOLUE DE DÉFAUT : ne propose "ajouter un témoignage/preuve sociale", "rassurer sur le confort/la praticité" ou "ajouter un visuel avant/après" QUE si tu as explicitement identifié dans les sections précédentes que cet élément précis manque ET que c'est le frein principal pour CE produit. Ces 3 idées sont interdites par défaut car trop génériques.

À la place, cherche en priorité des leviers spécifiques à ce visuel et ce produit : composition, contraste, choix typographique, ordre de lecture, formulation exacte du texte, choix de l'image principale, couleur du CTA, longueur du message, élément structurel manquant identifié dans la vérification structurelle (pas un élément générique e-commerce).

Format OBLIGATOIRE pour chaque priorité :
"Quoi: [action précise et spécifique à ce visuel]\\nPourquoi: [impact sur Meta et sur la conversion]\\nComment: [étapes concrètes]\\nOù: [emplacement sur le visuel]\\nExemple: [adapté aux codes Meta]"

7) resume_rapide
"Points forts: ...\\nPoints faibles: ...\\nPar où commencer: ..."

JSON attendu :
{{
  "rapport_sections": {{
    "accroche_visuelle": "...",
    "clarte_message": "...",
    "cta_analyse": "...",
    "coherence_marque": "...",
    "codes_meta": "...",
    "recommandations": {{
      "priorite_1": "Quoi: ...\\nPourquoi: ...\\nComment: ...\\nOù: ...\\nExemple: ...",
      "priorite_2": "Quoi: ...\\nPourquoi: ...\\nComment: ...\\nOù: ...\\nExemple: ...",
      "priorite_3": "Quoi: ...\\nPourquoi: ...\\nComment: ...\\nOù: ...\\nExemple: ..."
    }},
    "resume_rapide": "Points forts: ...\\nPoints faibles: ...\\nPar où commencer: ..."
  }}
}}
"""

PROMPT_ADS_PLAN_2_TIKTOK = f"""
Tu es un expert en création publicitaire et en performance des publicités TikTok.

IMPORTANT : tu dois répondre au format JSON STRICT (et rien d'autre).
Le JSON doit contenir une clé "rapport_sections".

Ton objectif : analyser ce visuel publicitaire en tenant compte des codes créatifs spécifiques à TikTok.
Ton : stratégique, direct, ancré dans les réalités de la plateforme. Jamais condescendant.

{BLOC_VERIFICATION_STRUCTURELLE}

RÈGLES ABSOLUES :
- Chaque section doit être ancrée dans les codes et pratiques de TikTok
- Zéro invention : chaque observation cite un élément RÉEL visible sur l'image
- Pas de markdown, pas d'emojis, pas de hashtags dans le JSON
- Minimum 2 phrases réelles et spécifiques par clé texte

Structure — 7 sections dans cet ordre exact :

1) accroche_visuelle
Ce qui capte l'œil en moins d'une seconde dans un fil TikTok.
- L'élément dominant stoppe-t-il le scroll sur TikTok ?
- Le style est-il natif TikTok ou trop "publicitaire" pour la plateforme ?

2) clarte_message
Le message est-il compris sans effort sur TikTok ?
- La promesse est-elle lisible en 3 secondes sur mobile format vertical ?
- La hiérarchie texte/visuel est-elle adaptée aux codes de lecture TikTok ?

3) cta_analyse
Analyse du CTA dans le contexte TikTok.
- Le CTA est-il visible dans le format vertical mobile ?
- Est-il formulé dans un registre TikTok (curiosité, FOMO, communauté) ?
- Si absent : pose la question ouverte prévue dans la vérification structurelle, n'affirme jamais un choix à la place de la cliente.

4) coherence_marque
Cohérence de l'identité visuelle.
- Les couleurs, la typographie et le style sont-ils cohérents ?
- L'impression générale est-elle authentique et adaptée à TikTok ?

5) codes_tiktok
Codes créatifs spécifiques à TikTok.
- Ce visuel respecte-t-il les codes qui performent sur TikTok (authenticité, UGC, dynamisme, storytelling rapide) ?
- Le style est-il natif à la plateforme ou trop poli/corporate pour TikTok ?
- Quels éléments TikTok-natifs sont présents ou manquants (texte superposé, style UGC, ambiance raw) ?

6) recommandations
3 priorités adaptées à TikTok, classées par impact réel sur la conversion.

INTERDICTION ABSOLUE DE DÉFAUT : ne propose "ajouter un témoignage/preuve sociale", "rassurer sur le confort/la praticité" ou "ajouter un visuel avant/après" QUE si tu as explicitement identifié dans les sections précédentes que cet élément précis manque ET que c'est le frein principal pour CE produit. Ces 3 idées sont interdites par défaut car trop génériques.

À la place, cherche en priorité des leviers spécifiques à ce visuel et ce produit : composition, contraste, choix typographique, ordre de lecture, formulation exacte du texte, choix de l'image principale, couleur du CTA, longueur du message, élément structurel manquant identifié dans la vérification structurelle (pas un élément générique e-commerce).

Format OBLIGATOIRE pour chaque priorité :
"Quoi: [action précise et spécifique à ce visuel]\\nPourquoi: [impact sur TikTok et sur la conversion]\\nComment: [étapes concrètes]\\nOù: [emplacement sur le visuel]\\nExemple: [adapté aux codes TikTok]"

7) resume_rapide
"Points forts: ...\\nPoints faibles: ...\\nPar où commencer: ..."

JSON attendu :
{{
  "rapport_sections": {{
    "accroche_visuelle": "...",
    "clarte_message": "...",
    "cta_analyse": "...",
    "coherence_marque": "...",
    "codes_tiktok": "...",
    "recommandations": {{
      "priorite_1": "Quoi: ...\\nPourquoi: ...\\nComment: ...\\nOù: ...\\nExemple: ...",
      "priorite_2": "Quoi: ...\\nPourquoi: ...\\nComment: ...\\nOù: ...\\nExemple: ...",
      "priorite_3": "Quoi: ...\\nPourquoi: ...\\nComment: ...\\nOù: ...\\nExemple: ..."
    }},
    "resume_rapide": "Points forts: ...\\nPoints faibles: ...\\nPar où commencer: ..."
  }}
}}
"""

PROMPT_ADS_PLAN_3_PART1_META = f"""
Tu es un expert en création publicitaire, performance Meta et psychologie du comportement d'achat.

IMPORTANT : tu dois répondre au format JSON STRICT (et rien d'autre).
Le JSON doit contenir une clé "rapport_sections".

Ton objectif : analyser ce visuel publicitaire à travers le prisme du persona cible ET des codes Meta.
Ton : stratégique, humain, précis. Jamais condescendant.

{BLOC_VERIFICATION_STRUCTURELLE}

RÈGLES ABSOLUES :
- Le persona doit influencer CHAQUE section
- Chaque section ancrée dans les codes Meta
- Zéro invention : chaque observation cite un élément RÉEL visible sur l'image
- Pas de markdown, pas d'emojis, pas de hashtags dans le JSON
- Minimum 2 phrases réelles et spécifiques par clé texte

Structure — 5 sections dans cet ordre exact :

1) accroche_visuelle
L'accroche analysée à travers la psychologie du persona sur Meta.
- L'élément dominant stoppe-t-il le scroll de CE persona sur Meta ?
- Le point focal est-il aligné avec ce qui motive ce persona ?

2) clarte_message
Le message résonne-t-il avec ce persona sur Meta ?
- La promesse répond-elle à la question implicite de ce persona ?
- Le registre de langage est-il dans son vocabulaire ?

3) cta_analyse
Le CTA analysé à travers la psychologie du persona sur Meta.
- Le CTA crée-t-il l'urgence, la curiosité ou la confiance dont CE persona a besoin ?
- Est-il formulé dans le registre qui déclenche l'action chez ce persona ?
- Si absent : pose la question ouverte prévue dans la vérification structurelle, n'affirme jamais un choix à la place de la cliente.

4) coherence_marque
Cohérence de l'identité visuelle vue par ce persona.
- Les codes visuels inspirent-ils confiance à CE persona ?
- L'impression générale correspond-elle aux attentes de ce persona sur Meta ?

5) codes_meta_persona
Codes Meta analysés à travers la psychologie du persona.
- Les signaux de confiance présents sont-ils ceux que CE persona cherche sur Meta ?
- Le style est-il adapté au contexte dans lequel CE persona navigue sur Meta ?

JSON attendu (première partie) :
{{
  "rapport_sections": {{
    "accroche_visuelle": "...",
    "clarte_message": "...",
    "cta_analyse": "...",
    "coherence_marque": "...",
    "codes_meta_persona": "..."
  }}
}}
"""

PROMPT_ADS_PLAN_3_PART1_TIKTOK = f"""
Tu es un expert en création publicitaire, performance TikTok et psychologie du comportement d'achat.

IMPORTANT : tu dois répondre au format JSON STRICT (et rien d'autre).
Le JSON doit contenir une clé "rapport_sections".

Ton objectif : analyser ce visuel publicitaire à travers le prisme du persona cible ET des codes TikTok.
Ton : stratégique, humain, précis. Jamais condescendant.

{BLOC_VERIFICATION_STRUCTURELLE}

RÈGLES ABSOLUES :
- Le persona doit influencer CHAQUE section
- Chaque section ancrée dans les codes TikTok
- Zéro invention : chaque observation cite un élément RÉEL visible sur l'image
- Pas de markdown, pas d'emojis, pas de hashtags dans le JSON
- Minimum 2 phrases réelles et spécifiques par clé texte

Structure — 5 sections dans cet ordre exact :

1) accroche_visuelle
L'accroche analysée à travers la psychologie du persona sur TikTok.
- L'élément dominant stoppe-t-il le scroll de CE persona sur TikTok ?
- Le style est-il natif TikTok et aligné avec ce que CE persona consomme ?

2) clarte_message
Le message résonne-t-il avec ce persona sur TikTok ?
- La promesse répond-elle à la question implicite de ce persona ?
- Le registre de langage est-il dans le vocabulaire TikTok de ce persona ?

3) cta_analyse
Le CTA analysé à travers la psychologie du persona sur TikTok.
- Le CTA crée-t-il l'urgence, la curiosité ou la communauté dont CE persona a besoin sur TikTok ?
- Est-il formulé dans le registre TikTok qui déclenche l'action chez ce persona ?
- Si absent : pose la question ouverte prévue dans la vérification structurelle, n'affirme jamais un choix à la place de la cliente.

4) coherence_marque
Cohérence de l'identité visuelle vue par ce persona sur TikTok.
- Les codes visuels semblent-ils authentiques et crédibles pour CE persona ?
- L'impression générale est-elle native TikTok aux yeux de ce persona ?

5) codes_tiktok_persona
Codes TikTok analysés à travers la psychologie du persona.
- Les signaux d'authenticité présents sont-ils ceux que CE persona cherche sur TikTok ?
- Le style UGC, storytelling ou dynamisme correspond-il aux attentes de CE persona ?

JSON attendu (première partie) :
{{
  "rapport_sections": {{
    "accroche_visuelle": "...",
    "clarte_message": "...",
    "cta_analyse": "...",
    "coherence_marque": "...",
    "codes_tiktok_persona": "..."
  }}
}}
"""

PROMPT_ADS_PLAN_3_PART2 = f"""
Tu es un expert en création publicitaire, stratégie plateforme et psychologie du comportement d'achat.

IMPORTANT : tu dois répondre au format JSON STRICT (et rien d'autre).
Le JSON doit contenir une clé "rapport_sections".

Tu as déjà analysé les premiers éléments de cette pub.
Génère maintenant la deuxième partie du rapport.
Reste cohérent avec la première partie fournie en contexte — ne répète jamais un constat déjà fait dans la première partie, même reformulé.
Ton : stratégique, humain, précis. Jamais condescendant.

L'objectif final de cette analyse reste unique : déterminer si ce visuel donne envie de s'arrêter, d'en savoir plus, et d'acquérir le produit pour CE persona précis. Toute observation doit être reliée explicitement à cet objectif de conversion.

Structure — 4 sections dans cet ordre exact :

1) lecture_persona
Psychologie du persona face à cette pub.
- État d'esprit réel quand il tombe sur cette pub sur la plateforme
- Question implicite dans sa tête (la vraie, pas la question de surface)
- Ce qui peut le faire scroller sans s'arrêter VS ce qui peut le faire cliquer
- Registre émotionnel à activer pour CE persona

2) adequation_persona
Adéquation entre le visuel et la psychologie du persona.
- Le visuel parle-t-il vraiment aux motivations profondes de ce persona ?
- Y a-t-il des objections probables de ce persona que la pub ne traite pas ?
- Les déclencheurs d'achat de ce persona sont-ils activés par ce visuel ?
- Si un élément structurel manquant (prix, CTA, réassurance) identifié dans la première partie constitue un frein pour CE persona précis, précise ici pourquoi, sans le reformuler à l'identique.

3) recommandations
3 priorités pour CE persona sur CETTE plateforme, classées par impact réel sur la conversion.

INTERDICTION ABSOLUE DE DÉFAUT : ne propose "ajouter un témoignage/preuve sociale", "rassurer sur le confort/la praticité" ou "ajouter un visuel avant/après" QUE si tu as explicitement identifié dans les sections précédentes (lecture_persona, adequation_persona) que cet élément précis manque ET que c'est le frein principal pour CE persona sur CE produit. Ces 3 idées sont interdites par défaut car trop génériques et reviennent sur presque tous les produits.

À la place, cherche en priorité des leviers spécifiques à la psychologie de CE persona et à CE visuel : reformulation du message dans le vocabulaire exact du persona, ordre de présentation des bénéfices selon ses priorités réelles, ajustement du registre émotionnel (urgence vs réassurance vs aspiration), élément visuel à mettre en avant ou à retirer, objection précise et inédite identifiée dans adequation_persona à traiter directement.

Format OBLIGATOIRE pour chaque priorité :
"Quoi: [action précise et spécifique à ce persona et ce visuel]\\nPourquoi: [impact psychologique sur ce persona et sur la conversion]\\nComment: [étapes concrètes]\\nOù: [emplacement sur le visuel]\\nExemple: [dans le registre exact du persona et de la plateforme]"

4) resume_rapide
"Points forts: ...\\nPoints faibles: ...\\nPar où commencer: ..."

JSON attendu (deuxième partie) :
{{
  "rapport_sections": {{
    "lecture_persona": "...",
    "adequation_persona": "...",
    "recommandations": {{
      "priorite_1": "Quoi: ...\\nPourquoi: ...\\nComment: ...\\nOù: ...\\nExemple: ...",
      "priorite_2": "Quoi: ...\\nPourquoi: ...\\nComment: ...\\nOù: ...\\nExemple: ...",
      "priorite_3": "Quoi: ...\\nPourquoi: ...\\nComment: ...\\nOù: ...\\nExemple: ..."
    }},
    "resume_rapide": "Points forts: ...\\nPoints faibles: ...\\nPar où commencer: ..."
  }}
}}
"""

# =========================
# ACTIVATION PAR CODE ADS
# =========================

class ActivationCodeAdsRequest(BaseModel):
    code: str
    email: str


def activer_code_ads(code: str, email: str) -> Dict[str, Any]:
    """
    Vérifie qu'un code d'activation Ads est valide (existe, non utilisé, email correspondant).
    Ne le marque PAS comme utilisé — ça se fait uniquement au moment de lancer l'analyse.
    """
    code = code.strip().upper()
    email = email.strip().lower()

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT * FROM codes_ads_activation WHERE code = %s", (code,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if row is None:
        raise HTTPException(status_code=404, detail="Code d'activation introuvable.")

    if row["utilise"]:
        raise HTTPException(status_code=403, detail="Ce code a déjà été utilisé.")

    if row["email_client"].strip().lower() != email:
        raise HTTPException(status_code=403, detail="L'email ne correspond pas à ce code.")

    return {
        "ok": True,
        "message": "Code valide",
        "plan": row["plan"],
        "order_number": row["order_number"],
        "code": code,
    }

def consommer_code_ads(code: str, email: str) -> int:
    """
    Vérifie à nouveau le code (protection contre les doubles soumissions) et le marque
    comme utilisé. Appelée juste avant de lancer une analyse (image ou vidéo). Retourne le plan
    (1/2/3 = image, 4 = vidéo).
    """
    code = (code or "").strip().upper()
    email = (email or "").strip().lower()
    if not code:
        raise HTTPException(status_code=400, detail="Code d'activation manquant.")

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM codes_ads_activation WHERE code = %s", (code,))
    row = cur.fetchone()

    if row is None:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="Code d'activation introuvable.")

    if row["utilise"]:
        cur.close()
        conn.close()
        raise HTTPException(status_code=403, detail="Ce code a déjà été utilisé pour lancer une analyse.")

    if row["email_client"].strip().lower() != email:
        cur.close()
        conn.close()
        raise HTTPException(status_code=403, detail="L'email ne correspond pas à ce code.")

    cur.execute(
        "UPDATE codes_ads_activation SET utilise = TRUE, date_utilisation = NOW() WHERE code = %s",
        (code,),
    )
    conn.commit()
    cur.close()
    conn.close()

    print(f"Code Ads {code} consommé pour lancer une analyse (plan {row['plan']}).")
    return row["plan"]


def verifier_et_consommer_code_video(email: str, code: str) -> None:
    """
    Consomme le code d'activation Ads pour l'analyse vidéo. Réutilise le même système
    de codes que les analyses image (table codes_ads_activation), avec le plan 4 réservé
    à la vidéo. Lève une HTTPException si le code est invalide, déjà utilisé, ou lié à
    un autre plan que la vidéo.

    CORRIGÉ (24/07/2026) : remplace l'ancien stub verifier_et_consommer_credits_video
    qui autorisait toujours l'analyse sans vérifier quoi que ce soit.
    """
    plan = consommer_code_ads(code, email)
    if plan != 4:
        raise HTTPException(status_code=403, detail="Ce code n'est pas valide pour une analyse vidéo.")

# =========================
# LOGIQUE OPENAI — IMAGE
# =========================

def get_model_for_plan(plan: int) -> str:
    if plan == 1:
        return DEFAULT_MODEL_PLAN_1
    if plan == 2:
        return DEFAULT_MODEL_PLAN_2
    return DEFAULT_MODEL_PLAN_3


def _call_openai(
    system_prompt: str,
    image_base64: str,
    image_type: str,
    user_text: str,
    model: str,
    max_tokens: int
) -> Dict[str, Any]:
    if client is None:
        raise HTTPException(
            status_code=500,
            detail="OPENAI_API_KEY manquante côté serveur."
        )
    try:
        response = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            temperature=0.35,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{image_type};base64,{image_base64}",
                                "detail": "high"
                            }
                        },
                        {
                            "type": "text",
                            "text": user_text
                        }
                    ]
                }
            ]
        )
    except OpenAIError as e:
        raise HTTPException(status_code=502, detail=f"Erreur OpenAI : {str(e)}")

    raw = response.choices[0].message.content or ""

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Réponse OpenAI non valide (JSON malformé).")

    if "rapport_sections" not in data:
        raise HTTPException(status_code=500, detail="rapport_sections manquant dans la réponse.")

    return data["rapport_sections"]


def call_openai_ads(
    plan: int,
    image_base64: str,
    image_type: str,
    plateforme: Optional[str],
    persona: Optional[str]
) -> Dict[str, Any]:
    model = get_model_for_plan(plan)
    max_tokens = MAX_TOKENS_BY_PLAN[plan]

    user_text = "\n".join([
        "Analyse cette publicité.",
        f"Plateforme : {plateforme or ''}",
        f"Persona : {persona or ''}"
    ])

    if plan == 1:
        sections = _call_openai(
            PROMPT_ADS_PLAN_1, image_base64, image_type, user_text, model, max_tokens
        )
        return {"rapport_sections": sections}

    if plan == 2:
        prompt_plan2 = PROMPT_ADS_PLAN_2_TIKTOK if plateforme == "tiktok" else PROMPT_ADS_PLAN_2_META
        sections = _call_openai(
            prompt_plan2, image_base64, image_type, user_text, model, max_tokens
        )
        return {"rapport_sections": sections}

    # Plan 3 — deux appels séquentiels
    prompt_part1 = PROMPT_ADS_PLAN_3_PART1_TIKTOK if plateforme == "tiktok" else PROMPT_ADS_PLAN_3_PART1_META
    sections_part1 = _call_openai(
        prompt_part1, image_base64, image_type, user_text, model, 3500
    )

    user_text_part2 = "\n".join([
        "Analyse cette publicité.",
        f"Plateforme : {plateforme or ''}",
        f"Persona : {persona or ''}",
        "",
        "Première partie du rapport déjà générée :",
        json.dumps(sections_part1, ensure_ascii=False)
    ])

    sections_part2 = _call_openai(
        PROMPT_ADS_PLAN_3_PART2, image_base64, image_type, user_text_part2, model, 3500
    )

    sections_complete = {**sections_part1, **sections_part2}
    return {"rapport_sections": sections_complete}

ABONNEMENTS_DATABASE_URL = os.getenv("ABONNEMENTS_DATABASE_URL")

def get_abonnements_db_connection():
    if not ABONNEMENTS_DATABASE_URL:
        raise HTTPException(status_code=500, detail="ABONNEMENTS_DATABASE_URL manquante.")
    return psycopg2.connect(ABONNEMENTS_DATABASE_URL)


def get_cout_credits_ads_abonnements(customer_id: str, produit: str, identifiant: str) -> int:
    conn = get_abonnements_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT cout FROM cout_credits_par_plan WHERE produit = %s AND identifiant = %s",
        (produit, identifiant),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row is None:
        raise HTTPException(status_code=500, detail=f"Coût en crédits non configuré pour {produit}/{identifiant}.")
    return row[0]


def debiter_credits_abonnement(customer_id: str, produit: str, cout: int) -> int:
    conn = get_abonnements_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT solde_abonnement_mensuel, solde_achete FROM soldes_abonnement WHERE customer_id = %s AND produit = %s",
        (customer_id, produit),
    )
    row = cur.fetchone()
    if row is None:
        cur.close()
        conn.close()
        raise HTTPException(status_code=403, detail="Aucun crédit disponible pour ce produit.")

    solde_mensuel, solde_achete = row
    solde_total = solde_mensuel + solde_achete
    if solde_total < cout:
        cur.close()
        conn.close()
        raise HTTPException(status_code=403, detail="Solde de crédits insuffisant pour cette profondeur.")

    # On consomme d'abord le solde mensuel, puis le solde acheté
    if solde_mensuel >= cout:
        nouveau_mensuel = solde_mensuel - cout
        nouveau_achete = solde_achete
    else:
        reste = cout - solde_mensuel
        nouveau_mensuel = 0
        nouveau_achete = solde_achete - reste

    cur.execute(
        "UPDATE soldes_abonnement SET solde_abonnement_mensuel = %s, solde_achete = %s WHERE customer_id = %s AND produit = %s",
        (nouveau_mensuel, nouveau_achete, customer_id, produit),
    )
    cur.execute(
        "INSERT INTO mouvements_points (customer_id, produit, type, montant, solde_apres, detail) VALUES (%s, %s, %s, %s, %s, %s)",
        (customer_id, produit, "consommation", -cout, nouveau_mensuel + nouveau_achete, f"analyse_profondeur_{cout}"),
    )
    conn.commit()
    cur.close()
    conn.close()
    return solde_total
    
# =========================
# WEBHOOK SHOPIFY ADS
# =========================

@app.post("/webhook/commande")
async def webhook_commande(request: Request):
    body = await request.body()

    if SHOPIFY_WEBHOOK_SECRET:
        hmac_header = request.headers.get("X-Shopify-Hmac-Sha256", "")
        computed = hmac.new(
            SHOPIFY_WEBHOOK_SECRET.encode("utf-8"),
            body,
            hashlib.sha256,
        ).digest()
        computed_b64 = base64.b64encode(computed).decode("utf-8")
        if not hmac.compare_digest(computed_b64, hmac_header):
            print("Webhook signature invalide")
            return JSONResponse(status_code=200, content={"ok": False})

    try:
        data = json.loads(body)
        order_number = str(data.get("order_number", "")).strip()
        email = (data.get("email") or "").strip().lower()

        # Shopify inclut un objet "customer" dans le payload webhook si l'acheteur
        # était connecté à son compte client au moment de l'achat (absent ou null
        # pour un achat en invité).
        customer_obj = data.get("customer") or {}
        customer_id = customer_obj.get("id")
        customer_id = str(customer_id) if customer_id else None

        if order_number and email:
            line_items = data.get("line_items", [])

            # Détecter le plan Ads le plus élevé commandé
            plan_detecte = 1
            quantite_ads = 0
            for item in line_items:
                variant_id = str(item.get("variant_id", ""))
                qty = int(item.get("quantity", 1))
                if variant_id == VARIANT_ADS_VIDEO:
                    plan_detecte = 4
                    quantite_ads += qty
                elif variant_id == VARIANT_ADS_PLAN_3 and plan_detecte < 4:
                    plan_detecte = 3
                    quantite_ads += qty
                elif variant_id == VARIANT_ADS_PLAN_2 and plan_detecte < 3:
                    plan_detecte = 2
                    quantite_ads += qty
                elif variant_id == VARIANT_ADS_PLAN_1 and plan_detecte < 2:
                    quantite_ads += qty

            # Ne rien enregistrer si aucun produit Ads dans la commande
            if quantite_ads == 0:
                print(f"Commande #{order_number} : aucun produit Ads détecté, ignorée.")
                return JSONResponse(status_code=200, content={"ok": True})

            # Upsert en base commandes_ads : historique/traçabilité uniquement
            # (voir note sur init_db). Protection contre les webhooks dupliqués.
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO commandes_ads (order_number, email, plan, quantite, analyses_utilisees, customer_id)
                VALUES (%s, %s, %s, %s, 0, %s)
                ON CONFLICT (order_number) DO UPDATE
                    SET email       = EXCLUDED.email,
                        plan        = EXCLUDED.plan,
                        quantite    = EXCLUDED.quantite,
                        customer_id = EXCLUDED.customer_id
            """, (order_number, email, plan_detecte, quantite_ads, customer_id))
            conn.commit()
            cur.close()
            conn.close()

            print(f"Commande Ads enregistrée : #{order_number} → {email} → Plan {plan_detecte} → Quantité {quantite_ads} → customer_id {customer_id}")

            # Génération des codes d'activation Ads (un par unité/plan) — c'est
            # le seul système effectivement utilisé pour donner accès aux analyses,
            # y compris pour la vidéo (plan 4).
            generer_codes_ads_pour_commande(order_number, email, line_items, customer_id)

    except Exception as e:
        print(f"Erreur webhook Ads : {e}")

    return JSONResponse(status_code=200, content={"ok": True})


@app.post("/activer/code")
async def activer_code_ads_route(req: ActivationCodeAdsRequest):
    return activer_code_ads(req.code, req.email)

# =========================
# SECTIONS TO PLAIN TEXT
# =========================

def sections_to_plain_text_ads(sections: Dict[str, Any]) -> str:
    parts = []

    def add(title: str, body: str) -> None:
        if body and str(body).strip():
            parts.append(f"{title}\n{body}".strip())

    # Sections communes aux 3 plans
    add("Accroche visuelle", sections.get("accroche_visuelle", ""))
    add("Clarté du message", sections.get("clarte_message", ""))
    add("Analyse du CTA", sections.get("cta_analyse", ""))
    add("Cohérence de la marque", sections.get("coherence_marque", ""))

    # Plan 2 — codes plateforme
    add("Codes Meta", sections.get("codes_meta", ""))
    add("Codes TikTok", sections.get("codes_tiktok", ""))

    # Plan 3 — persona
    add("Codes Meta (persona)", sections.get("codes_meta_persona", ""))
    add("Codes TikTok (persona)", sections.get("codes_tiktok_persona", ""))
    add("Lecture du persona", sections.get("lecture_persona", ""))
    add("Adéquation persona", sections.get("adequation_persona", ""))

    # Recommandations
    recos = sections.get("recommandations") or {}
    if isinstance(recos, dict):
        parts.append("Recommandations priorisées")
        parts.append("Priorité 1 :\n" + str(recos.get("priorite_1", "")))
        parts.append("Priorité 2 :\n" + str(recos.get("priorite_2", "")))
        parts.append("Priorité 3 :\n" + str(recos.get("priorite_3", "")))

    # Résumé
    add("Résumé rapide", sections.get("resume_rapide", ""))

    return "\n\n".join(p for p in parts if p.strip())

# =========================
# EMAIL VIA RESEND
# =========================

import resend as resend_client

def send_rapport_ads_by_email(email: str, plan: int, rapport_texte: str) -> None:
    plan_names = {
        1: "Plan Essentielle Ads",
        2: "Plan Ciblée Plateforme Ads",
        3: "Plan Avancée Persona Ads",
        4: "Plan Vidéo Ads",
    }
    plan_name = plan_names.get(plan, f"Plan {plan}")
    RESEND_API_KEY = os.getenv("RESEND_API_KEY")
    if not RESEND_API_KEY:
        print("RESEND_API_KEY manquante, email non envoyé.")
        return
    resend_client.api_key = RESEND_API_KEY
    try:
        resend_client.Emails.send({
            "from": "MayNov <rapport@maynov.fr>",
            "to": email,
            "subject": f"Votre rapport MayNov Ads — {plan_name}",
            "html": f"""
<div style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto;padding:20px;">
  <div style="background:#1d3557;padding:16px 24px;border-radius:12px;margin-bottom:24px;">
    <span style="color:white;font-size:20px;font-weight:900;">MAY<span style="color:#8fd19e;">NOV</span> <span style="color:#f4a261;font-size:14px;">ADS</span></span>
  </div>
  <h2 style="color:#1d3557;">Votre rapport d'analyse pub est prêt ✅</h2>
  <p style="color:#475569;">Voici votre rapport <strong>{plan_name}</strong>. Conservez cet email pour y revenir à tout moment.</p>
  <div style="background:#f2f2f2;border-radius:12px;padding:20px;margin:20px 0;white-space:pre-wrap;font-size:14px;line-height:1.7;color:#1a1a1a;">
{rapport_texte}
  </div>
  <p style="color:#475569;font-size:13px;">Des questions ? Contactez-nous à <a href="mailto:contact@maynov.fr">contact@maynov.fr</a></p>
  <p style="color:#94a3b8;font-size:11px;">© 2026 MayNov · maynov.fr</p>
</div>
            """,
        })
        print(f"Email Ads envoyé à {email} pour {plan_name}")
    except Exception as e:
        print(f"Erreur envoi email Resend Ads : {e}")

# =========================
# ROUTES — ANALYSE IMAGE
# =========================


@app.post("/analyser/ads/basique")
async def analyser_ads_basique(file: UploadFile = File(...)):
    image_base64, image_type = await read_and_encode_image(file)
    return call_openai_ads(
        plan=1,
        image_base64=image_base64,
        image_type=image_type,
        plateforme=None,
        persona=None
    )


@app.post("/analyser/ads/plateforme")
async def analyser_ads_plateforme(
    file: UploadFile = File(...),
    plateforme: str = Form(...)
):
    if plateforme not in ["meta", "tiktok"]:
        raise HTTPException(status_code=400, detail="Plateforme invalide. Valeurs acceptées : meta, tiktok.")
    image_base64, image_type = await read_and_encode_image(file)
    return call_openai_ads(
        plan=2,
        image_base64=image_base64,
        image_type=image_type,
        plateforme=plateforme,
        persona=None
    )


@app.post("/analyser/ads/persona")
async def analyser_ads_persona(
    file: UploadFile = File(...),
    plateforme: str = Form(...),
    persona: str = Form(...)
):
    if plateforme not in ["meta", "tiktok"]:
        raise HTTPException(status_code=400, detail="Plateforme invalide. Valeurs acceptées : meta, tiktok.")
    if not persona or not persona.strip():
        raise HTTPException(status_code=400, detail="Persona manquant pour ce plan.")
    image_base64, image_type = await read_and_encode_image(file)
    return call_openai_ads(
        plan=3,
        image_base64=image_base64,
        image_type=image_type,
        plateforme=plateforme,
        persona=persona
    )

@app.post("/analyser/ads/basique/rapport")
async def analyser_ads_basique_rapport(
    file: UploadFile = File(...),
    email: str = Form(...),
    code: str = Form(...)
):
    consommer_code_ads(code, email)
    image_base64, image_type = await read_and_encode_image(file)
    data = call_openai_ads(
        plan=1,
        image_base64=image_base64,
        image_type=image_type,
        plateforme=None,
        persona=None
    )
    sections = data["rapport_sections"]
    rapport_texte = sections_to_plain_text_ads(sections)
    if email:
        send_rapport_ads_by_email(email, 1, rapport_texte)
    return {"plan": 1, "rapport_sections": sections, "rapport_texte": rapport_texte}


@app.post("/analyser/ads/plateforme/rapport")
async def analyser_ads_plateforme_rapport(
    file: UploadFile = File(...),
    plateforme: str = Form(...),
    email: str = Form(...),
    code: str = Form(...)
):
    if plateforme not in ["meta", "tiktok"]:
        raise HTTPException(status_code=400, detail="Plateforme invalide. Valeurs acceptées : meta, tiktok.")
    consommer_code_ads(code, email)
    image_base64, image_type = await read_and_encode_image(file)
    data = call_openai_ads(
        plan=2,
        image_base64=image_base64,
        image_type=image_type,
        plateforme=plateforme,
        persona=None
    )
    sections = data["rapport_sections"]
    rapport_texte = sections_to_plain_text_ads(sections)
    if email:
        send_rapport_ads_by_email(email, 2, rapport_texte)
    return {"plan": 2, "rapport_sections": sections, "rapport_texte": rapport_texte}


@app.post("/analyser/ads/persona/rapport")
async def analyser_ads_persona_rapport(
    file: UploadFile = File(...),
    plateforme: str = Form(...),
    persona: str = Form(...),
    email: str = Form(...),
    code: str = Form(...)
):
    if plateforme not in ["meta", "tiktok"]:
        raise HTTPException(status_code=400, detail="Plateforme invalide. Valeurs acceptées : meta, tiktok.")
    if not persona or not persona.strip():
        raise HTTPException(status_code=400, detail="Persona manquant pour ce plan.")
    consommer_code_ads(code, email)
    image_base64, image_type = await read_and_encode_image(file)
    data = call_openai_ads(
        plan=3,
        image_base64=image_base64,
        image_type=image_type,
        plateforme=plateforme,
        persona=persona
    )
    sections = data["rapport_sections"]
    rapport_texte = sections_to_plain_text_ads(sections)
    if email:
        send_rapport_ads_by_email(email, 3, rapport_texte)
    return {"plan": 3, "rapport_sections": sections, "rapport_texte": rapport_texte}


# =========================
# ANALYSE VIDÉO (fusionné depuis route_video.py le 24/07/2026)
# =========================

MODELE_VIDEO = os.getenv("OPENAI_VIDEO_MODEL", "gpt-4o")
INTERVALLE_SECONDES = 1.5
NB_IMAGES_MIN = 8
NB_IMAGES_MAX = 20
MAX_TOKENS_SORTIE_VIDEO = 4000

TAILLE_VIDEO_MAX_MO = 100
DUREE_VIDEO_MAX_SECONDES = 30
FORMATS_ACCEPTES_VIDEO = {".mp4"}

TYPES_CONTENU_VALIDES = {"promo_marque", "lifestyle_engagement", "pub_payante"}

ANGLE_PAR_TYPE = {
    "promo_marque": "Une marque présente son propre produit avec un objectif de conversion, de visite du site ou de découverte de l'offre.",
    "lifestyle_engagement": "Un contenu qui montre un produit dans un contexte de vie pour générer de l'engagement. Un CTA de vente explicite n'est PAS attendu ici — ne pénalise jamais son absence.",
    "pub_payante": "Une publicité display classique (Meta/TikTok Ads). Analyse selon la grille hook / preuve / objection / CTA habituelle de la publicité payante.",
}


def construire_prompt_systeme_video(type_contenu: str) -> str:
    """Prompt vidéo v9 — figé, voir /areas/maynov-video-ads dans les notes.
    Non modifié lors de la fusion du 24/07/2026 (seul le prompt image a été retravaillé)."""
    angle = ANGLE_PAR_TYPE[type_contenu]
    return f"""
Tu es un consultant senior en stratégie publicitaire e-commerce et en création publicitaire, spécialisé dans l'optimisation des formats courts (TikTok, Reels, Stories). Tu analyses une publicité vidéo non encore publiée pour déterminer si elle est prête à être diffusée, et pour livrer un accompagnement de niveau expert. Une cliente paie pour ton regard professionnel : chaque section doit apporter une information qu'elle n'aurait pas vue seule en regardant sa propre vidéo.

Précise systématiquement que ton analyse porte uniquement sur les éléments visuels et textuels (aucune transcription audio, aucune musique, aucun effet sonore ne te sont fournis) — et que le verdict final reste limité par cette absence.

FIABILITÉ DES TIMESTAMPS

La durée totale réelle de la vidéo et le timestamp exact de chaque image te sont fournis explicitement. Utilise UNIQUEMENT ces timestamps — ne devine jamais un timestamp intermédiaire, ne suppose jamais que la vidéo se termine avant sa durée réelle annoncée. Ne prétends jamais connaître avec certitude une durée d'affichage exacte ou la fluidité réelle d'un mouvement lorsque cela ne peut pas être déterminé à partir des images — utilise alors une formulation prudente. La dernière image fournie correspond à la fin ou quasi-fin réelle de la vidéo : traite-la comme telle.

CONTEXTE DU CONTENU

Type de contenu : {type_contenu}. {angle}

LES 8 CRITÈRES DE JUGEMENT — À APPLIQUER SYSTÉMATIQUEMENT :

1. Hook (0-3 premières secondes) : présence d'un pattern interrupt, d'un curiosity gap court, ou d'un bénéfice direct immédiat. Retranscris le texte du hook, compte ses mots, calcule le temps de lecture à ~3 mots/seconde et montre ce calcul. Un hook dont le calcul montre un temps de lecture largement inférieur au temps d'affichage n'est PAS trop long — ne le signale comme problème que si le calcul le confirme réellement.

2. Rythme et progression : un cadrage fixe n'est PAS automatiquement un défaut. Vérifie si le CONTENU évolue entre les frames (nouvelle teinte, nouvelle étape, nouvelle preuve, nouvel angle). Si le contenu progresse dans un cadre stable, parle de "progression dans un cadre stable", jamais de répétition. Ne signale un vrai risque de décrochage que si le cadrage ET le contenu restent identiques.

3. Lisibilité du texte à l'écran : quantité de mots, hiérarchie visuelle, contraste, emplacement, compréhension possible sans audio.

4. Preuve et démonstration ("show, don't tell") : distingue ce qui est réellement démontré de ce qui est seulement affirmé. Une promesse sans preuve visuelle est un point faible explicite.

5. Fin et boucle : juge la ou les dernières images fournies. Capitalisent-elles sur l'attention ou gâchent-elles l'opportunité finale ?

6. CTA — clarté et objectif réel : distingue toujours l'objectif immédiat (cliquer, visiter le profil, retrouver la marque) de l'objectif commercial final (acheter). Un lien "trouve ta teinte en bio" vise une visite du site, pas un achat immédiat — ne le qualifie jamais de "conversion" sans cette nuance. Un écran de recherche TikTok facilite le repérage de la marque, ce n'est pas automatiquement de l'engagement. (Pour le type lifestyle_engagement, ne pénalise jamais l'absence de CTA de vente explicite.)

7. Authenticité voulue vs amateurisme non maîtrisé : distingue un format natif/UGC volontaire d'un amateurisme non maîtrisé. Vérifie la saturation des couleurs comme signal concret.

8. Audio — hors périmètre : ne juge jamais le son, la voix, ou la musique.

RÈGLE DE PREUVE

Pour chaque critique importante, distingue : Observation → Interprétation publicitaire → Risque → Impact (élevé / moyen / faible). Ne crée jamais un défaut pour rendre le rapport plus sévère. Une recommandation faible qui ne change presque rien à la compréhension, au désir ou à la rétention ne doit pas apparaître dans les recommandations prioritaires — mieux vaut 2 recommandations fortes que 3 dont une inutile.

RÈGLES ANTI-HALLUCINATION — STRICTES ET NON NÉGOCIABLES

1. N'attribue jamais un bénéfice, un effet, ou une compatibilité qui n'est pas directement prouvé par ce qui est montré. Si la catégorie exacte du produit n'est pas certaine, dis-le explicitement.

2. Une absence n'est pas automatiquement un défaut. Demande-toi si cet élément est réellement nécessaire compte tenu de l'objectif apparent de CETTE vidéo précise avant de la signaler comme un manque.

3. Impact "élevé" réservé strictement aux problèmes qui empêchent réellement de comprendre le produit ou de vouloir passer à l'achat. Justifie explicitement pourquoi si tu l'utilises.

4. Aucune recommandation ne peut introduire un bénéfice ou un angle marketing qui n'est pas déjà visible dans la vidéo.

5. La section "Proposition de déroulé optimisé" doit être strictement proportionnée au verdict final. Si le verdict est "corrections mineures avant diffusion", ne modifie au maximum que 1 à 2 lignes du déroulé réel existant, sans en ajouter de nouvelles ni inventer de séquence absente de la vidéo. Une refonte complète n'est permise que si le verdict est "à retravailler en profondeur".

RÈGLES DE COHÉRENCE INTERNE — STRICTES

6. Avant de signaler un problème, vérifie que ton observation ne contredit pas tes propres données. Si tu calcules qu'un hook est court et rapide à lire, tu ne peux pas ensuite dire qu'il est trop long.

7. Dans une démonstration de variantes (teintes, tailles, modèles, étapes successives), chaque nouvelle variante montrée constitue une nouvelle information, même si le geste ou le cadrage reste identique. Ne qualifie jamais ça de répétition sans nouvelle information — sauf si deux variantes consécutives sont visuellement indiscernables, à justifier explicitement.

8. Dans la section "Pouvoir de conviction réel", ne suppose jamais une promesse implicite non montrée (ex. "qualité"). Si la vidéo crée seulement de la curiosité esthétique sans démontrer de différenciation réelle, dis-le explicitement plutôt que d'inventer une raison d'achat absente.

RÈGLE FONDAMENTALE — EXIGENCE, PROFONDEUR, JAMAIS DE GÉNÉRICITÉ

Cherche activement ce qui risque de faire échouer la vidéo. Ne fabrique jamais une critique non fondée. En cas de doute, penche du côté de l'exigence, mais uniquement sur des problèmes réels et vérifiables.

Interdiction stricte des formulations génériques : "pourrait être amélioré", "pensez à optimiser", "rendre la vidéo plus dynamique", "améliorer l'engagement", "optimiser le hook" — sans jamais préciser exactement quoi, où et pourquoi. Chaque phrase doit référencer un timestamp ou une observation visuelle précise. Aucune répétition de la même critique dans plus de deux sections.

STRUCTURE OBLIGATOIRE DU RAPPORT

### 1. Lecture globale de la vidéo
Durée totale réelle, produit/offre identifiable, format observable, objectif apparent, angle publicitaire dominant, logique générale, limite liée à l'absence d'audio.

### 2. Promesse et logique publicitaire
Ce que la publicité cherche réellement à vendre, la promesse perçue (en distinguant ce qui est prouvé de ce qui est seulement suggéré), le bénéfice concret, l'émotion recherchée, la différenciation perceptible ou son absence.

### 2bis. Pouvoir de conviction réel
Est-ce que cette vidéo donne envie d'acheter, au-delà de la simple curiosité ? Distingue curiosité générée et désir d'achat réel, sans inventer de promesse implicite non montrée.

### 3. Spectateur visé et compréhension
Type de spectateur apparent (préciser qu'il s'agit d'une déduction), attentes probables, doutes possibles, facilité de compréhension sans audio.

### 4. Déroulé et architecture persuasive
Analyse chronologique par séquences cohérentes. Pour chaque séquence :
#### [Plage de timestamps observée]
Observation :
Fonction publicitaire :
Ce qui fonctionne :
Limite éventuelle :
Couvre l'ensemble de la vidéo.

### 5. Ce que la vidéo fait déjà bien
2 à 5 éléments réellement réussis avec timestamp et explication.

### 6. Ce qui risque de freiner la performance
Section la plus développée. Pour chaque problème réellement significatif et vérifié :
#### [Titre précis du problème]
Timestamp ou plage observée :
Observation :
Interprétation publicitaire :
Risque :
Impact : élevé / moyen / faible

### 7. Lisibilité, démonstration et preuve
Hook et son calcul mots/temps explicite, lisibilité des textes principaux, ce qui est démontré vs affirmé.

### 8. CTA et fin de vidéo
CTA visibles, objectif immédiat vs objectif commercial final, timing, cohérence, dernières images.

### 9. Recommandations priorisées
2 à 5 recommandations à fort impact réel uniquement :
#### Priorité [N] — [Titre]
Quoi :
Pourquoi :
Comment :
Où :
Exemple :
Effet recherché :

### 10. Proposition de déroulé optimisé
Si le verdict est "corrections mineures" : maximum 1 à 2 ajustements sur le déroulé réel existant. Sinon, tableau complet (maximum 8 séquences) :
| Plage temporelle proposée | Visuel ou action | Texte éventuel | Fonction publicitaire |

### 11. Verdict de préparation
Un seul verdict : Prête à être diffusée / Corrections mineures avant diffusion / À retravailler en profondeur. Justifie en un paragraphe court. Rappelle la limite audio. Aucun score chiffré.

CONTRAINTE DE LONGUEUR

Vise 1200 à 1800 mots. Ne répète jamais la même critique dans plus de deux sections.
""".strip()


def obtenir_infos_video(chemin_video: Path) -> dict:
    resultat = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=width,height", "-of", "json", str(chemin_video)],
        capture_output=True, text=True, check=False,
    )
    if resultat.returncode != 0:
        raise HTTPException(status_code=400, detail="Impossible de lire cette vidéo. Vérifiez le format du fichier.")
    donnees = json.loads(resultat.stdout)
    duree = float(donnees["format"]["duration"])
    stream = next((s for s in donnees.get("streams", []) if "width" in s), {})
    largeur = stream.get("width", 0)
    hauteur = stream.get("height", 0)
    return {"duree": duree, "largeur": largeur, "hauteur": hauteur}


def valider_video(chemin_video: Path, taille_mo: float):
    if taille_mo > TAILLE_VIDEO_MAX_MO:
        raise HTTPException(status_code=400, detail=f"Vidéo trop lourde ({taille_mo:.1f} Mo). Maximum : {TAILLE_VIDEO_MAX_MO} Mo.")
    infos = obtenir_infos_video(chemin_video)
    if infos["duree"] > DUREE_VIDEO_MAX_SECONDES:
        raise HTTPException(status_code=400, detail=f"Vidéo trop longue ({infos['duree']:.0f}s). Maximum : {DUREE_VIDEO_MAX_SECONDES}s.")
    if infos["hauteur"] and infos["largeur"] and infos["hauteur"] <= infos["largeur"]:
        raise HTTPException(status_code=400, detail="Seules les vidéos verticales (format 9:16) sont acceptées pour l'instant.")
    return infos["duree"]


def calculer_nb_images(duree: float) -> int:
    nombre_estime = round(duree / INTERVALLE_SECONDES) + 1
    return max(NB_IMAGES_MIN, min(nombre_estime, NB_IMAGES_MAX))


def extraire_frames(chemin_video: Path, duree: float, dossier_temp: Path) -> list:
    nb_images = calculer_nb_images(duree)
    dernier_timestamp = max(0.0, duree - 0.1)
    frames = []
    for i in range(nb_images):
        timestamp = round((dernier_timestamp / (nb_images - 1)) * i, 2) if nb_images > 1 else 0.0
        nom_fichier = dossier_temp / f"frame_{i:02d}.jpg"
        resultat = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(timestamp), "-i", str(chemin_video),
             "-frames:v", "1", "-vf", "scale='min(1440,iw)':-2", "-q:v", "2", str(nom_fichier)],
            capture_output=True, text=True, check=False,
        )
        if resultat.returncode != 0 or not nom_fichier.exists():
            raise HTTPException(status_code=500, detail="Erreur lors du traitement de la vidéo. Réessayez.")
        frames.append((nom_fichier, timestamp))
    return frames


def encoder_image(chemin_image: Path) -> str:
    with chemin_image.open("rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


@app.post("/analyser/video")
async def analyser_video_route(
    file: UploadFile = File(...),
    email: str = Form(...),
    code: str = Form(...),
    type_contenu: str = Form(...),
):
    if type_contenu not in TYPES_CONTENU_VALIDES:
        raise HTTPException(status_code=400, detail="Type de contenu invalide.")

    if Path(file.filename).suffix.lower() not in FORMATS_ACCEPTES_VIDEO:
        raise HTTPException(status_code=400, detail="Seul le format MP4 est accepté.")

    # CORRIGÉ (24/07/2026) : vérifie et consomme réellement le code d'activation
    # (plan 4) au lieu du stub qui laissait toujours passer.
    verifier_et_consommer_code_video(email, code)

    with tempfile.TemporaryDirectory() as dossier_temp_str:
        dossier_temp = Path(dossier_temp_str)
        chemin_video = dossier_temp / "video.mp4"

        contenu = await file.read()
        taille_mo = len(contenu) / (1024 * 1024)
        with chemin_video.open("wb") as f:
            f.write(contenu)

        duree = valider_video(chemin_video, taille_mo)
        frames = extraire_frames(chemin_video, duree, dossier_temp)

        contenu_message = [{
            "type": "text",
            "text": (
                f"Analyse la publicité vidéo à partir des images suivantes.\n\n"
                f"Durée totale réelle : {duree:.2f} secondes.\n"
                f"Nombre d'images fournies : {len(frames)}.\n\n"
                "Les images sont classées dans l'ordre chronologique. Chaque image est précédée de son timestamp exact.\n"
                "Aucune transcription audio n'est fournie."
            ),
        }]
        for index, (nom_fichier, timestamp) in enumerate(frames):
            contenu_message.append({"type": "text", "text": f"FRAME {index + 1}/{len(frames)} — timestamp exact : {timestamp:.2f}s"})
            contenu_message.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoder_image(nom_fichier)}", "detail": "high"},
            })
        contenu_message.append({
            "type": "text",
            "text": "Rédige maintenant le rapport complet en français. Respecte strictement la structure et toutes les règles du prompt système.",
        })

        if client is None:
            raise HTTPException(status_code=500, detail="OPENAI_API_KEY manquante côté serveur.")

        try:
            reponse = client.chat.completions.create(
                model=MODELE_VIDEO,
                temperature=0.3,
                max_completion_tokens=MAX_TOKENS_SORTIE_VIDEO,
                messages=[
                    {"role": "system", "content": construire_prompt_systeme_video(type_contenu)},
                    {"role": "user", "content": contenu_message},
                ],
            )
        except Exception:
            raise HTTPException(status_code=502, detail="Erreur lors de la génération du rapport. Réessayez dans quelques instants.")

        rapport = reponse.choices[0].message.content
        if not rapport:
            raise HTTPException(status_code=502, detail="Aucun rapport n'a pu être généré. Réessayez.")
        if email:
            send_rapport_ads_by_email(email, 4, rapport)
        return {
            "rapport_texte": rapport,
            "usage": {
                "modele": MODELE_VIDEO,
                "duree_video_secondes": round(duree, 2),
                "nombre_images": len(frames),
                "tokens_total": getattr(reponse.usage, "total_tokens", None),
            },
        }
@app.post("/analyser/ads/abonnement/rapport")
async def analyser_ads_abonnement_rapport(
    file: UploadFile = File(...),
    customer_id: str = Form(...),
    profondeur: int = Form(...),
    plateforme: Optional[str] = Form(None),
    persona: Optional[str] = Form(None),
):
    if profondeur not in (1, 2, 3):
        raise HTTPException(status_code=400, detail="Profondeur invalide pour une analyse image (1, 2 ou 3).")
    if profondeur in (2, 3) and plateforme not in ("meta", "tiktok"):
        raise HTTPException(status_code=400, detail="Plateforme invalide. Valeurs acceptées : meta, tiktok.")
    if profondeur == 3 and (not persona or not persona.strip()):
        raise HTTPException(status_code=400, detail="Persona manquant pour cette profondeur.")

    identifiant_cout = str(profondeur)
    cout = get_cout_credits_ads_abonnements(customer_id, "ads", identifiant_cout)

    solde_avant = debiter_credits_abonnement(customer_id, "ads", cout)

    image_base64, image_type = await read_and_encode_image(file)
    data = call_openai_ads(
        plan=profondeur,
        image_base64=image_base64,
        image_type=image_type,
        plateforme=plateforme,
        persona=persona,
    )
    sections = data["rapport_sections"]
    rapport_texte = sections_to_plain_text_ads(sections)

    return {
        "plan": profondeur,
        "rapport_sections": sections,
        "rapport_texte": rapport_texte,
        "solde_restant": solde_avant - cout,
    }
