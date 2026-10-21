from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_authority(value: str) -> tuple[str, int | None]:
    """Parse an HTTP Host authority, preserving bracketed IPv6 port semantics."""
    if not value or any(character.isspace() for character in value):
        raise ValueError("host must be a nonempty authority without whitespace")
    if any(character in value for character in "/\\?#@") or value.endswith(":"):
        raise ValueError("host must contain only a hostname and optional port")
    if value.startswith("["):
        closing = value.find("]")
        suffix = value[closing + 1 :] if closing >= 0 else value
        if suffix and (
            not suffix.startswith(":") or not suffix[1:].isascii() or not suffix[1:].isdigit()
        ):
            raise ValueError("IPv6 host must use brackets and an optional numeric port")
    elif "[" in value or "]" in value or value.count(":") > 1:
        raise ValueError("IPv6 host must use brackets")
    parsed = urlsplit("//" + value)
    hostname, port = parsed.hostname, parsed.port
    if not hostname or (port is not None and port < 1):
        raise ValueError("host requires a valid hostname and optional port in 1..65535")
    return hostname.lower(), port


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TOF_", extra="ignore")

    api_keys: list[SecretStr] = Field(default_factory=list, repr=False)
    default_backend: Literal["laya", "jev", "mock"] = "laya"
    laya_base_url: str = "http://laya-inference:8080"
    laya_api_key: SecretStr | None = Field(default=None, repr=False)
    laya_model: str | None = None
    laya_model_revision: str | None = None
    jev_base_url: str = "https://api.typesafe.ai"
    jev_api_key: SecretStr | None = Field(default=None, repr=False)
    jev_model: str = "jev-1.13.0"
    enable_hosted: bool = False
    allow_mock: bool = False
    request_timeout_seconds: float = Field(default=30, gt=0, le=300)
    max_concurrent_requests: int = Field(default=4, ge=1, le=256)
    max_inflight_requests: int = Field(default=64, ge=1, le=4096)
    body_timeout_seconds: float = Field(default=10, gt=0, le=60, allow_inf_nan=False)
    max_body_bytes: int = Field(default=262144, ge=1024, le=2097152)
    max_response_bytes: int = Field(default=1048576, ge=1024, le=8388608)
    allowed_hosts: list[str] = [
        "localhost",
        "127.0.0.1",
        "[::1]",
        "testserver",
        "tof-prediction-adapter",
        "tof-prediction-adapter:*",
    ]
    allowed_origins: list[str] = []

    @field_validator("laya_base_url", "jev_base_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        if any(character.isspace() for character in value) or "\\" in value:
            raise ValueError("backend URLs must not contain whitespace or backslashes")
        url = urlsplit(value)
        if url.scheme not in ("http", "https") or not url.hostname:
            raise ValueError("backend URLs must be absolute http(s) URLs")
        if url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
            raise ValueError("backend URLs must contain only scheme, host and port")
        parse_authority(url.netloc)
        return f"{url.scheme}://{url.netloc}"

    @field_validator("allowed_hosts")
    @classmethod
    def valid_hosts(cls, values: list[str]) -> list[str]:
        for value in values:
            host = value[:-2] if value.endswith(":*") else value
            if "*" in host:
                raise ValueError("configure explicit allowed hosts")
            parse_authority(host)
        return values

    @model_validator(mode="after")
    def configured(self):
        if not self.api_keys or any(len(k.get_secret_value()) < 24 for k in self.api_keys):
            raise ValueError("TOF_API_KEYS must contain at least one key of 24 or more characters")
        if self.default_backend == "mock" and not self.allow_mock:
            raise ValueError("mock backend requires TOF_ALLOW_MOCK=true")
        if self.enable_hosted:
            if not self.jev_api_key or not self.jev_api_key.get_secret_value():
                raise ValueError("hosted backend requires TOF_JEV_API_KEY")
            if not self.jev_base_url.startswith("https://"):
                raise ValueError("hosted backend requires HTTPS")
        if self.default_backend == "jev" and not self.enable_hosted:
            raise ValueError("default Jev backend requires TOF_ENABLE_HOSTED=true")
        if not self.allowed_hosts or "*" in self.allowed_hosts:
            raise ValueError("configure explicit allowed hosts")
        return self
