from unittest.mock import MagicMock, patch
from trust5.core.error_summarizer import summarize_errors

class TestSummarizeErrors:

    def test_short_input_returned_as_is(self) -> None:
        short = "Error: x"
        assert summarize_errors(short) == short

    def test_empty_input_returned_as_is(self) -> None:
        assert summarize_errors("") == ""

    def test_long_input_calls_llm(self) -> None:
        raw = "E" * 33_000  # Must exceed 32k threshold to trigger LLM path
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {
            "content": (
                "FAILURE_TYPE: test\nROOT_CAUSE: Something broke\n"
                "FILES_AFFECTED:\n- app.py:10 bad import\nSUGGESTED_FIX: Fix the import"
            )
        }
        with patch("trust5.core.error_summarizer.LLM") as llm_cls:
            llm_cls.for_tier.return_value = mock_llm
            result = summarize_errors(raw, failure_type="test")
        llm_cls.for_tier.assert_called_once_with("fast", thinking_level=None)
        mock_llm.chat.assert_called_once()
        assert "ROOT_CAUSE" in result
