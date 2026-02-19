"""LLM error types."""


class LLMError(Exception):
    """LLM call failure with error classification for smart retry logic.

    error_class values:
      "connection"  -- network unreachable, DNS failure, TCP connect timeout
      "server"      -- 5xx, read timeout (server alive but struggling)
      "rate_limit"  -- 429 (use retry_after from server header)
      "auth"        -- 401/403, expired or invalid credentials
      "permanent"   -- 4xx (non-auth), bad request (no retry)
    """

    def __init__(
        self,
        message: str,
        retryable: bool = False,
        retry_after: float = 0,
        error_class: str = "permanent",
    ):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
        self.error_class = error_class

    @property
    def is_network_error(self) -> bool:
        """True when the failure is infrastructure-related (not a logic error)."""
        return self.error_class in ("connection", "server", "rate_limit")

    @property
    def is_auth_error(self) -> bool:
        """True when the failure is an authentication/authorization error."""
        return self.error_class == "auth"
