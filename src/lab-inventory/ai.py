"""Identification d'un composant à partir d'une photo, via Claude + recherche web."""
import base64
import json
import re

import anthropic

MODEL = "claude-opus-5-5"

PROMPT = """Tu reçois la photo d'un composant électronique (ou de son emballage / étiquette).
1. Identifie le composant : lis les marquages, le boîtier, le logo du fabricant.
2. Utilise la recherche web pour trouver la référence exacte, la datasheet officielle (PDF de préférence,
   sur le site du fabricant) et la page produit du fabricant.
3. Réponds UNIQUEMENT avec un objet JSON (sans texte autour) de la forme :
{
  "name": "nom court lisible, ex. 'Régulateur de tension 5V'",
  "part_number": "référence exacte, ex. 'LM7805CT'",
  "manufacturer": "fabricant",
  "category": "une catégorie parmi : Résistance, Condensateur, Inductance, Diode, LED, Transistor, Circuit intégré, Microcontrôleur, Régulateur, Capteur, Module, Connecteur, Interrupteur, Relais, Afficheur, Quartz/Oscillateur, Câble, Autre",
  "package": "boîtier, ex. TO-220, DIP-8, SMD 0805",
  "description": "2 ou 3 phrases en français sur ce que fait le composant",
  "specs": {"caractéristique": "valeur", "...": "..."},
  "datasheet_url": "URL de la datasheet ou null",
  "manufacturer_url": "URL de la page produit ou null",
  "confidence": "haute | moyenne | faible",
  "notes": "doutes éventuels, marquages illisibles, alternatives possibles"
}
Les specs doivent contenir 3 à 8 caractéristiques clés (tension, courant, valeur, tolérance, brochage...).
Ne mets que des URL que tu as réellement trouvées pendant la recherche."""


def identify(image_bytes: bytes, media_type: str, api_key: str) -> dict:
    client = anthropic.Anthropic(api_key=api_key)
    messages = [{
        "role": "user",
        "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                         "data": base64.b64encode(image_bytes).decode()}},
            {"type": "text", "text": PROMPT},
        ],
    }]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}]

    # Un tour long de recherche web peut s'arrêter en "pause_turn" : on le relance.
    for _ in range(4):
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            tools=tools,
            messages=messages,
        )
        if response.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": response.content})

    if response.stop_reason == "refusal":
        raise RuntimeError("L'IA a refusé d'analyser cette image.")

    text = "".join(b.text for b in response.content if b.type == "text")
    return _parse_json(text)


def _parse_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise RuntimeError("Réponse de l'IA illisible : " + text[:300])
    data = json.loads(match.group(0))
    if not isinstance(data.get("specs"), dict):
        data["specs"] = {}
    return data
