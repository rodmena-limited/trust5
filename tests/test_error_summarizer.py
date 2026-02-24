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
            "message": {
                "role": "assistant",
                "content": (
                    "FAILURE_TYPE: test\nROOT_CAUSE: Something broke\n"
                    "FILES_AFFECTED:\n- app.py:10 bad import\nSUGGESTED_FIX: Fix the import"
                ),
            },
            "done": True,
        }
        with patch("trust5.core.error_summarizer.LLM") as llm_cls:
            llm_cls.for_tier.return_value = mock_llm
            result = summarize_errors(raw, failure_type="test")
        llm_cls.for_tier.assert_called_once_with("fast", thinking_level=None)
        mock_llm.chat.assert_called_once()
        assert "ROOT_CAUSE" in result

    def test_llm_failure_returns_truncated_raw(self) -> None:
        raw = "X" * 500
        with patch("trust5.core.error_summarizer.LLM") as llm_cls:
            llm_cls.for_tier.side_effect = Exception("LLM down")
            result = summarize_errors(raw)
        assert result == raw[:3000]

    def test_llm_returns_short_response_uses_raw(self) -> None:
        raw = "Y" * 500
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {"message": {"role": "assistant", "content": "ok"}, "done": True}
        with patch("trust5.core.error_summarizer.LLM") as llm_cls:
            llm_cls.for_tier.return_value = mock_llm
            result = summarize_errors(raw)
        assert result == raw[:3000]

    def test_llm_content_is_list(self) -> None:
        raw = "Z" * 33_000  # Must exceed 32k threshold to trigger LLM path
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "FAILURE_TYPE: lint\nROOT_CAUSE: Missing semicolons everywhere"}
            ]},
            "done": True,
        }
        with patch("trust5.core.error_summarizer.LLM") as llm_cls:
            llm_cls.for_tier.return_value = mock_llm
            result = summarize_errors(raw, failure_type="lint")
        assert "ROOT_CAUSE" in result

    def test_truncates_to_max_summary(self) -> None:
        raw = "A" * 500
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {"message": {"role": "assistant", "content": "B" * 5000}, "done": True}
        with patch("trust5.core.error_summarizer.LLM") as llm_cls:
            llm_cls.for_tier.return_value = mock_llm
            result = summarize_errors(raw)
        assert len(result) <= 3000
