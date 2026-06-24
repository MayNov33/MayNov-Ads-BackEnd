import os
import json
import base64
import hmac
import hashlib
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

APP_VERSION = "1.0.0"

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

# =========================
# BASE DE DONNÉES
# =========================

def get_db_connection():
    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="DATABASE_URL manquante.")
    conn = psycopg2.connect(DATABASE_URL)
    return conn


def init_db():
    """Crée la table commandes_ads si elle n'existe pas encore."""
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
                analyses_utilisees INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("Table commandes_ads prête.")
    except Exception as e:
        print(f"Erreur initialisation base de données Ads : {e}")

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
# CONFIG UPLOAD
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
# PROMPTS
# =========================

PROMPT_ADS_PLAN_1 = """
Tu es un expert en création publicitaire et en optimisation de visuels e-commerce.

IMPORTANT : tu dois répondre au format JSON STRICT (et rien d'autre).
Le JSON doit contenir une clé "rapport_sections".

Ton objectif : analyser ce visuel publicitaire point par point et identifier ce qui fonctionne ou freine sa performance.
Ton : direct, concret, orienté action. Jamais condescendant.

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
- Si absent : quel impact probable sur la performance ?

4) coherence_marque
Cohérence de l'identité visuelle.
- Les couleurs, la typographie et le style sont-ils cohérents entre eux ?
- La pub dégage-t-elle une identité claire ou un mélange confus ?
- L'impression générale est-elle professionnelle, amateur ou confuse ?

5) recommandations
3 priorités d'amélioration classées par impact.

INTERDICTION ABSOLUE DE DÉFAUT : ne propose "ajouter un témoignage/preuve sociale", "rassurer sur le confort/la praticité" ou "ajouter un visuel avant/après" QUE si tu as explicitement identifié dans les sections précédentes que cet élément précis manque ET que c'est le frein principal pour CE produit. Ces 3 idées sont interdites par défaut car trop génériques.

À la place, cherche en priorité des leviers spécifiques à ce visuel et ce produit : composition, contraste, choix typographique, ordre de lecture, formulation exacte du texte, choix de l'image principale, couleur du CTA, longueur du message, élément manquant unique à CE produit.

Format OBLIGATOIRE :
"Quoi: [action précise et spécifique à ce visuel]\\nPourquoi: [impact]\\nComment: [étapes]\\nOù: [emplacement]\\nExemple: [concret]"

6) resume_rapide
"Points forts: ...\\nPoints faibles: ...\\nPar où commencer: ..."

JSON attendu :
{
  "rapport_sections": {
    "accroche_visuelle": "...",
    "clarte_message": "...",
    "cta_analyse": "...",
    "coherence_marque": "...",
    "recommandations": {
      "priorite_1": "Quoi: ...\\nPourquoi: ...\\nComment: ...\\nOù: ...\\nExemple: ...",
      "priorite_2": "Quoi: ...\\nPourquoi: ...\\nComment: ...\\nOù: ...\\nExemple: ...",
      "priorite_3": "Quoi: ...\\nPourquoi: ...\\nComment: ...\\nOù: ...\\nExemple: ..."
    },
    "resume_rapide": "Points forts: ...\\nPoints faibles: ...\\nPar où commencer: ..."
  }
}
"""

