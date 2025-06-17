from unittest.mock import MagicMock, patch
from trust5.core.error_summarizer import summarize_errors

class TestSummarizeErrors:

    def test_short_input_returned_as_is(self) -> None:
        short = "Error: x"
        assert summarize_errors(short) == short
