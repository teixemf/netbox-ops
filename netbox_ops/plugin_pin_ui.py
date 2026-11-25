"""Shared pin review and explicit approval for CLI and TUI."""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from typing import Sequence

from .i18n import Translator
from .plugin_inventory import (
    PinProposal, PluginInventory, PluginInventoryError, apply_approved_pins,
    inventory_plugins, normalize_name,
)
from .upgrade_discovery import inspect_git_installation


@dataclass(frozen=True)
class PinReview:
    inventory: PluginInventory
    selected: tuple[str, ...]
    proposals: tuple[PinProposal, ...]
    fingerprint: str


def build_pin_review(inventory: PluginInventory, selected: Sequence[str]) -> PinReview:
    available = {normalize_name(p.package): p for p in inventory.proposals}
    names = tuple(sorted(normalize_name(name) for name in selected))
    if len(set(names)) != len(names) or set(names) - set(available) or len(available) != len(inventory.proposals):
        raise PluginInventoryError('Select distinct unambiguous proposed pins')
    proposals = tuple(available[name] for name in names)
    payload = (
        str(inventory.root), inventory.requirements_hash, inventory.enabled,
        [(p.name, p.version, p.modules) for p in inventory.packages],
        [(name, p.version, p.line, p.before, p.after) for name, p in zip(names, proposals)],
    )
    fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=True).encode()).hexdigest()
    return PinReview(inventory, names, proposals, fingerprint)


def pin_review_lines(review: PinReview, t: Translator) -> list[str]:
    lines = []
    if not review.proposals:
        lines.append(t('upgrade_no_pins'))
    for item in review.proposals:
        where = (t('upgrade_pin_append') if item.line is None else
                 t('upgrade_pin_line', number=item.line))
        lines.append(f'{item.package} ({where})')
        # JSON quoting preserves spaces/newlines and escapes all terminal controls.
        if item.line is not None:
            lines.append('- ' + json.dumps(item.before, ensure_ascii=True))
        lines.append('+ ' + json.dumps(item.after, ensure_ascii=True))
    lines.extend(t('upgrade_warning', message=''.join(c if c.isprintable() else ' ' for c in warning))
                 for warning in review.inventory.warnings)
    if review.proposals:
        lines.append(t('upgrade_pin_fingerprint', fingerprint=review.fingerprint))
    return lines


def apply_pin_review(review: PinReview, fingerprint: str) -> None:
    if (not review.selected or fingerprint != review.fingerprint
            or review != build_pin_review(review.inventory, review.selected)):
        raise PluginInventoryError('Approval must match the exact pin review fingerprint')
    apply_approved_pins(review.inventory, review.selected, approved=True)


def run_pin_cli(args, t: Translator) -> int:
    try:
        inventory = inventory_plugins(inspect_git_installation())
        explicit = bool(args.all or args.plugin)
        selected = (tuple(p.package for p in inventory.proposals) if args.all or not args.plugin
                    else tuple(args.plugin))
        review = build_pin_review(inventory, selected)
        for line in pin_review_lines(review, t):
            print(line)
        if not args.apply:
            if args.approve_pins:
                raise PluginInventoryError('Use --apply with an explicitly approved pin review')
            return 0
        if not explicit:
            raise PluginInventoryError('Select pins using --plugin or --all before applying')
        apply_pin_review(review, args.approve_pins)
        print(t('upgrade_pins_applied'))
        return 0
    except PluginInventoryError as exc:
        print(t('upgrade_error', message=str(exc)), file=sys.stderr)
    except (KeyboardInterrupt, EOFError):
        print(t('upgrade_cancelled'), file=sys.stderr)
    except Exception:
        print(t('upgrade_discovery_error'), file=sys.stderr)
    return 1
