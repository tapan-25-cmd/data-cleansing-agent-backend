from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.domain.unit_rule import UnitRule, UnitRuleset


class RulesetError(ValueError):
    pass


class RuleRegistry:
    def __init__(self, ruleset: UnitRuleset, checksum: str):
        self.ruleset = ruleset
        self.checksum = checksum
        self._by_source: dict[str, UnitRule] = {}
        rule_ids: set[str] = set()
        for rule in ruleset.rules:
            if rule.rule_id in rule_ids:
                raise RulesetError(f"duplicate rule id: {rule.rule_id}")
            rule_ids.add(rule.rule_id)
            if not rule.enabled:
                continue
            for source in rule.source_uoms:
                if source in self._by_source:
                    raise RulesetError(f"duplicate enabled source UOM: {source}")
                self._by_source[source] = rule

    @classmethod
    def load(cls, path: Path) -> "RuleRegistry":
        raw = path.read_bytes()
        try:
            payload = yaml.safe_load(raw)
            ruleset = UnitRuleset.model_validate(payload)
        except (yaml.YAMLError, ValidationError, TypeError) as exc:
            raise RulesetError(f"invalid ruleset {path}: {exc}") from exc
        return cls(ruleset, sha256(raw).hexdigest())

    @property
    def version(self) -> str:
        return self.ruleset.version

    @property
    def sources(self) -> frozenset[str]:
        return frozenset(self._by_source)

    def get(self, source_uom: str | None) -> UnitRule | None:
        if not source_uom:
            return None
        return self._by_source.get(source_uom.strip().upper())


DEFAULT_RULESET_PATH = Path(__file__).with_name("unit_mappings.v1.yaml")


def load_default_registry() -> RuleRegistry:
    return RuleRegistry.load(DEFAULT_RULESET_PATH)
