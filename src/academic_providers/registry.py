"""Registry for academic providers and their capability profiles."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Dict, Mapping

from .profile import AcademicProviderProfile, load_academic_provider_profiles


@dataclass(frozen=True)
class ProviderRegistration:
    id: str
    provider: object
    profile: AcademicProviderProfile

    def status(self) -> Dict[str, object]:
        status_method = getattr(self.provider, "status", None)
        if not callable(status_method):
            return {}
        status = status_method()
        return dict(status or {})


def academic_provider_registry() -> Dict[str, ProviderRegistration]:
    profiles = load_academic_provider_profiles()
    return {provider_id: _registration_from_profile(profile) for provider_id, profile in profiles.items() if profile.enabled}


def academic_provider_profiles() -> Dict[str, AcademicProviderProfile]:
    return load_academic_provider_profiles()


def academic_provider_profile_status() -> Dict[str, Dict[str, object]]:
    return {provider_id: profile.to_status_dict() for provider_id, profile in academic_provider_profiles().items()}


def instantiate_academic_providers(
    profiles: Mapping[str, AcademicProviderProfile] | None = None,
) -> Dict[str, object]:
    if profiles is None:
        registrations = academic_provider_registry()
    else:
        registrations = {
            provider_id: _registration_from_profile(profile)
            for provider_id, profile in profiles.items()
            if profile.enabled
        }
    return {provider_id: registration.provider for provider_id, registration in registrations.items()}


def registered_provider_profiles(registry: Mapping[str, ProviderRegistration] | None = None) -> Dict[str, AcademicProviderProfile]:
    registrations = registry or academic_provider_registry()
    return {provider_id: registration.profile for provider_id, registration in registrations.items()}


def _registration_from_profile(profile: AcademicProviderProfile) -> ProviderRegistration:
    module_name, sep, class_name = profile.provider_class.partition(":")
    if not sep:
        raise ValueError(f"Invalid provider_class for {profile.id}: {profile.provider_class}")
    module = import_module(module_name)
    provider_cls = getattr(module, class_name)
    provider = provider_cls()
    return ProviderRegistration(id=profile.id, provider=provider, profile=profile)
