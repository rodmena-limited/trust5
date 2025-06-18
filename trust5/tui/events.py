from textual.message import Message


class PipelineEvent(Message):
    """Event received from the pipeline EventBus."""

    def __init__(self, code: str, content: str, timestamp: str) -> None:
        self.code = code
        self.content = content
        self.timestamp = timestamp
        super().__init__()


class UpdateStatus(Message):
    """Update status bar information."""

    def __init__(self, target: int, content: str) -> None:
        self.target = target  # 0 or 1
        self.content = content
        super().__init__()


class StreamFlush(Message):
    """Timer-driven signal to flush accumulated stream tokens."""

    pass
