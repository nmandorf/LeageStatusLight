"""Shared errors that are safe to display without exposing account details."""


class SetupError(Exception):
    """A user-facing configuration, device-capability, or command error."""
