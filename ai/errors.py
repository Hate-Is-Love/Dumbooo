"""Provider-independent AI errors.

Each provider adapter translates its SDK's exceptions into these classes, so
the controller and dashboard handle "quota exhausted" the same way whether it
came from Gemini or OpenAI. ``user_message`` is short and safe to show;
``detail`` is for logs and never contains API keys.
"""

from __future__ import annotations


class AIError(Exception):
    kind = "error"
    retryable = False          # may a limited, automatic retry help?
    user_message = "The AI request failed."

    def __init__(self, user_message: str | None = None, *, detail: str = "", provider: str = "",
                 retry_after: float | None = None) -> None:
        self.user_message = user_message or self.user_message
        self.detail = detail
        self.provider = provider
        self.retry_after = retry_after
        super().__init__(self.user_message)


class ConfigError(AIError):
    kind = "config"
    user_message = "The AI provider is not configured."


class AuthError(AIError):
    kind = "auth"
    user_message = "The API key was rejected. Check that it is correct and active."


class QuotaError(AIError):
    """The account's quota or credit is used up. Waiting a minute will not fix it."""
    kind = "quota"
    user_message = "The AI provider's quota for this account is used up."


class RateLimitError(AIError):
    """Too many requests in a short window; retrying after a pause usually works."""
    kind = "rate_limit"
    retryable = True
    user_message = "Too many requests right now. Please wait a moment."


class ModelNotFoundError(AIError):
    kind = "model"
    user_message = "The configured model is not available to this account."


class UnsupportedParameterError(AIError):
    """The model rejected an optional parameter (temperature, thinking…)."""
    kind = "unsupported_parameter"

    def __init__(self, parameter: str, **kw) -> None:
        self.parameter = parameter
        super().__init__(f"The model does not support the '{parameter}' setting.", **kw)


class BadRequestError(AIError):
    kind = "bad_request"
    user_message = "The AI provider rejected the request."


class InputTooLongError(AIError):
    kind = "too_long"
    user_message = "The conversation is too long for this model. Clear the conversation and try again."


class TimeoutError_(AIError):  # noqa: N801 - avoid shadowing the builtin
    kind = "timeout"
    retryable = True
    user_message = "The AI provider took too long to answer."


class NetworkError(AIError):
    kind = "network"
    retryable = True
    user_message = "Could not reach the AI provider. Check your internet connection."


class ProviderUnavailableError(AIError):
    kind = "unavailable"
    retryable = True
    user_message = "The AI provider is temporarily unavailable."


class BlockedError(AIError):
    kind = "blocked"
    user_message = "The provider's safety filter blocked this response."


class EmptyResponseError(AIError):
    kind = "empty"
    user_message = "The AI returned an empty answer."


class MalformedResponseError(AIError):
    kind = "malformed"
    user_message = "The AI provider sent a response DUMBOO could not read."


class CancelledError(AIError):
    kind = "cancelled"
    user_message = "Cancelled."
