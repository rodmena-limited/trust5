import os
from trust5.core.compliance import (
    check_compliance,
    extract_identifiers,
)

class TestExtractIdentifiers:

    def test_extracts_pascal_case(self) -> None:
        ids = extract_identifiers("[UBIQ] The MonteCarloSimulator shall run simulations.")
        assert "MonteCarloSimulator" in ids

    def test_extracts_backtick_identifiers(self) -> None:
        ids = extract_identifiers("[EVENT] When `random_seed` is set, results shall be reproducible.")
        assert "random_seed" in ids

    def test_extracts_quoted_identifiers(self) -> None:
        ids = extract_identifiers('[UBIQ] The system shall support "batch_size" configuration.')
        assert "batch_size" in ids

    def test_extracts_snake_case_long(self) -> None:
        ids = extract_identifiers("[UBIQ] The system shall use confidence_interval calculations.")
        assert "confidence_interval" in ids

    def test_ignores_short_snake_case(self) -> None:
        ids = extract_identifiers("[UBIQ] The is_ok flag shall be set.")
        # "is_ok" is 5 chars, threshold is >5, so it should NOT be extracted
        assert "is_ok" not in ids

    def test_deduplicates(self) -> None:
        ids = extract_identifiers("[UBIQ] The `MonteCarloSimulator` uses MonteCarloSimulator.")
        pascal_count = sum(1 for i in ids if i.lower() == "montecarlosimulator")
        assert pascal_count == 1

    def test_empty_criterion(self) -> None:
        ids = extract_identifiers("[UBIQ] The system shall work.")
        assert ids == []

    def test_multiple_types(self) -> None:
        ids = extract_identifiers(
            "[UBIQ] The GeometricBrownianMotion with `random_seed` and confidence_interval."
        )
        id_lower = [i.lower() for i in ids]
        assert "geometricbrownianmotion" in id_lower
        assert "random_seed" in id_lower
        assert "confidence_interval" in id_lower

class TestCheckCompliance:
    pass
