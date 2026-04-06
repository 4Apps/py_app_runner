import datetime as dt
import uuid
from typing import Literal, TypedDict, cast

import jwt

from py_app_runner.registry import AppRegistry
from py_app_runner.utils import CustomJSONEncoder

JwtType = Literal["user", "device", "impersonation"]


class JwtPayload(TypedDict, total=False):
    sub: str
    typ: JwtType
    iat: int
    exp: int
    jti: str
    imp: str  # impersonator public_id (only for typ="impersonation")


class AuthService:
    def __init__(self, logger):
        self.logger = logger

    def _now(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC)

    def create_access_jwt(
        self,
        *,
        subject_public_id: str,
        token_type: JwtType,
        ttl_seconds: int = 15 * 60,
    ) -> str:
        """
        Create short-lived access JWT.
        'sub' MUST be the public_id UUID string of the user/device.
        """
        now = self._now()
        payload: dict = {
            "sub": str(subject_public_id),
            "typ": token_type,
            "iat": int(now.timestamp()),
            "exp": int((now + dt.timedelta(seconds=ttl_seconds)).timestamp()),
            "jti": str(uuid.uuid4()),
        }
        secret = AppRegistry.config()["jwt"]["secret"]
        return jwt.encode(payload, secret, algorithm="HS256", json_encoder=CustomJSONEncoder)  # type: ignore

    def create_impersonation_jwt(
        self,
        *,
        target_public_id: str,
        impersonator_public_id: str,
        ttl_seconds: int = 30 * 60,
    ) -> str:
        """
        Create an impersonation JWT.
        sub = target user, imp = superadmin who initiated impersonation.
        """
        now = self._now()
        payload: dict = {
            "sub": str(target_public_id),
            "typ": "impersonation",
            "imp": str(impersonator_public_id),
            "iat": int(now.timestamp()),
            "exp": int((now + dt.timedelta(seconds=ttl_seconds)).timestamp()),
            "jti": str(uuid.uuid4()),
        }
        secret = AppRegistry.config()["jwt"]["secret"]
        return jwt.encode(payload, secret, algorithm="HS256", json_encoder=CustomJSONEncoder)  # type: ignore

    def verify_access_jwt(self, token: str, *, expected_type: JwtType) -> JwtPayload | None:
        """
        Verify JWT signature + exp, enforce expected typ.
        Returns payload dict if valid else None.
        """
        try:
            payload = cast(
                JwtPayload,
                jwt.decode(
                    token,
                    AppRegistry.config()["jwt"]["secret"],
                    algorithms=["HS256"],
                    options={
                        "require": ["exp", "iat", "sub", "typ"],
                    },
                    leeway=10,  # seconds clock skew tolerance
                ),
            )
        except jwt.ExpiredSignatureError:
            self.logger.debug("JWT expired")
            return None
        except jwt.InvalidTokenError:
            self.logger.debug("JWT invalid")
            return None

        if payload.get("typ") != expected_type:
            self.logger.debug("JWT type mismatch: expected=%s got=%s", expected_type, payload.get("typ"))
            return None

        # Validate UUID format for sub/jti (defensive)
        try:
            uuid.UUID(payload["sub"])
            uuid.UUID(payload["jti"])
        except Exception:
            self.logger.debug("JWT payload UUID fields invalid")
            return None

        # For impersonation tokens, also validate the impersonator UUID
        if payload.get("typ") == "impersonation":
            imp = payload.get("imp")
            if not imp:
                self.logger.debug("Impersonation JWT missing imp claim")
                return None
            try:
                uuid.UUID(imp)
            except Exception:
                self.logger.debug("Impersonation JWT imp field invalid")
                return None

        return payload
