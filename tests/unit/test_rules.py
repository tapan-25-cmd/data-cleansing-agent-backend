from decimal import Decimal
from pathlib import Path

import pytest

from app.rules.registry import RuleRegistry, RulesetError, load_default_registry
from app.services.rule_engine import RuleEngine


def test_default_ruleset_is_versioned_and_excludes_unresolved_units():
    registry = load_default_registry()
    assert registry.version == "poc-v1"
    assert len(registry.checksum) == 64
    assert registry.get("kg").rule_id == "KG_TO_GM"
    assert registry.get("Pack").rule_id == "PACK_TO_EA"
    for unresolved in ("FZ", "ST", "SET", "PR", "AV KG"):
        assert registry.get(unresolved) is None


def test_decimal_conversion_and_rounding():
    engine = RuleEngine(load_default_registry())
    result = engine.propose("1.25", "KG")
    assert result is not None
    assert result.raw_target == Decimal("1250.00")
    assert result.standard_size == Decimal("1250.00")
    assert result.standard_uom == "GM"


def test_unknown_rule_never_guesses():
    assert RuleEngine(load_default_registry()).propose("16", "FZ") is None


def test_duplicate_enabled_source_is_rejected(tmp_path: Path):
    path = tmp_path / "rules.yaml"
    path.write_text("""
version: test
rules:
  - {rule_id: ONE, source_uoms: [KG], target_uom: GM, factor: '1'}
  - {rule_id: TWO, source_uoms: [KG], target_uom: GM, factor: '2'}
""")
    with pytest.raises(RulesetError, match="duplicate enabled source UOM"):
        RuleRegistry.load(path)


def test_invalid_target_is_rejected(tmp_path: Path):
    path = tmp_path / "rules.yaml"
    path.write_text("""
version: test
rules:
  - {rule_id: BAD, source_uoms: [KG], target_uom: KILOGRAM, factor: '1'}
""")
    with pytest.raises(RulesetError, match="target_uom"):
        RuleRegistry.load(path)
