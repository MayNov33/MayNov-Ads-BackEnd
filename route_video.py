import base64
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import APIRouter, UploadFile, Form, File, HTTPException
from openai import OpenAI

router = APIRouter()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ============================================================
# CONFIGURATION VIDÉO
# ============================================================

MODELE = os.getenv("OPENAI_VIDEO_MODEL", "gpt-4o")
INTERVALLE_SECONDES = 1.5
NB_IMAGES_MIN = 8
NB_IMAGES_MAX = 20
MAX_TOKENS_SORTIE = 4000

TAILLE_VIDEO_MAX_MO = 100
DUREE_VIDEO_MAX_SECONDES = 30
FORMATS_ACCEPTES = {".mp4"}

TYPES_CONTENU_VALIDES = {"promo_marque", "lifestyle_engagement", "pub_payante"}

ANGLE_PAR_TYPE = {
    "promo_marque": "Une marque présente son propre produit avec un objectif de conversion, de visite du site ou de découverte de l'offre.",
    "lifestyle_engagement": "Un contenu qui montre un produit dans un contexte de vie pour générer de l'engagement. Un CTA de vente explicite n'est PAS attendu ici — ne pénalise jamais son absence.",
    "pub_payante": "Une publicité display classique (Meta/TikTok Ads). Analyse selon la grille hook / preuve / objection / CTA habituelle de la publicité payante.",
}


# ============================================================
# CRÉDITS — À BRANCHER QUAND LE SERVICE DE TOM SERA PRÊT
# ============================================================

def verifier_et_consommer_credits_video(email: str, code: str) -> bool:
    """
    Stub temporaire : autorise toujours l'analyse.
    À remplacer par un appel réel à maynov-abonnements-backend
    une fois ce service disponible (vérification + décrément des crédits).
    """
    return True


# ============================================================
# PROMPT (v9 — figé, voir /areas/maynov-video-ads dans les notes)
# ============================================================

def construire_prompt_systeme(type_contenu: str) -> str:
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


# ============================================================
# TRAITEMENT VIDÉO (FFmpeg)
# ============================================================

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


# ============================================================
# ROUTE
# ============================================================

@router.post("/analyser/video")
async def analyser_video_route(
    file: UploadFile = File(...),
    email: str = Form(...),
    code: str = Form(...),
    type_contenu: str = Form(...),
):
    if type_contenu not in TYPES_CONTENU_VALIDES:
        raise HTTPException(status_code=400, detail="Type de contenu invalide.")

    if Path(file.filename).suffix.lower() not in FORMATS_ACCEPTES:
        raise HTTPException(status_code=400, detail="Seul le format MP4 est accepté.")

    if not verifier_et_consommer_credits_video(email, code):
        raise HTTPException(status_code=403, detail="Crédits insuffisants ou code invalide.")

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

        try:
            reponse = client.chat.completions.create(
                model=MODELE,
                temperature=0.3,
                max_completion_tokens=MAX_TOKENS_SORTIE,
                messages=[
                    {"role": "system", "content": construire_prompt_systeme(type_contenu)},
                    {"role": "user", "content": contenu_message},
                ],
            )
        except Exception:
            raise HTTPException(status_code=502, detail="Erreur lors de la génération du rapport. Réessayez dans quelques instants.")

        rapport = reponse.choices[0].message.content
        if not rapport:
            raise HTTPException(status_code=502, detail="Aucun rapport n'a pu être généré. Réessayez.")

        return {
            "rapport_texte": rapport,
            "usage": {
                "modele": MODELE,
                "duree_video_secondes": round(duree, 2),
                "nombre_images": len(frames),
                "tokens_total": getattr(reponse.usage, "total_tokens", None),
            },
        }
