"""Evaluate normalized compatibility requirements without reading a manifest.

The release metadata adapter must supply verified requirements for each version.
Missing metadata never implies compatibility.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .self_upgrade_releases import OfficialRelease, Version


@dataclass(frozen=True)
class LocalEnvironment:
    python: tuple[int, int] | None
    linux_family: str | None
    layout: str | None


@dataclass(frozen=True)
class ReleaseRequirements:
    version: Version
    python_min: tuple[int, int]
    python_max_exclusive: tuple[int, int] | None
    linux_families: frozenset[str]
    layouts: frozenset[str]
    required_intermediates: tuple[Version, ...] = ()


@dataclass(frozen=True)
class UpgradeDecision:
    status: str
    reason: str
    steps: tuple[Version, ...] = ()
    blocked_at: Version | None = None


def _valid_python(value: object) -> bool:
    return (isinstance(value, tuple) and len(value) == 2
            and all(type(part) is int and part >= 0 for part in value))


def _valid_names(value: object) -> bool:
    return (isinstance(value, frozenset) and bool(value)
            and all(isinstance(name, str) and name and name == name.strip()
                    for name in value))


def _valid_requirements(req: ReleaseRequirements, version: Version) -> bool:
    return (
        isinstance(req, ReleaseRequirements)
        and req.version == version
        and _valid_python(req.python_min)
        and (req.python_max_exclusive is None
             or (_valid_python(req.python_max_exclusive)
                 and req.python_max_exclusive > req.python_min))
        and _valid_names(req.linux_families)
        and _valid_names(req.layouts)
        and isinstance(req.required_intermediates, tuple)
        and all(isinstance(step, Version) and step < version
                for step in req.required_intermediates)
        and tuple(sorted(set(req.required_intermediates))) == req.required_intermediates
    )


def evaluate_upgrade(
    installed: Version, target: Version,
    releases: Sequence[OfficialRelease],
    requirements: Mapping[Version, ReleaseRequirements],
    environment: LocalEnvironment,
) -> UpgradeDecision:
    """Build an ordered plan from explicitly declared mandatory checkpoints.

    Every required step must exist in the official catalogue and have verified
    requirements. The same local environment is assumed for all steps; no OS,
    Python or layout migration is inferred or performed. This is an internal
    policy, not a serialization contract or an installation authorization.
    """
    if target < installed:
        return UpgradeDecision('downgrade', 'downgrade_blocked')
    if target == installed:
        return UpgradeDecision('current', 'already_installed')
    if (not _valid_python(environment.python)
            or not environment.linux_family or not environment.layout):
        return UpgradeDecision('unverified', 'unknown_local_environment', blocked_at=target)
    available: dict[Version, OfficialRelease] = {}
    duplicates: set[Version] = set()
    for release in releases:
        if release.version in available:
            duplicates.add(release.version)
        available[release.version] = release

    current = installed
    steps: list[Version] = []

    def visit(version: Version, depth: int = 0) -> UpgradeDecision | None:
        nonlocal current
        if version <= current:
            return None
        if depth >= 64:
            return UpgradeDecision('unverified', 'too_many_intermediate_steps', blocked_at=version)
        entry = available.get(version)
        if entry is None or version in duplicates or entry.tag != f'v{version}':
            return UpgradeDecision('unverified', 'release_unavailable', blocked_at=version)
        req = requirements.get(version)
        if req is None or not _valid_requirements(req, version):
            return UpgradeDecision('unverified', 'missing_or_invalid_metadata', blocked_at=version)
        if (environment.python < req.python_min
                or (req.python_max_exclusive is not None
                    and environment.python >= req.python_max_exclusive)):
            return UpgradeDecision('incompatible', 'unsupported_python', blocked_at=version)
        if environment.linux_family not in req.linux_families:
            return UpgradeDecision('incompatible', 'unsupported_linux', blocked_at=version)
        if environment.layout not in req.layouts:
            return UpgradeDecision('incompatible', 'unsupported_layout', blocked_at=version)
        for intermediate in req.required_intermediates:
            failure = visit(intermediate, depth + 1)
            if failure is not None:
                return failure
        if len(steps) >= 64:
            return UpgradeDecision('unverified', 'too_many_intermediate_steps', blocked_at=version)
        steps.append(version)
        current = version
        return None

    failure = visit(target)
    if failure is not None:
        return failure  # Never offer a partially verified plan.
    return UpgradeDecision('compatible', 'verified_plan', tuple(steps))
