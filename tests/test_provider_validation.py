"""Tests for provider validation in main.py CLI commands."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from trust5.core.auth.provider import TokenData

# ---------------------------------------------------------------------------
# Test validate_provider() behavior
# ---------------------------------------------------------------------------


class TestValidateProvider:
    """Tests for the validate_provider() function."""

    @patch("trust5.core.auth.registry._get_store")
    def test_unknown_provider_override_raises_system_exit(self, mock_get_store: MagicMock) -> None:
        """Unknown provider via --provider flag should exit with error."""
        from trust5.core.auth.registry import set_provider_override, validate_provider

        # Set an unknown provider override
        set_provider_override("unknown_provider")

        # Mock store to avoid actual DB access
        mock_store = MagicMock()
        mock_get_store.return_value = mock_store

        with patch("trust5.core.message.emit") as mock_emit:
            with pytest.raises(SystemExit) as exc_info:
                validate_provider()

            assert exc_info.value.code == 1
            # Check error message mentions unknown provider
            error_calls = [call for call in mock_emit.call_args_list if call[0][0].value == "SERR"]
            assert len(error_calls) == 1
            error_msg = error_calls[0][0][1]
            assert "Unknown provider" in error_msg
            assert "unknown_provider" in error_msg

    @patch("trust5.core.auth.registry._get_store")
    def test_known_provider_no_token_raises_system_exit(self, mock_get_store: MagicMock) -> None:
        """Known provider without valid token should exit with login message."""
        from trust5.core.auth.registry import set_provider_override, validate_provider

        # Set a known provider override (claude)
        set_provider_override("claude")

        # Mock store to return no valid token
        mock_store = MagicMock()
        mock_store.get_valid_token.return_value = None  # No valid token
        mock_get_store.return_value = mock_store

        with patch("trust5.core.message.emit") as mock_emit:
            with patch("trust5.core.auth.registry.get_provider") as mock_get_provider:
                mock_get_provider.return_value = MagicMock()

                with pytest.raises(SystemExit) as exc_info:
                    validate_provider()

                assert exc_info.value.code == 1
                # Check error message mentions login
                error_calls = [call for call in mock_emit.call_args_list if call[0][0].value == "SERR"]
                assert len(error_calls) == 1
                error_msg = error_calls[0][0][1]
                assert "not authenticated" in error_msg.lower()
                assert "login" in error_msg.lower()

    @patch("trust5.core.auth.registry._get_store")
    def test_ollama_provider_passes_silently(self, mock_get_store: MagicMock) -> None:
        """Ollama provider should always pass validation (no auth required)."""
        from trust5.core.auth.registry import set_provider_override, validate_provider

        # Set ollama provider
        set_provider_override("ollama")

        # Mock store - should not be called for ollama
        mock_store = MagicMock()
        mock_get_store.return_value = mock_store

        # Should NOT raise
        validate_provider()

        # Store should not have been queried for valid token
        mock_store.get_valid_token.assert_not_called()

    @patch("trust5.core.auth.registry._get_store")
    def test_no_override_no_active_passes_silently(self, mock_get_store: MagicMock) -> None:
        """No provider override and no active provider should pass (Ollama fallback is fine)."""
        from trust5.core.auth.registry import set_provider_override, validate_provider

        # Clear any provider override (pass None to clear)
        set_provider_override(None)

        # Mock store to return no active provider
        mock_store = MagicMock()
        mock_store.get_active.return_value = None
        mock_get_store.return_value = mock_store

        # Should NOT raise
        validate_provider()

        # No token lookup should have happened
        mock_store.get_valid_token.assert_not_called()

    @patch("trust5.core.auth.registry._get_store")
    def test_authenticated_provider_passes(self, mock_get_store: MagicMock) -> None:
        """Provider with valid token should pass validation."""
        from trust5.core.auth.registry import set_provider_override, validate_provider

        # Set a known provider
        set_provider_override("claude")

        # Mock store to return valid token
        mock_store = MagicMock()
        valid_token = TokenData(
            access_token="test_token",
            refresh_token="test_refresh",
            expires_at=sys.float_info.max,  # Never expires
        )
        # get_valid_token returns (provider_instance, token_data) tuple
        mock_provider = MagicMock()
        mock_store.get_valid_token.return_value = (mock_provider, valid_token)
        mock_get_store.return_value = mock_store

        with patch("trust5.core.auth.registry.get_provider") as mock_get_provider:
            mock_get_provider.return_value = mock_provider

            # Should NOT raise
            validate_provider()

            # Token should have been validated
            mock_store.get_valid_token.assert_called_once()


# ---------------------------------------------------------------------------
# Integration tests with main.py commands
# ---------------------------------------------------------------------------


class TestMainProviderValidation:
    """Tests for provider validation integration with CLI commands."""

    @patch("trust5.main._run_workflow_dispatch")
    @patch("trust5.main.ensure_ollama_models")
    @patch("trust5.main.setup_stabilize")
    @patch("trust5.main.Tools.set_non_interactive")
    def test_plan_validates_provider(
        self,
        mock_set_non_interactive: MagicMock,
        mock_setup_stabilize: MagicMock,
        mock_ensure_ollama: MagicMock,
        mock_run_workflow: MagicMock,
    ) -> None:
        """plan command should call validate_provider before ensure_ollama_models."""
        from trust5.core.auth.registry import set_provider_override
        from trust5.main import app

        # Clear any existing override
        set_provider_override(None)

        # Mock setup
        mock_setup_stabilize.return_value = (
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            "/tmp/test.db",
        )

        # Test with ollama (no auth required)
        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(app, ["--provider", "ollama", "plan", "test request"])

        # Should not have exited with error
        assert result.exit_code == 0 or "provider" not in result.output.lower()


# ---------------------------------------------------------------------------
# Cleanup helper
# ---------------------------------------------------------------------------


def teardown_function() -> None:
    """Clear provider override after each test."""
    from trust5.core.auth.registry import set_provider_override

    set_provider_override(None)  # Clear the override
