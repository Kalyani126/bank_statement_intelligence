"""TypeSafe AI (Jev) client adapter - the real integration point.

Jev is TypeSafe's decision model: you POST structured state plus typed
"questions" to the System One endpoint and it answers each question with a
choice, probabilities and a confidence - it does not generate text.

Real API contract (verified against public docs):
    POST {TYPESAFE_API_BASE}/v1/systemone
    Authorization: Bearer $TYPESAFE_API_KEY
    {"state": "...", "model": "jev-latest", "questions": {...}}
    -> {"model": "jev-1.13.0", "answers": {key: {"type": "choice",
        "choice": "...", "probabilities": {...}, "confidence": 0.82}}, ...}

Docs: https://docs.litellm.ai/docs/pass_through/typesafe
      https://pydantic.dev/docs/ai/models/typesafe/

If credentials are not configured this module refuses to call anything and
raises JEVNotConfiguredError - it NEVER fabricates a JEV response.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

_SYSTEMONE_PATH = "/v1/systemone"


class JEVError(Exception):
    """Base error with a message safe to surface (no secrets)."""


class JEVNotConfiguredError(JEVError):
    """JEV is enabled but real TypeSafe credentials are missing."""


class JEVUnavailableError(JEVError):
    """The TypeSafe API could not be reached or answered with an error."""


class JEVInvalidResponseError(JEVError):
    """The TypeSafe API answered with something we cannot trust."""


class JEVClient(Protocol):
    def evaluate(self, *, state: str, questions: dict[str, Any]) -> dict[str, Any]:
        """Send structured questions to the decision model and return the raw response."""
        ...


class TypeSafeJEVClient:
    """HTTP adapter for TypeSafe's Jev System One endpoint."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.typesafe.ai",
        model: str = "jev-latest",
        timeout: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    @property
    def model(self) -> str:
        return self._model

    def evaluate(self, *, state: str, questions: dict[str, Any]) -> dict[str, Any]:
        if not self._api_key:
            raise JEVNotConfiguredError("TYPESAFE_API_KEY is not configured.")

        url = f"{self._base_url}{_SYSTEMONE_PATH}"
        payload = {
            "state": state,
            "model": self._model,
            "questions": questions,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = httpx.post(
                url, json=payload, headers=headers, timeout=self._timeout
            )
        except httpx.HTTPError as error:
            # httpx messages can embed the URL only - never the header key.
            logger.warning("TypeSafe JEV request failed: %s", type(error).__name__)
            raise JEVUnavailableError(
                "The TypeSafe API could not be reached."
            ) from error

        if response.status_code in (401, 403):
            raise JEVNotConfiguredError("TypeSafe rejected the API credentials.")
        if response.status_code >= 500:
            raise JEVUnavailableError(
                f"The TypeSafe API returned status {response.status_code}."
            )
        if response.status_code >= 400:
            raise JEVInvalidResponseError(
                f"The TypeSafe API rejected the request (status {response.status_code})."
            )

        try:
            body = response.json()
        except ValueError as error:
            raise JEVInvalidResponseError(
                "The TypeSafe API returned a non-JSON response."
            ) from error

        if not isinstance(body, dict) or "answers" not in body:
            raise JEVInvalidResponseError(
                "The TypeSafe API response is missing 'answers'."
            )
        return body
