"""Machine-readable academic provider capability profiles."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List


PROFILE_SCHEMA_VERSION = "knowledgeradar-academic-provider-profiles/v1"

_VALID_WAVES = {"A", "B", "C"}
_VALID_SPEEDS = {"fast", "medium", "slow"}
_VALID_STABILITY = {"high", "medium", "low"}
_VALID_QUOTA_TYPES = {"none", "daily", "monthly", "daily_monthly", "external"}


@dataclass(frozen=True)
class ProviderContentCapability:
    metadata: bool = True
    abstract: bool = False
    html_fulltext: bool = False
    pdf_fulltext: bool = False
    direct_read_preferred: bool = False

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProviderContentCapability":
        return cls(
            metadata=bool(data.get("metadata", True)),
            abstract=bool(data.get("abstract", False)),
            html_fulltext=bool(data.get("html_fulltext", False)),
            pdf_fulltext=bool(data.get("pdf_fulltext", False)),
            direct_read_preferred=bool(data.get("direct_read_preferred", False)),
        )

    def to_dict(self) -> Dict[str, bool]:
        return {
            "metadata": self.metadata,
            "abstract": self.abstract,
            "html_fulltext": self.html_fulltext,
            "pdf_fulltext": self.pdf_fulltext,
            "direct_read_preferred": self.direct_read_preferred,
        }


@dataclass(frozen=True)
class ProviderAccessPolicy:
    login_required: bool = False
    payment_required: bool = False
    auth_mode: str = "anonymous_public"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProviderAccessPolicy":
        return cls(
            login_required=bool(data.get("login_required", False)),
            payment_required=bool(data.get("payment_required", False)),
            auth_mode=str(data.get("auth_mode") or "anonymous_public"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "login_required": self.login_required,
            "payment_required": self.payment_required,
            "auth_mode": self.auth_mode,
        }


@dataclass(frozen=True)
class ProviderQuotaPolicy:
    type: str = "none"
    daily_env: str = ""
    monthly_env: str = ""
    default_daily: int | None = None
    default_monthly: int | None = None
    count_on: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProviderQuotaPolicy":
        return cls(
            type=str(data.get("type") or "none"),
            daily_env=str(data.get("daily_env") or ""),
            monthly_env=str(data.get("monthly_env") or ""),
            default_daily=_optional_int(data.get("default_daily")),
            default_monthly=_optional_int(data.get("default_monthly")),
            count_on=str(data.get("count_on") or ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"type": self.type}
        if self.daily_env:
            data["daily_env"] = self.daily_env
        if self.monthly_env:
            data["monthly_env"] = self.monthly_env
        if self.default_daily is not None:
            data["default_daily"] = self.default_daily
        if self.default_monthly is not None:
            data["default_monthly"] = self.default_monthly
        if self.count_on:
            data["count_on"] = self.count_on
        return data


@dataclass(frozen=True)
class ProviderRuntimeProfile:
    speed: str = "medium"
    stability: str = "medium"
    priority: Dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProviderRuntimeProfile":
        return cls(
            speed=str(data.get("speed") or "medium"),
            stability=str(data.get("stability") or "medium"),
            priority={str(key): int(value) for key, value in dict(data.get("priority") or {}).items()},
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"speed": self.speed, "stability": self.stability, "priority": dict(self.priority)}


@dataclass(frozen=True)
class AcademicProviderProfile:
    id: str
    display_name: str
    provider_class: str
    enabled: bool
    default_policy: str
    wave: List[str]
    languages: List[str]
    disciplines: List[str]
    content: ProviderContentCapability
    access: ProviderAccessPolicy
    quota: ProviderQuotaPolicy
    runtime: ProviderRuntimeProfile
    dedupe_ids: List[str]
    role: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AcademicProviderProfile":
        content = ProviderContentCapability.from_dict(dict(data.get("content") or {}))
        access = ProviderAccessPolicy.from_dict(dict(data.get("access") or {}))
        quota = ProviderQuotaPolicy.from_dict(dict(data.get("quota") or {}))
        runtime_data = dict(data.get("runtime") or {})
        if "speed" in data and "speed" not in runtime_data:
            runtime_data["speed"] = data["speed"]
        if "stability" in data and "stability" not in runtime_data:
            runtime_data["stability"] = data["stability"]
        if "priority" in data and "priority" not in runtime_data:
            runtime_data["priority"] = data["priority"]
        profile = cls(
            id=str(data.get("id") or "").strip(),
            display_name=str(data.get("display_name") or data.get("id") or "").strip(),
            provider_class=str(data.get("provider_class") or "").strip(),
            enabled=bool(data.get("enabled", True)),
            default_policy=str(data.get("default_policy") or "auto").strip(),
            wave=[str(item) for item in data.get("wave") or []],
            languages=[str(item) for item in data.get("languages") or []],
            disciplines=[str(item) for item in data.get("disciplines") or []],
            content=content,
            access=access,
            quota=quota,
            runtime=ProviderRuntimeProfile.from_dict(runtime_data),
            dedupe_ids=[str(item) for item in data.get("dedupe_ids") or []],
            role=str(data.get("role") or ""),
        )
        validate_profile(profile)
        return profile

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "provider_class": self.provider_class,
            "enabled": self.enabled,
            "default_policy": self.default_policy,
            "wave": list(self.wave),
            "languages": list(self.languages),
            "disciplines": list(self.disciplines),
            "content": self.content.to_dict(),
            "access": self.access.to_dict(),
            "quota": self.quota.to_dict(),
            "runtime": self.runtime.to_dict(),
            "dedupe_ids": list(self.dedupe_ids),
            "role": self.role,
        }

    def to_status_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "default_policy": self.default_policy,
            "wave": list(self.wave),
            "languages": list(self.languages),
            "disciplines": list(self.disciplines),
            "content": self.content.to_dict(),
            "access": self.access.to_dict(),
            "quota": self.quota.to_dict(),
            "runtime": self.runtime.to_dict(),
            "role": self.role,
        }


def builtin_profile_path() -> Path:
    return Path(__file__).with_name("profiles").joinpath("academic_provider_profiles.json")


def load_academic_provider_profiles(path: Path | None = None) -> Dict[str, AcademicProviderProfile]:
    data = json.loads((path or builtin_profile_path()).read_text(encoding="utf-8"))
    if data.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise ValueError(f"Unsupported academic provider profile schema: {data.get('schema_version')}")
    providers = data.get("providers")
    if not isinstance(providers, list):
        raise ValueError("Academic provider profile file must contain a providers list")
    profiles = [AcademicProviderProfile.from_dict(item) for item in providers if isinstance(item, dict)]
    duplicate_ids = _duplicates(profile.id for profile in profiles)
    if duplicate_ids:
        raise ValueError(f"Duplicate academic provider profile ids: {', '.join(sorted(duplicate_ids))}")
    return {profile.id: profile for profile in profiles}


def validate_profile(profile: AcademicProviderProfile) -> None:
    if not profile.id:
        raise ValueError("Academic provider profile id is required")
    if not profile.provider_class:
        raise ValueError(f"Academic provider profile {profile.id} must declare provider_class")
    if not profile.wave:
        raise ValueError(f"Academic provider profile {profile.id} must declare at least one wave")
    if any(wave not in _VALID_WAVES for wave in profile.wave):
        raise ValueError(f"Academic provider profile {profile.id} has invalid wave {profile.wave}")
    if not profile.languages:
        raise ValueError(f"Academic provider profile {profile.id} must declare languages")
    if not profile.disciplines:
        raise ValueError(f"Academic provider profile {profile.id} must declare disciplines")
    if profile.quota.type not in _VALID_QUOTA_TYPES:
        raise ValueError(f"Academic provider profile {profile.id} has invalid quota type {profile.quota.type}")
    if profile.runtime.speed not in _VALID_SPEEDS:
        raise ValueError(f"Academic provider profile {profile.id} has invalid speed {profile.runtime.speed}")
    if profile.runtime.stability not in _VALID_STABILITY:
        raise ValueError(f"Academic provider profile {profile.id} has invalid stability {profile.runtime.stability}")
    if not profile.dedupe_ids:
        raise ValueError(f"Academic provider profile {profile.id} must declare dedupe_ids")


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _duplicates(values: Iterable[str]) -> set[str]:
    seen = set()
    duplicates = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates
