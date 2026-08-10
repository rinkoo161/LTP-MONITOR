"""
Unified LLM layer. Prefers a LOCAL model via Ollama (zero cost, runs on your
machine, no data leaves it). Falls back to the Anthropic API only if you
explicitly enable it and local is unavailable.

Setup Ollama (one time):
  1. Install from https://ollama.com  (macOS: `brew install ollama`)
  2. Pull a model:   ollama pull llama3.1        (or qwen2.5, mistral)
  3. Ollama serves on http://localhost:11434 automatically.

In Settings choose AI engine:
  - "local"  -> Ollama only (recommended; free)
  - "online" -> Anthropic API only (costs tokens)
  - "auto"   -> try local first, fall back to online if local is down
  - "off"    -> rule engine only, no LLM at all
"""

import json
import os
import urllib.request
import urllib.error

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")


class LLMError(Exception):
    pass


class LLMAuthError(LLMError):
    pass


def _ollama_available(model: str) -> bool:
    try:
        req = urllib.request.Request(OLLAMA_URL + "/api/tags")
        with urllib.request.urlopen(req, timeout=3) as r:
            tags = json.loads(r.read())
        names = [m.get("name", "") for m in tags.get("models", [])]
        # accept exact or prefix match (llama3.1 matches llama3.1:latest)
        return any(n == model or n.startswith(model + ":") or
                   n.split(":")[0] == model for n in names)
    except Exception:
        return False


def _ollama_json(prompt: str, model: str, max_tokens: int = 600):
    """Ask a local Ollama model for JSON. Retries once on HTTP 500 with a
    stricter JSON-only prompt — small models sometimes emit malformed output
    that Ollama's format=json validator rejects."""
    import config as _cfg
    cfg = _cfg.load()

    def _call(p, budget):
        body = json.dumps({
            "model": model,
            "prompt": p,
            "stream": False,
            "format": "json",
            "keep_alive": cfg.get("ollama_keep_alive", "2m"),
            "options": {
                "temperature": 0.2,
                "num_predict": budget,
                "num_thread": cfg.get("ollama_num_thread", 4),
                "num_ctx": cfg.get("ollama_num_ctx", 2048),
            },
        }).encode()
        req = urllib.request.Request(
            OLLAMA_URL + "/api/generate", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=cfg.get("ollama_timeout", 60)) as r:
            j = json.loads(r.read())
            return (j.get("response", "").strip(),
                    j.get("done_reason"))

    def _call_with_length_retry(p):
        # v59.78 — TRUNCATION retry. 2026-08-10: every signal-engine
        # call failed all session with "Unterminated string … char
        # ~840" — the model answered in PRETTY-PRINTED JSON, the
        # newlines/indentation burned the 400-token budget, Ollama cut
        # the output mid-string (done_reason "length"), and the rule
        # engine traded alone all day. Ollama TELLS us when it
        # truncated; a cut JSON is never parseable, so retrying once at
        # double the budget (capped) is strictly better than returning
        # bytes we know are broken.
        text, done = _call(p, max_tokens)
        if done == "length":
            text, done = _call(p, min(max_tokens * 2, 2000))
        return text

    try:
        return _call_with_length_retry(prompt)
    except urllib.error.HTTPError as e:
        if e.code != 500:
            raise
        # Retry once with a much stricter, shorter prompt
        strict = ("You must reply with a single valid JSON object and "
                  "nothing else. No prose, no markdown, no commentary. "
                  "The JSON schema and data follow:\n\n" + prompt)
        return _call_with_length_retry(strict)


def _anthropic_json(prompt: str, api_key: str, max_tokens: int = 600):
    body = json.dumps({
        "model": "claude-sonnet-4-6", "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"Content-Type": "application/json", "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read())
        text = "".join(b.get("text", "") for b in data.get("content", []))
        return text.replace("```json", "").replace("```", "").strip()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise LLMAuthError("Anthropic key invalid/expired") from e
        raise LLMError(f"HTTP {e.code} from Anthropic") from e


def generate_json(prompt: str, max_tokens: int = 600):
    """Return (text, engine_used, error). Honors the Settings AI engine
    choice. Never raises — errors come back in the tuple."""
    import config as _cfg
    cfg = _cfg.load()
    engine = cfg.get("ai_engine", "local")
    model = cfg.get("ollama_model", "llama3.1")
    api_key = cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")

    if engine == "off":
        return None, None, "ai_off"

    def try_local():
        if not _ollama_available(model):
            return None, f"Ollama/model '{model}' not reachable — is `ollama serve` running and `ollama pull {model}` done?"
        try:
            return _ollama_json(prompt, model, max_tokens), None
        except Exception as e:
            return None, f"Ollama error: {e}"

    def try_online():
        if not api_key:
            return None, "no Anthropic key set"
        try:
            return _anthropic_json(prompt, api_key, max_tokens), None
        except LLMAuthError as e:
            return None, str(e)
        except Exception as e:
            return None, str(e)

    if engine == "local":
        text, err = try_local()
        return (text, "local", None) if text else (None, None, err)
    if engine == "online":
        text, err = try_online()
        return (text, "online", None) if text else (None, None, err)
    # auto: local first, then online
    text, err = try_local()
    if text:
        return text, "local", None
    text2, err2 = try_online()
    if text2:
        return text2, "online", None
    return None, None, f"local: {err}; online: {err2}"


def health():
    """Status of both engines for the dashboard."""
    import config as _cfg
    cfg = _cfg.load()
    model = cfg.get("ollama_model", "llama3.1")
    return {
        "engine": cfg.get("ai_engine", "local"),
        "ollama_model": model,
        "ollama_up": _ollama_available(model),
        "online_key_set": bool(cfg.get("anthropic_api_key")
                               or os.environ.get("ANTHROPIC_API_KEY")),
    }
