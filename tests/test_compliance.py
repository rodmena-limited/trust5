import os
from trust5.core.compliance import (
    check_compliance,
    extract_identifiers,
)

class TestExtractIdentifiers:

    def test_extracts_pascal_case(self) -> None:
        ids = extract_identifiers("[UBIQ] The MonteCarloSimulator shall run simulations.")
        assert "MonteCarloSimulator" in ids
