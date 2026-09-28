"""Identification d'un composant à partir d'une photo, via une IA (Claude, ChatGPT, Gemini, Mistral)."""
import base64
import json
import re

import anthropic
import httpx

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

NO_SEARCH_NOTE = """
Tu n'as pas accès à la recherche web : ne donne une URL que si tu es certain qu'elle existe, sinon null."""

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


def identify(image_bytes: bytes, media_type: str, provider: str, model: str, api_key: str) -> dict:
    fn = {"claude": _claude, "openai": _openai, "gemini": _gemini, "mistral": _mistral}.get(provider)
    if not fn:
        raise RuntimeError(f"Fournisseur d'IA inconnu : {provider}")
    b64 = base64.b64encode(image_bytes).decode()
    prompt = PROMPT if PROVIDERS[provider]["web_search"] else PROMPT + NO_SEARCH_NOTE
    return _parse_json(fn(b64, media_type, model or PROVIDERS[provider]["default_model"], api_key, prompt))


def _claude(b64, media_type, model, api_key, prompt):
    client = anthropic.Anthropic(api_key=api_key)
    messages = [{
        "role": "user",
        "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
            {"type": "text", "text": prompt},
        ],
    }]
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
        raise RuntimeError("L'IA a refusé d'analyser cette image.")
    return "".join(b.text for b in response.content if b.type == "text")


def _openai(b64, media_type, model, api_key, prompt):
    r = httpx.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "tools": [{"type": "web_search"}],
            "input": [{"role": "user", "content": [
                {"type": "input_image", "image_url": f"data:{media_type};base64,{b64}"},
                {"type": "input_text", "text": prompt},
            ]}],
        },
        timeout=TIMEOUT)
    data = _check(r)
    return "".join(c.get("text", "") for item in data.get("output", []) if item.get("type") == "message"
                   for c in item.get("content", []) if c.get("type") == "output_text")


def _gemini(b64, media_type, model, api_key, prompt):
    r = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": api_key},
        json={
            "contents": [{"parts": [{"inline_data": {"mime_type": media_type, "data": b64}},
                                    {"text": prompt}]}],
            "tools": [{"google_search": {}}],
        },
        timeout=TIMEOUT)
    data = _check(r)
    parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts)


def _mistral(b64, media_type, model, api_key, prompt):
    r = httpx.post(
        "https://api.mistral.ai/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": f"data:{media_type};base64,{b64}"},
                {"type": "text", "text": prompt},
            ]}],
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
