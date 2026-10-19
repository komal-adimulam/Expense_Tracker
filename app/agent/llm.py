"""Pick the chat model from whatever API key the user supplied."""
from langchain_core.language_models.chat_models import BaseChatModel

from app.config import settings

DEFAULT_MODELS = {
    "gemini": "gemini-3.5-flash-lite",
    "openai": "gpt-4.1-mini",
    "anthropic": "claude-haiku-5-5",
    "groq": "llama-3.3-70b-versatile",
}


LLM_TIMEOUT = 25  # seconds per request: fail fast instead of typing forever


class LLMConfigError(RuntimeError):
    pass


def detect_provider(api_key: str) -> str | None:
    key = api_key.strip()
    if key.startswith(("AIza", "AQ.")):
        return "gemini"
    if key.startswith("sk-ant-"):
        return "anthropic"
    if key.startswith("gsk_"):
        return "groq"
    if key.startswith("sk-"):
        return "openai"
    return None


def _clean(key: str) -> str:
    return key.strip().strip("\"'").strip()


def get_llm() -> BaseChatModel:
    key = _clean(settings.llm_api_key)
    provider = settings.llm_provider.strip().lower()
    if not key:  # fall back to the providers' conventional variable names
        for prov, value in (("gemini", settings.google_api_key), ("openai", settings.openai_api_key),
                            ("anthropic", settings.anthropic_api_key), ("groq", settings.groq_api_key)):
            if _clean(value):
                key, provider = _clean(value), prov
                break
    if not key:
        raise LLMConfigError("LLM_API_KEY is empty. Put a Gemini / OpenAI / Anthropic / Groq key in .env")

    if provider == "auto":
        provider = detect_provider(key) or ""
        if not provider:
            raise LLMConfigError(
                f"Could not recognise the API key (starts with '{key[:4]}...', {len(key)} chars). "
                "Set LLM_PROVIDER=gemini|openai|anthropic|groq in .env."
            )
    if provider not in DEFAULT_MODELS:
        raise LLMConfigError(f"Unknown LLM_PROVIDER '{provider}'. Use one of {sorted(DEFAULT_MODELS)}.")

    model = settings.llm_model or DEFAULT_MODELS[provider]
    temp = settings.llm_temperature

    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        # 'thinking' adds seconds to every call and gives nothing for ledger entry -> turn it down
        thinking = (
            {"thinking_level": "low"} if model.startswith("gemini-3")
            else {"thinking_budget": 0} if model.startswith("gemini-2.5") and "pro" not in model
            else {}
        )
        return ChatGoogleGenerativeAI(
            model=model, google_api_key=key, temperature=temp, timeout=LLM_TIMEOUT, max_retries=1, **thinking
        )
    if provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model, api_key=key, temperature=temp, timeout=LLM_TIMEOUT, max_retries=1)
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model, api_key=key, temperature=temp, timeout=LLM_TIMEOUT, max_retries=1)
    from langchain_groq import ChatGroq
    return ChatGroq(model=model, api_key=key, temperature=temp, timeout=LLM_TIMEOUT, max_retries=1)
