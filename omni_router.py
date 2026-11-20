"""Provider selection and chat completion routing for jrocai."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_PROVIDERS = ("openrouter", "gemini", "groq", "github_models", "ollama")
PROVIDER_MODELS = {
    "openrouter": ("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free"),
    "gemini": ("GEMINI_MODEL", "gemini-2.5-flash"),
    "groq": ("GROQ_MODEL", "llama-3.3-70b-versatile"),
    "github_models": ("GITHUB_MODELS_MODEL", "openai/gpt-4o-mini"),
    "ollama": ("OLLAMA_MODEL", "llama3.2"),
}
PROVIDER_KEYS = {
    "openrouter": ("OPENROUTER_API_KEY",),
    "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "groq": ("GROQ_API_KEY",),
    "github_models": ("GITHUB_TOKEN",),
    "ollama": (),
}
OPENAI_COMPATIBLE_ENDPOINTS = {
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "github_models": "https://models.github.ai/inference/chat/completions",
}


class ProviderError(RuntimeError):
    """A provider request failed; status is populated for HTTP responses."""

    def __init__(self, provider: str, message: str, status: int | None = None):
        self.provider = provider
        self.status = status
        super().__init__(f"{provider}: {message}")


@dataclass(frozen=True)
class Provider:
    name: str
    model: str
    api_key: str | None


def _configured_provider(name: str) -> Provider | None:
    if name not in PROVIDER_MODELS:
        raise ValueError(f"Unknown model provider: {name}")
    model_env, default_model = PROVIDER_MODELS[name]
    key = next((os.environ[key_name] for key_name in PROVIDER_KEYS[name]
                if os.environ.get(key_name)), None)
    if PROVIDER_KEYS[name] and key is None:
        return None
    model = os.environ.get(model_env, default_model).strip()
    if not model:
        raise ValueError(f"{model_env} must not be empty")
    return Provider(name=name, model=model, api_key=key)


def configured_providers() -> list[dict[str, Any]]:
    """Describe provider readiness without returning credentials."""
    result = []
    for name in DEFAULT_PROVIDERS:
        provider = _configured_provider(name)
        model_env, default_model = PROVIDER_MODELS[name]
        result.append({
            "name": name,
            "configured": provider is not None,
            "model": provider.model if provider else os.environ.get(model_env, default_model),
        })
    return result


def _provider_order(preferred: str | None = None) -> list[str]:
    configured_order = os.environ.get("JROCAI_PROVIDERS", "")
    requested = [item.strip().lower() for item in configured_order.split(",") if item.strip()]
    if preferred:
        requested.insert(0, preferred.strip().lower())
    order = []
    for name in requested + list(DEFAULT_PROVIDERS):
        if name not in PROVIDER_MODELS:
            raise ValueError(f"Unknown model provider: {name}")
        if name not in order:
            order.append(name)
    return order


def _request_json(
    provider: str,
    url: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 45,
) -> dict[str, Any]:
    request_headers = {"Accept": "application/json"}
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=body, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        message = f"HTTP {error.code}" + (f": {detail}" if detail else "")
        raise ProviderError(provider, message, status=error.code) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ProviderError(provider, f"request failed: {error}") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderError(provider, f"returned invalid JSON: {error}") from error
    if not isinstance(result, dict):
        raise ProviderError(provider, "returned a non-object response")
    return result


def _openai_compatible_completion(provider: Provider, prompt: str, system_prompt: str | None) -> str:
    if provider.name == "ollama":
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1").rstrip("/")
        endpoint = f"{base_url}/chat/completions"
        api_key = os.environ.get("OLLAMA_API_KEY", "ollama")
    else:
        endpoint = OPENAI_COMPATIBLE_ENDPOINTS[provider.name]
        api_key = provider.api_key
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    if provider.name == "openrouter":
        headers["X-Title"] = "jrocai"
    response = _request_json(
        provider.name,
        endpoint,
        {"model": provider.model, "messages": messages},
        headers,
    )
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise ProviderError(provider.name, "response is missing choices[0].message.content") from error
    if not isinstance(content, str) or not content.strip():
        raise ProviderError(provider.name, "returned an empty completion")
    return content.strip()


def _gemini_completion(provider: Provider, prompt: str, system_prompt: str | None) -> str:
    if not provider.api_key:
        raise ProviderError("gemini", "API key is not configured")
    model = urllib.parse.quote(provider.model, safe="")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    contents = [{"role": "user", "parts": [{"text": prompt}]}]
    payload: dict[str, Any] = {"contents": contents}
    if system_prompt:
        payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}
    response = _request_json("gemini", url, payload, {"x-goog-api-key": provider.api_key})
    try:
        parts = response["candidates"][0]["content"]["parts"]
        content = "".join(part["text"] for part in parts if isinstance(part.get("text"), str))
    except (KeyError, IndexError, TypeError) as error:
        raise ProviderError("gemini", "response is missing candidate text") from error
    if not content.strip():
        raise ProviderError("gemini", "returned an empty completion")
    return content.strip()


def _complete(provider: Provider, prompt: str, system_prompt: str | None) -> str:
    if provider.name == "gemini":
        return _gemini_completion(provider, prompt, system_prompt)
    return _openai_compatible_completion(provider, prompt, system_prompt)


def generate_response(
    prompt: str,
    system_prompt: str | None = None,
    provider: str | None = None,
) -> str:
    """Generate a response, failing over across configured providers."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    if system_prompt is not None and not isinstance(system_prompt, str):
        raise ValueError("system_prompt must be a string or None")

    errors = []
    available = 0
    for name in _provider_order(provider):
        configured = _configured_provider(name)
        if configured is None:
            continue
        available += 1
        try:
            return _complete(configured, prompt, system_prompt)
        except ProviderError as error:
            errors.append(str(error))
    if not available:
        raise RuntimeError("No model providers are configured; set a provider API key or start Ollama")
    raise RuntimeError("All configured model providers failed: " + "; ".join(errors))
