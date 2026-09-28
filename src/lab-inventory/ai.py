"""Appels aux IA (Claude, ChatGPT, Gemini, Mistral) : identification par photo et recherche d'images."""
import base64
import json
import re

import anthropic
import httpx

PROMPT = """Tu reçois une ou plusieurs photos d'un même composant électronique (ou de son emballage / étiquette).
1. Identifie le composant : lis les marquages, le boîtier, le logo du fabricant.
2. Utilise la recherche web pour trouver la référence exacte, la datasheet officielle (PDF de préférence,
   sur le site du fabricant) et la page produit du fabricant.
3. Réponds UNIQUEMENT avec un objet JSON (sans texte autour) de la forme :
{
  "name": "nom court lisible, ex. 'Régulateur de tension 5V'",
  "part_number": "référence exacte, ex. 'LM7805CT'",
  "manufacturer": "fabricant",
  "category": "une catégorie parmi : __CATEGORIES__",
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

NO_SEARCH_NOTE = """
Tu n'as pas accès à la recherche web : ne donne une URL que si tu es certain qu'elle existe, sinon null."""

IMAGES_PROMPT = """Trouve sur le web des photos produit du composant électronique suivant :
{desc}
Cherche sur les sites des fabricants et des distributeurs (Mouser, DigiKey, Farnell, LCSC, RS, Adafruit, SparkFun…)
ou via une recherche d'images. Je veux les URL DIRECTES des fichiers image (se terminant généralement par .jpg, .jpeg,
.png ou .webp), pas les pages web qui les contiennent. Privilégie des photos nettes sur fond neutre du composant lui-même.
Réponds UNIQUEMENT avec un objet JSON (sans texte autour) de la forme :
{{"images": [{{"url": "URL directe de l'image", "page": "URL de la page où tu l'as trouvée"}}]}}
Donne jusqu'à 10 images, uniquement des URL que tu as réellement vues pendant la recherche."""

TIMEOUT = httpx.Timeout(180.0, connect=15.0)

# Fournisseurs proposés dans les paramètres. Le modèle par défaut reste modifiable par l'utilisateur.
PROVIDERS = {
    "claude": {"label": "Claude (Anthropic)", "default_model": "claude-opus-5-5", "web_search": True,
               "key_url": "https://console.anthropic.com/settings/keys"},
    "openai": {"label": "ChatGPT (OpenAI)", "default_model": "gpt-5", "web_search": True,
               "key_url": "https://platform.openai.com/api-keys"},
    "gemini": {"label": "Gemini (Google)", "default_model": "gemini-2.5-flash", "web_search": True,
               "key_url": "https://aistudio.google.com/apikey"},
    "mistral": {"label": "Mistral AI", "default_model": "mistral-medium-latest", "web_search": False,
                "key_url": "https://console.mistral.ai/api-keys"},
}


DEFAULT_CATEGORIES = ["Résistance", "Condensateur", "Inductance", "Diode", "LED", "Transistor", "Circuit intégré",
                      "Microcontrôleur", "Régulateur", "Capteur", "Module", "Connecteur", "Interrupteur", "Relais",
                      "Afficheur", "Quartz/Oscillateur", "Câble", "Autre"]


def identify(images: list[tuple[bytes, str]], provider: str, model: str, api_key: str,
             categories: list[str] | None = None) -> dict:
    """images : liste de (contenu, type MIME) d'un même composant ; categories : celles définies dans les paramètres."""
    prompt = PROMPT.replace("__CATEGORIES__", ", ".join(categories or DEFAULT_CATEGORIES))
    if not PROVIDERS[provider]["web_search"]:
        prompt += NO_SEARCH_NOTE
    encoded = [(base64.b64encode(data).decode(), media_type) for data, media_type in images]
    return _parse_json(_call(provider, model, api_key, prompt, encoded))


def find_images(desc: str, provider: str, model: str, api_key: str) -> list[dict]:
    """Demande à l'IA (avec recherche web) des URL d'images du composant décrit."""
    data = _parse_json(_call(provider, model, api_key, IMAGES_PROMPT.format(desc=desc), []))
    return [i for i in data.get("images", []) if isinstance(i, dict) and i.get("url")]


def _call(provider, model, api_key, prompt, images):
    fn = {"claude": _claude, "openai": _openai, "gemini": _gemini, "mistral": _mistral}.get(provider)
    if not fn:
        raise RuntimeError(f"Fournisseur d'IA inconnu : {provider}")
    return fn(images, model or PROVIDERS[provider]["default_model"], api_key, prompt)


def _claude(images, model, api_key, prompt):
    client = anthropic.Anthropic(api_key=api_key)
    content = [{"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}}
               for b64, mt in images]
    messages = [{"role": "user", "content": content + [{"type": "text", "text": prompt}]}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}]

    # Un tour long de recherche web peut s'arrêter en "pause_turn" : on le relance.
    for _ in range(4):
        response = client.beta.messages.create(
            model=model,
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
        raise RuntimeError("L'IA a refusé d'analyser cette demande.")
    return "".join(b.text for b in response.content if b.type == "text")


def _openai(images, model, api_key, prompt):
    content = [{"type": "input_image", "image_url": f"data:{mt};base64,{b64}"} for b64, mt in images]
    r = httpx.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "tools": [{"type": "web_search"}],
            "input": [{"role": "user", "content": content + [{"type": "input_text", "text": prompt}]}],
        },
        timeout=TIMEOUT)
    data = _check(r)
    return "".join(c.get("text", "") for item in data.get("output", []) if item.get("type") == "message"
                   for c in item.get("content", []) if c.get("type") == "output_text")


def _gemini(images, model, api_key, prompt):
    parts = [{"inline_data": {"mime_type": mt, "data": b64}} for b64, mt in images]
    r = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": api_key},
        json={
            "contents": [{"parts": parts + [{"text": prompt}]}],
            "tools": [{"google_search": {}}],
        },
        timeout=TIMEOUT)
    data = _check(r)
    parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts)


def _mistral(images, model, api_key, prompt):
    content = [{"type": "image_url", "image_url": f"data:{mt};base64,{b64}"} for b64, mt in images]
    r = httpx.post(
        "https://api.mistral.ai/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": content + [{"type": "text", "text": prompt}]}],
            "response_format": {"type": "json_object"},
        },
        timeout=TIMEOUT)
    data = _check(r)
    return data["choices"][0]["message"]["content"]


def _check(r: httpx.Response) -> dict:
    """Renvoie le JSON de la réponse, ou lève une erreur lisible (clé invalide, modèle inconnu…)."""
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.is_error:
        err = data.get("error") if isinstance(data, dict) else None
        msg = (err.get("message") if isinstance(err, dict) else err) or data.get("message") or r.text[:200]
        raise RuntimeError(f"HTTP {r.status_code} : {msg}")
    return data


def _parse_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        raise RuntimeError("Réponse de l'IA illisible : " + (text or "")[:300])
    data = json.loads(match.group(0))
    if not isinstance(data.get("specs"), dict):
        data["specs"] = {}
    return data
