"""In-process API client for the terminal application.

The terminal app talks to the *same* FastAPI application the deployed service
exposes, but without opening a network socket: Starlette's ``TestClient`` runs
the ASGI app (and its lifespan, so the bell scheduler and outbox dispatcher run)
in a background thread. Every request therefore passes through the exact same
authentication, authorization and validation code that protects the HTTP API -
the terminal cannot bypass a rule the server would enforce.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi.testclient import TestClient


class ApiError(Exception):
    """A non-2xx response from the SACS service."""

    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{status_code}: {detail}")


class ApiClient:
    def __init__(self, app=None, *, base_url: str = "http://sacs.local"):
        if app is None:
            from .app import create_app

            app = create_app()
        self._client = TestClient(app)
        self._client.__enter__()  # start lifespan (bell loop, dispatcher)
        self.token: Optional[str] = None

    # -- lifecycle -------------------------------------------------------- #
    def close(self) -> None:
        try:
            self._client.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "ApiClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- core ------------------------------------------------------------- #
    def _headers(self, extra: Optional[dict] = None) -> dict:
        headers = dict(extra or {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def request(self, method: str, path: str, *, json=None, params=None, headers=None, expect_json=True):
        response = self._client.request(
            method, path, json=json, params=params, headers=self._headers(headers)
        )
        if response.status_code >= 400:
            detail: Any
            try:
                detail = response.json().get("detail")
            except Exception:  # noqa: BLE001
                detail = response.text
            if isinstance(detail, list):  # pydantic validation errors
                detail = "; ".join(str(item.get("msg", item)) for item in detail)
            raise ApiError(response.status_code, detail or response.reason_phrase)
        if not expect_json or response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except Exception:  # noqa: BLE001
            return None

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw):
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw):
        return self.request("PUT", path, **kw)

    def patch(self, path: str, **kw):
        return self.request("PATCH", path, **kw)

    def delete(self, path: str, **kw):
        return self.request("DELETE", path, **kw)

    # -- auth ------------------------------------------------------------- #
    def login(self, email: str, password: str, school_id: Optional[str] = None) -> dict:
        payload: dict = {"email": email, "password": password}
        if school_id:
            payload["school_id"] = school_id
        data = self.request("POST", "/api/auth/login", json=payload)
        self.token = data["token"]
        return data

    def logout(self) -> None:
        try:
            if self.token:
                self.request("POST", "/api/auth/logout")
        finally:
            self.token = None

    def me(self) -> dict:
        return self.request("GET", "/api/auth/me")
