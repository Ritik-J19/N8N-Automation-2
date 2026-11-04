from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import secrets
import threading
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status


COOKIE_NAME = "channel_factory_session"
SESSION_HOURS = 8


@dataclass(frozen=True)
class Session:
    session_id: str
    expires_at: int
    csrf_token: str


def _encode(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return urlsafe_b64decode(value + "=" * (-len(value) % 4))


class SessionSigner:
    def __init__(self, access_token: str) -> None:
        if len(access_token) < 24 or len(set(access_token)) < 8:
            raise ValueError(
                "CHANNEL_FACTORY_DASHBOARD_TOKEN must be at least 24 characters with varied characters"
            )
        self._secret = hashlib.sha256(
            f"channel-factory-session:{access_token}".encode("utf-8")
        ).digest()
        self._access_token_digest = hashlib.sha256(access_token.encode("utf-8")).digest()
        self._active_sessions: dict[str, int] = {}
        self._lock = threading.Lock()

    def verify_access_token(self, supplied: str) -> bool:
        supplied_digest = hashlib.sha256(supplied.encode("utf-8")).digest()
        return hmac.compare_digest(self._access_token_digest, supplied_digest)

    def create(self) -> tuple[str, Session]:
        session = Session(
            session_id=secrets.token_urlsafe(24),
            expires_at=int((datetime.now(UTC) + timedelta(hours=SESSION_HOURS)).timestamp()),
            csrf_token=secrets.token_urlsafe(24),
        )
        payload = _encode(
            json.dumps(
                {"sid": session.session_id, "exp": session.expires_at, "csrf": session.csrf_token},
                separators=(",", ":"),
            ).encode("utf-8")
        )
        signature = _encode(hmac.new(self._secret, payload.encode("ascii"), hashlib.sha256).digest())
        with self._lock:
            now = int(datetime.now(UTC).timestamp())
            self._active_sessions = {
                key: expiry for key, expiry in self._active_sessions.items() if expiry > now
            }
            self._active_sessions[session.session_id] = session.expires_at
        return f"{payload}.{signature}", session

    def verify(self, value: str | None) -> Session | None:
        if not value or "." not in value:
            return None
        payload, supplied_signature = value.split(".", 1)
        expected = _encode(hmac.new(self._secret, payload.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(expected, supplied_signature):
            return None
        try:
            data = json.loads(_decode(payload))
            session = Session(
                session_id=str(data["sid"]),
                expires_at=int(data["exp"]),
                csrf_token=str(data["csrf"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if session.expires_at <= int(datetime.now(UTC).timestamp()):
            return None
        with self._lock:
            if self._active_sessions.get(session.session_id) != session.expires_at:
                return None
        return session

    def revoke(self, session: Session) -> None:
        with self._lock:
            self._active_sessions.pop(session.session_id, None)


class LoginThrottle:
    def __init__(self, maximum: int = 5, window_seconds: int = 300) -> None:
        self.maximum = maximum
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def retry_after(self, client_key: str) -> int:
        now = time.monotonic()
        with self._lock:
            failures = self._failures[client_key]
            while failures and now - failures[0] >= self.window_seconds:
                failures.popleft()
            if len(failures) < self.maximum:
                return 0
            return max(1, int(self.window_seconds - (now - failures[0])))

    def record_failure(self, client_key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            failures = self._failures[client_key]
            while failures and now - failures[0] >= self.window_seconds:
                failures.popleft()
            failures.append(now)
            return len(failures) == self.maximum

    def clear(self, client_key: str) -> None:
        with self._lock:
            self._failures.pop(client_key, None)


def authenticated_session(request: Request) -> Session:
    signer: SessionSigner = request.app.state.session_signer
    session = signer.verify(request.cookies.get(COOKIE_NAME))
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return session


def csrf_protected(request: Request) -> Session:
    session = authenticated_session(request)
    origin = request.headers.get("Origin")
    if origin:
        parsed = urlsplit(origin)
        expected_host = request.headers.get("host", "")
        if parsed.scheme not in {"http", "https"} or parsed.netloc != expected_host:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid request origin")
    supplied = request.headers.get("X-CSRF-Token", "")
    if not hmac.compare_digest(session.csrf_token, supplied):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    return session
