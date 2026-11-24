"""Classify official release candidates using bounded, on-demand metadata."""
from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Callable

from .self_upgrade_compatibility import (
    LocalEnvironment, ReleaseRequirements, UpgradeDecision, evaluate_upgrade,
)
from .self_upgrade_manifest import ManifestError, read_release_requirements
from .self_upgrade_releases import OfficialRelease, Version, discover_releases

MAX_MANIFEST_REQUESTS = 64


class _Requirements(Mapping[Version, ReleaseRequirements]):
    """Resolve each release at most once for one catalogue snapshot.

    Mapping.get returns None on failure, so the existing compatibility policy
    remains the only authority on whether a plan is complete. No metadata
    request is made for a downgrade, the installed version or an unavailable
    intermediate release. Exhausting the budget fails closed.
    """

    def __init__(self, releases, reader, max_requests):
        self._available = {}
        self._duplicates = set()
        for release in releases:
            if release.version in self._available:
                self._duplicates.add(release.version)
            self._available[release.version] = release
        self._reader = reader
        self._remaining = max_requests
        self._cache: dict[Version, ReleaseRequirements | None] = {}

    def __getitem__(self, version: Version) -> ReleaseRequirements:
        if version not in self._cache:
            entry = self._available.get(version)
            result = None
            if (entry is not None and version not in self._duplicates
                    and entry.compatibility is not None and self._remaining > 0):
                self._remaining -= 1
                try:
                    result = self._reader(entry)
                except ManifestError:
                    pass
            self._cache[version] = result
        value = self._cache[version]
        if value is None:
            raise KeyError(version)
        return value

    def __iter__(self) -> Iterator[Version]:
        return iter(self._available)

    def __len__(self) -> int:
        return len(self._available)


class CompatibilityCatalogue:
    """A read-only snapshot of releases and their compatibility decisions.

    Use a fresh snapshot for a new operation or a changed local environment.
    This class does not download installation archives or approve a swap.
    """

    def __init__(
        self, releases: Sequence[OfficialRelease], *,
        installed: Version,
        environment: LocalEnvironment,
        reader: Callable[[OfficialRelease], ReleaseRequirements] = read_release_requirements,
        max_requests: int = MAX_MANIFEST_REQUESTS,
    ):
        if type(max_requests) is not int or not 1 <= max_requests <= MAX_MANIFEST_REQUESTS:
            raise ValueError('Invalid compatibility request limit')
        self.releases = tuple(sorted(releases, key=lambda release: release.version, reverse=True))
        self.installed = installed
        self.environment = environment
        self._requirements = _Requirements(self.releases, reader, max_requests)

    def decision_for(self, target: Version) -> UpgradeDecision:
        return evaluate_upgrade(self.installed, target, self.releases,
                                self._requirements, self.environment)

    def decisions(self) -> tuple[tuple[OfficialRelease, UpgradeDecision], ...]:
        """Keep all releases visible, including blocked and older versions."""
        return tuple((release, self.decision_for(release.version))
                     for release in self.releases)


def discover_compatibility(
    *, installed: Version, environment: LocalEnvironment,
) -> CompatibilityCatalogue:
    """Read the official catalogue; metadata is fetched only when evaluated."""
    return CompatibilityCatalogue(discover_releases(), installed=installed,
                                  environment=environment)