PROMPT_ADS_PLAN_2_META = """
Tu es un expert en création publicitaire et en performance des publicités Meta (Facebook et Instagram).

IMPORTANT : tu dois répondre au format JSON STRICT (et rien d'autre).
Le JSON doit contenir une clé "rapport_sections".

Ton objectif : analyser ce visuel publicitaire en tenant compte des codes créatifs spécifiques à Meta.
Ton : stratégique, direct, ancré dans les réalités de la plateforme. Jamais condescendant.

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
- Si absent : quel impact sur le taux de clic Meta ?

4) coherence_marque
Cohérence de l'identité visuelle.
- Les couleurs, la typographie et le style sont-ils cohérents ?
- L'impression générale est-elle professionnelle et digne de confiance sur Meta ?

5) codes_meta
Codes créatifs spécifiques à Meta.
- Ce visuel respecte-t-il les codes qui performent sur Meta (authenticité, preuve sociale, bénéfice immédiat) ?
- Le style est-il adapté au format Feed, Reels ou Stories ?
- Quels signaux de confiance sont présents ou manquants pour ce contexte Meta ?

6) recommandations
3 priorités adaptées à Meta.

INTERDICTION ABSOLUE DE DÉFAUT : ne propose "ajouter un témoignage/preuve sociale", "rassurer sur le confort/la praticité" ou "ajouter un visuel avant/après" QUE si tu as explicitement identifié dans les sections précédentes que cet élément précis manque ET que c'est le frein principal pour CE produit. Ces 3 idées sont interdites par défaut car trop génériques.

À la place, cherche en priorité des leviers spécifiques à ce visuel et ce produit : composition, contraste, choix typographique, ordre de lecture, formulation exacte du texte, choix de l'image principale, couleur du CTA, longueur du message, élément manquant unique à CE produit (pas un élément générique e-commerce).

Format OBLIGATOIRE pour chaque priorité :
"Quoi: [action précise et spécifique à ce visuel]\\nPourquoi: [impact sur Meta]\\nComment: [étapes concrètes]\\nOù: [emplacement sur le visuel]\\nExemple: [adapté aux codes Meta]"

7) resume_rapide
"Points forts: ...\\nPoints faibles: ...\\nPar où commencer: ..."

JSON attendu :
{
  "rapport_sections": {
    "accroche_visuelle": "...",
    "clarte_message": "...",
    "cta_analyse": "...",
    "coherence_marque": "...",
    "codes_meta": "...",
    "recommandations": {
      "priorite_1": "Quoi: ...\\nPourquoi: ...\\nComment: ...\\nOù: ...\\nExemple: ...",
      "priorite_2": "Quoi: ...\\nPourquoi: ...\\nComment: ...\\nOù: ...\\nExemple: ...",
      "priorite_3": "Quoi: ...\\nPourquoi: ...\\nComment: ...\\nOù: ...\\nExemple: ..."
    },
    "resume_rapide": "Points forts: ...\\nPoints faibles: ...\\nPar où commencer: ..."
  }
}
"""

PROMPT_ADS_PLAN_2_TIKTOK = """
Tu es un expert en création publicitaire et en performance des publicités TikTok.

IMPORTANT : tu dois répondre au format JSON STRICT (et rien d'autre).
Le JSON doit contenir une clé "rapport_sections".

Ton objectif : analyser ce visuel publicitaire en tenant compte des codes créatifs spécifiques à TikTok.
Ton : stratégique, direct, ancré dans les réalités de la plateforme. Jamais condescendant.

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
- Si absent : quel impact sur l'engagement TikTok ?

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
3 priorités adaptées à TikTok.

INTERDICTION ABSOLUE DE DÉFAUT : ne propose "ajouter un témoignage/preuve sociale", "rassurer sur le confort/la praticité" ou "ajouter un visuel avant/après" QUE si tu as explicitement identifié dans les sections précédentes que cet élément précis manque ET que c'est le frein principal pour CE produit. Ces 3 idées sont interdites par défaut car trop génériques.

À la place, cherche en priorité des leviers spécifiques à ce visuel et ce produit : composition, contraste, choix typographique, ordre de lecture, formulation exacte du texte, choix de l'image principale, couleur du CTA, longueur du message, élément manquant unique à CE produit (pas un élément générique e-commerce).

Format OBLIGATOIRE pour chaque priorité :
"Quoi: [action précise et spécifique à ce visuel]\\nPourquoi: [impact sur TikTok]\\nComment: [étapes concrètes]\\nOù: [emplacement sur le visuel]\\nExemple: [adapté aux codes TikTok]"

7) resume_rapide
"Points forts: ...\\nPoints faibles: ...\\nPar où commencer: ..."

JSON attendu :
{
  "rapport_sections": {
    "accroche_visuelle": "...",
    "clarte_message": "...",
    "cta_analyse": "...",
    "coherence_marque": "...",
    "codes_tiktok": "...",
    "recommandations": {
      "priorite_1": "Quoi: ...\\nPourquoi: ...\\nComment: ...\\nOù: ...\\nExemple: ...",
      "priorite_2": "Quoi: ...\\nPourquoi: ...\\nComment: ...\\nOù: ...\\nExemple: ...",
      "priorite_3": "Quoi: ...\\nPourquoi: ...\\nComment: ...\\nOù: ...\\nExemple: ..."
    },
    "resume_rapide": "Points forts: ...\\nPoints faibles: ...\\nPar où commencer: ..."
  }
}
"""

PROMPT_ADS_PLAN_3_PART1_META = """
Tu es un expert en création publicitaire, performance Meta et psychologie du comportement d'achat.

IMPORTANT : tu dois répondre au format JSON STRICT (et rien d'autre).
Le JSON doit contenir une clé "rapport_sections".

Ton objectif : analyser ce visuel publicitaire à travers le prisme du persona cible ET des codes Meta.
Ton : stratégique, humain, précis. Jamais condescendant.

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

4) coherence_marque
Cohérence de l'identité visuelle vue par ce persona.
- Les codes visuels inspirent-ils confiance à CE persona ?
- L'impression générale correspond-elle aux attentes de ce persona sur Meta ?

5) codes_meta_persona
Codes Meta analysés à travers la psychologie du persona.
- Les signaux de confiance présents sont-ils ceux que CE persona cherche sur Meta ?
- Le style est-il adapté au contexte dans lequel CE persona navigue sur Meta ?

JSON attendu (première partie) :
{
  "rapport_sections": {
    "accroche_visuelle": "...",
    "clarte_message": "...",
    "cta_analyse": "...",
    "coherence_marque": "...",
    "codes_meta_persona": "..."
  }
}
"""

