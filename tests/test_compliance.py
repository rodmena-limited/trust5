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
    def test_all_criteria_met(self, tmp_path: os.PathLike[str]) -> None:
        src = tmp_path / "simulator.py"
        src.write_text(
            "class MonteCarloSimulator:\n"
            "    def run(self, random_seed=None):\n"
            "        pass\n"
        )
        criteria = [
            "[UBIQ] The MonteCarloSimulator shall run simulations.",
            "[EVENT] When `random_seed` is set, results shall be reproducible.",
        ]
        report = check_compliance(criteria, str(tmp_path), extensions=(".py",))
        assert report.criteria_total == 2
        assert report.criteria_met == 2
        assert report.criteria_not_met == 0
        assert report.compliance_ratio == 1.0
        assert report.unmet_criteria == ()

    def test_no_criteria_met(self, tmp_path: os.PathLike[str]) -> None:
        src = tmp_path / "pi.py"
        src.write_text("def estimate_pi(n):\n    return 3.14\n")
        criteria = [
            "[UBIQ] The MonteCarloSimulator shall run simulations.",
            "[EVENT] When `random_seed` is set, results shall be reproducible.",
        ]
        report = check_compliance(criteria, str(tmp_path), extensions=(".py",))
        assert report.criteria_met == 0
        assert report.criteria_not_met == 2
        assert report.compliance_ratio == 0.0
        assert len(report.unmet_criteria) == 2

    def test_partial_match(self, tmp_path: os.PathLike[str]) -> None:
        src = tmp_path / "sim.py"
        src.write_text(
            "class MonteCarloSimulator:\n"
            "    pass\n"
        )
        criteria = [
            "[UBIQ] The MonteCarloSimulator shall run simulations.",
            "[UBIQ] The GeometricBrownianMotion model shall compute paths.",
        ]
        report = check_compliance(criteria, str(tmp_path), extensions=(".py",))
        assert report.criteria_met == 1
        assert report.criteria_not_met == 1
        assert report.compliance_ratio == 0.5

    def test_empty_criteria_returns_neutral(self, tmp_path: os.PathLike[str]) -> None:
        report = check_compliance([], str(tmp_path))
        assert report.criteria_total == 0
        assert report.compliance_ratio == 1.0

    def test_skips_test_files(self, tmp_path: os.PathLike[str]) -> None:
        # Source file has nothing, but test file has the identifier
        src = tmp_path / "app.py"
        src.write_text("def hello(): pass\n")
        test_dir = tmp_path / "tests"
        test_dir.mkdir()
        test_file = test_dir / "test_sim.py"
        test_file.write_text("class MonteCarloSimulator: pass\n")
        criteria = ["[UBIQ] The MonteCarloSimulator shall work."]
        report = check_compliance(criteria, str(tmp_path), extensions=(".py",))
        assert report.criteria_met == 0
        assert report.criteria_not_met == 1

    def test_criterion_with_no_identifiers_counts_as_met(self, tmp_path: os.PathLike[str]) -> None:
        src = tmp_path / "app.py"
        src.write_text("pass\n")
        criteria = ["[UBIQ] The system shall work correctly."]
        report = check_compliance(criteria, str(tmp_path), extensions=(".py",))
        assert report.criteria_met == 1
        assert report.compliance_ratio == 1.0

    def test_per_criterion_results(self, tmp_path: os.PathLike[str]) -> None:
        src = tmp_path / "engine.py"
        src.write_text("class MonteCarloSimulator:\n    pass\n")
        criteria = [
            "[UBIQ] The MonteCarloSimulator shall be available.",
            "[UBIQ] The BatchProcessor shall handle batches.",
        ]
        report = check_compliance(criteria, str(tmp_path), extensions=(".py",))
        assert len(report.results) == 2
        assert report.results[0].status == "met"
        assert report.results[1].status == "not_met"
