"""Sticky-by-user router hook for LiteLLM.

Rewrites unsuffixed Claude model names to their `-a` / `-b` siblings based on
a stable hash of the caller identity. Each -a/-b variant is a distinct
model_name in the router, allowing LiteLLM's native fallback mechanism to
handle failover. -a/-b are still advertised by /v1/models because LiteLLM
OSS has no "hide from listing" mechanism; document them as power-user paths.
"""
import hashlib
from litellm.integrations.custom_logger import CustomLogger

STICKY_MODELS = {"claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"}


def _suffix_for(identity: str) -> str:
    h = int(hashlib.md5(identity.encode()).hexdigest(), 16)
    return "-a" if (h & 1) == 0 else "-b"


class StickyRouter(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        model = data.get("model")
        if model in STICKY_MODELS:
            identity = (
                getattr(user_api_key_dict, "user_id", None)
                or getattr(user_api_key_dict, "team_id", None)
                or getattr(user_api_key_dict, "api_key", None)
                or "anon"
            )
            data["model"] = f"{model}{_suffix_for(identity)}"
        return data


proxy_handler_instance = StickyRouter()