PROMPT_ADS_PLAN_3_PART1_TIKTOK = """
Tu es un expert en création publicitaire, performance TikTok et psychologie du comportement d'achat.

IMPORTANT : tu dois répondre au format JSON STRICT (et rien d'autre).
Le JSON doit contenir une clé "rapport_sections".

Ton objectif : analyser ce visuel publicitaire à travers le prisme du persona cible ET des codes TikTok.
Ton : stratégique, humain, précis. Jamais condescendant.

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

4) coherence_marque
Cohérence de l'identité visuelle vue par ce persona sur TikTok.
- Les codes visuels semblent-ils authentiques et crédibles pour CE persona ?
- L'impression générale est-elle native TikTok aux yeux de ce persona ?

5) codes_tiktok_persona
Codes TikTok analysés à travers la psychologie du persona.
- Les signaux d'authenticité présents sont-ils ceux que CE persona cherche sur TikTok ?
- Le style UGC, storytelling ou dynamisme correspond-il aux attentes de CE persona ?

JSON attendu (première partie) :
{
  "rapport_sections": {
    "accroche_visuelle": "...",
    "clarte_message": "...",
    "cta_analyse": "...",
    "coherence_marque": "...",
    "codes_tiktok_persona": "..."
  }
}
"""

PROMPT_ADS_PLAN_3_PART2 = """
Tu es un expert en création publicitaire, stratégie plateforme et psychologie du comportement d'achat.

IMPORTANT : tu dois répondre au format JSON STRICT (et rien d'autre).
Le JSON doit contenir une clé "rapport_sections".

Tu as déjà analysé les premiers éléments de cette pub.
Génère maintenant la deuxième partie du rapport.
Reste cohérent avec la première partie fournie en contexte.
Ton : stratégique, humain, précis. Jamais condescendant.

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

3) recommandations
3 priorités pour CE persona sur CETTE plateforme.

INTERDICTION ABSOLUE DE DÉFAUT : ne propose "ajouter un témoignage/preuve sociale", "rassurer sur le confort/la praticité" ou "ajouter un visuel avant/après" QUE si tu as explicitement identifié dans les sections précédentes (lecture_persona, adequation_persona) que cet élément précis manque ET que c'est le frein principal pour CE persona sur CE produit. Ces 3 idées sont interdites par défaut car trop génériques et reviennent sur presque tous les produits.

À la place, cherche en priorité des leviers spécifiques à la psychologie de CE persona et à CE visuel : reformulation du message dans le vocabulaire exact du persona, ordre de présentation des bénéfices selon ses priorités réelles, ajustement du registre émotionnel (urgence vs réassurance vs aspiration), élément visuel à mettre en avant ou à retirer, objection précise et inédite identifiée dans adequation_persona à traiter directement.

Format OBLIGATOIRE pour chaque priorité :
"Quoi: [action précise et spécifique à ce persona et ce visuel]\\nPourquoi: [impact psychologique sur ce persona]\\nComment: [étapes concrètes]\\nOù: [emplacement sur le visuel]\\nExemple: [dans le registre exact du persona et de la plateforme]"

4) resume_rapide
"Points forts: ...\\nPoints faibles: ...\\nPar où commencer: ..."

JSON attendu (deuxième partie) :
{
  "rapport_sections": {
    "lecture_persona": "...",
    "adequation_persona": "...",
    "recommandations": {
      "priorite_1": "Quoi: ...\\nPourquoi: ...\\nComment: ...\\nOù: ...\\nExemple: ...",
      "priorite_2": "Quoi: ...\\nPourquoi: ...\\nComment: ...\\nOù: ...\\nExemple: ...",
      "priorite_3": "Quoi: ...\\nPourquoi: ...\\nComment: ...\\nOù: ...\\nExemple: ..."
    },
    "resume_rapide": "Points forts: ...\\nPoints faibles: ...\\nPar où commencer: ..."
  }
}
"""

# =========================
# QUOTA : VÉRIFIER ET CONSOMMER UNE ANALYSE
# =========================

