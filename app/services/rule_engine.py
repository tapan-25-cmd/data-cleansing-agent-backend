from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from app.domain.product import RuleProposal
from app.rules.registry import RuleRegistry


class RuleEngine:
    def __init__(self, registry: RuleRegistry, default_rounding_decimals: int | None = None):
        self.registry = registry
        self.default_rounding_decimals = default_rounding_decimals

    def propose(self, source_value: object, source_uom: str | None) -> RuleProposal | None:
        rule = self.registry.get(source_uom)
        if rule is None:
            return None
        try:
            value = Decimal(str(source_value).strip())
        except (InvalidOperation, ValueError, AttributeError):
            return None
        raw_target = value * rule.factor
        places = rule.rounding_decimals
        if places is None:
            places = self.default_rounding_decimals
        final_target = raw_target
        if places is not None:
            quantum = Decimal(1).scaleb(-places)
            final_target = raw_target.quantize(quantum, rounding=ROUND_HALF_UP)
        return RuleProposal(
            standard_size=final_target,
            standard_uom=rule.target_uom,
            rule_id=rule.rule_id,
            source_uom=(source_uom or "").strip().upper(),
            target_uom=rule.target_uom,
            factor=rule.factor,
            source_value=value,
            raw_target=raw_target,
            final_target=final_target,
            rounding_decimals=places,
        )
