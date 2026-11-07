"""Reasoning levels shared by the model picker, config validation and routing."""

from pydantic_ai.profiles.openai import openai_model_profile

LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def reasoning_options(reference):
    if not reference.startswith("openai:"):
        name = reference.removeprefix("deepseek:")
        if name in {"deepseek-flash", "deepseek-v4-flash", "deepseek-v4-pro"}:
            return ["none", "low", "high", "max"]
        return []

    name = reference.removeprefix("openai:")
    profile = openai_model_profile(name)
    if not profile.get("supports_thinking"):
        return []
    supported = profile.get("openai_reasoning_efforts")
    if supported is None:
        supported = {"low", "medium", "high"}
        if profile.get("openai_supports_reasoning_effort_none"):
            supported.add("none")
        if name.startswith(
            (
                "gpt-5.2",
                "gpt-5.3",
                "gpt-5.4",
                "gpt-5.5",
                "gpt-5.6",
                "gpt-6",
                "gpt-5.1-codex-max",
            )
        ):
            supported.add("xhigh")
        if profile.get("openai_responses_supports_reasoning_mode"):
            supported.add("max")
    return [level for level in LEVELS if level in supported]


def selected_effort(settings, purpose):
    if not settings:
        return None
    efforts = settings.get("reasoning_efforts", {})
    if purpose in efforts or settings["task_models"].get(purpose):
        return efforts.get(purpose)
    return efforts.get("default")