def verifier_et_consommer_ads(order_number: str) -> None:
    """Vérifie le quota Ads et incrémente si autorisé, sinon lève une erreur HTTP 403."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT quantite, analyses_utilisees FROM commandes_ads WHERE order_number = %s",
            (order_number,)
        )
        row = cur.fetchone()
        if row is None:
            cur.close()
            conn.close()
            return
        quantite, analyses_utilisees = row
        if analyses_utilisees >= quantite:
            cur.close()
            conn.close()
            raise HTTPException(
                status_code=403,
                detail="Toutes les analyses de cette commande ont déjà été utilisées."
            )
        cur.execute(
            "UPDATE commandes_ads SET analyses_utilisees = analyses_utilisees + 1 WHERE order_number = %s",
            (order_number,)
        )
        conn.commit()
        print(f"Quota Ads incrémenté pour commande #{order_number} ({analyses_utilisees + 1}/{quantite})")
        cur.close()
        conn.close()
    except HTTPException:
        raise
    except Exception as e:
        print(f"Erreur quota commande Ads #{order_number} : {e}")

# =========================
# LOGIQUE OPENAI
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

# =========================
# MODÈLE VÉRIFICATION
# =========================

class VerificationRequest(BaseModel):
    order_number: str
    email: str


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

        if order_number and email:
            line_items = data.get("line_items", [])

            # Détecter le plan Ads le plus élevé commandé
            plan_detecte = 1
            quantite_ads = 0
            for item in line_items:
                variant_id = str(item.get("variant_id", ""))
                qty = int(item.get("quantity", 1))
                if variant_id == VARIANT_ADS_PLAN_3:
                    plan_detecte = 3
                    quantite_ads += qty
                elif variant_id == VARIANT_ADS_PLAN_2 and plan_detecte < 3:
                    plan_detecte = 2
                    quantite_ads += qty
                elif variant_id == VARIANT_ADS_PLAN_1:
                    quantite_ads += qty

            # Ne rien enregistrer si aucun produit Ads dans la commande
            if quantite_ads == 0:
                print(f"Commande #{order_number} : aucun produit Ads détecté, ignorée.")
                return JSONResponse(status_code=200, content={"ok": True})

            # Upsert en base commandes_ads
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO commandes_ads (order_number, email, plan, quantite, analyses_utilisees)
                VALUES (%s, %s, %s, %s, 0)
                ON CONFLICT (order_number) DO UPDATE
                    SET email    = EXCLUDED.email,
                        plan     = EXCLUDED.plan,
                        quantite = EXCLUDED.quantite
            """, (order_number, email, plan_detecte, quantite_ads))
            conn.commit()
            cur.close()
            conn.close()

            print(f"Commande Ads enregistrée : #{order_number} → {email} → Plan {plan_detecte} → Quantité {quantite_ads}")

    except Exception as e:
        print(f"Erreur webhook Ads : {e}")

    return JSONResponse(status_code=200, content={"ok": True})


# =========================
# VÉRIFICATION COMMANDE ADS
# =========================

@app.post("/verifier/commande")
async def verifier_commande(req: VerificationRequest):
    order = req.order_number.strip().lstrip("#")
    email = req.email.strip().lower()

    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM commandes_ads WHERE order_number = %s", (order,))
        commande = cur.fetchone()
        cur.close()
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur base de données : {str(e)}")

    if commande is None:
        raise HTTPException(
            status_code=404,
            detail="Numéro de commande introuvable. Vérifiez votre email de confirmation.",
        )

    if commande["email"] != email:
        raise HTTPException(
            status_code=403,
            detail="L'email ne correspond pas à cette commande.",
        )

    analyses_restantes = commande["quantite"] - commande["analyses_utilisees"]

    if analyses_restantes <= 0:
        raise HTTPException(
            status_code=403,
            detail="Toutes les analyses de cette commande ont déjà été utilisées.",
        )

    return {
        "ok": True,
        "message": "Accès autorisé",
        "email": email,
        "order_number": order,
        "plan": commande["plan"],
        "quantite": commande["quantite"],
        "analyses_utilisees": commande["analyses_utilisees"],
        "analyses_restantes": analyses_restantes,
    }

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
        3: "Plan Avancée Persona Ads"
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
# ROUTES
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
    order_number: str = Form(...)
):
    verifier_et_consommer_ads(order_number.strip().lstrip("#"))
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
    order_number: str = Form(...)
):
    if plateforme not in ["meta", "tiktok"]:
        raise HTTPException(status_code=400, detail="Plateforme invalide. Valeurs acceptées : meta, tiktok.")
    verifier_et_consommer_ads(order_number.strip().lstrip("#"))
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
    order_number: str = Form(...)
):
    if plateforme not in ["meta", "tiktok"]:
        raise HTTPException(status_code=400, detail="Plateforme invalide. Valeurs acceptées : meta, tiktok.")
    if not persona or not persona.strip():
        raise HTTPException(status_code=400, detail="Persona manquant pour ce plan.")
    verifier_et_consommer_ads(order_number.strip().lstrip("#"))
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