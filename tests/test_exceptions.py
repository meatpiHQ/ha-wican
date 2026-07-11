"""Tests for WiCAN exception classes."""

from __future__ import annotations

import pytest

from custom_components.wican.exceptions import (
    MeatPiConnectionError,
    MeatPiDataError,
    MeatPiDeviceNotFoundError,
    MeatPiError,
    MeatPiWebhookError,
)


def test_wican_error_base_exception():
    """Test MeatPiError base exception."""
    error = MeatPiError("Base error message")
    assert str(error) == "Base error message"
    assert isinstance(error, Exception)


def test_wican_error_inheritance():
    """Test MeatPiError can be caught as Exception."""
    with pytest.raises(Exception) as exc_info:
        raise MeatPiError("Test error")
    
    assert "Test error" in str(exc_info.value)


def test_wican_connection_error():
    """Test MeatPiConnectionError for connection failures."""
    error = MeatPiConnectionError("Failed to connect to device")
    assert str(error) == "Failed to connect to device"
    assert isinstance(error, MeatPiError)
    assert isinstance(error, Exception)


def test_wican_connection_error_inheritance():
    """Test MeatPiConnectionError can be caught as MeatPiError."""
    with pytest.raises(MeatPiError) as exc_info:
        raise MeatPiConnectionError("Connection timeout")
    
    assert isinstance(exc_info.value, MeatPiConnectionError)
    assert "Connection timeout" in str(exc_info.value)


def test_wican_device_not_found_error():
    """Test MeatPiDeviceNotFoundError for device discovery failures."""
    error = MeatPiDeviceNotFoundError("Device not found on network")
    assert str(error) == "Device not found on network"
    assert isinstance(error, MeatPiError)
    assert isinstance(error, Exception)


def test_wican_device_not_found_error_inheritance():
    """Test MeatPiDeviceNotFoundError can be caught as MeatPiError."""
    with pytest.raises(MeatPiError) as exc_info:
        raise MeatPiDeviceNotFoundError("Device unreachable")
    
    assert isinstance(exc_info.value, MeatPiDeviceNotFoundError)
    assert "Device unreachable" in str(exc_info.value)


def test_wican_webhook_error():
    """Test MeatPiWebhookError for webhook-related issues."""
    error = MeatPiWebhookError("Webhook registration failed")
    assert str(error) == "Webhook registration failed"
    assert isinstance(error, MeatPiError)
    assert isinstance(error, Exception)


def test_wican_webhook_error_inheritance():
    """Test MeatPiWebhookError can be caught as MeatPiError."""
    with pytest.raises(MeatPiError) as exc_info:
        raise MeatPiWebhookError("Invalid webhook payload")
    
    assert isinstance(exc_info.value, MeatPiWebhookError)
    assert "Invalid webhook payload" in str(exc_info.value)


def test_wican_data_error():
    """Test MeatPiDataError for data validation failures."""
    error = MeatPiDataError("Invalid data format")
    assert str(error) == "Invalid data format"
    assert isinstance(error, MeatPiError)
    assert isinstance(error, Exception)


def test_wican_data_error_inheritance():
    """Test MeatPiDataError can be caught as MeatPiError."""
    with pytest.raises(MeatPiError) as exc_info:
        raise MeatPiDataError("Malformed JSON")
    
    assert isinstance(exc_info.value, MeatPiDataError)
    assert "Malformed JSON" in str(exc_info.value)


def test_exception_hierarchy():
    """Test exception hierarchy allows catching by base class."""
    exceptions = [
        MeatPiConnectionError("Connection error"),
        MeatPiDeviceNotFoundError("Device not found"),
        MeatPiWebhookError("Webhook error"),
        MeatPiDataError("Data error"),
    ]
    
    for exc in exceptions:
        # All specific exceptions should be catchable as MeatPiError
        assert isinstance(exc, MeatPiError)
        # All should ultimately be Exception
        assert isinstance(exc, Exception)


def test_exception_with_empty_message():
    """Test exceptions can be raised with no message."""
    error = MeatPiError()
    assert str(error) == ""
    
    # Should still be catchable
    with pytest.raises(MeatPiError):
        raise MeatPiError()


def test_exception_with_args():
    """Test exceptions support multiple arguments."""
    error = MeatPiConnectionError("Connection failed", "192.168.1.100", 80)
    assert error.args == ("Connection failed", "192.168.1.100", 80)


def test_multiple_exceptions_catchable():
    """Test catching multiple exception types."""
    # Test that we can catch either specific or base exception
    with pytest.raises((MeatPiConnectionError, MeatPiDeviceNotFoundError)):
        raise MeatPiConnectionError("Test")
    
    with pytest.raises((MeatPiConnectionError, MeatPiDeviceNotFoundError)):
        raise MeatPiDeviceNotFoundError("Test")


def test_exception_repr():
    """Test exception representations."""
    error = MeatPiError("Test error")
    # Exception repr typically shows the class and message
    repr_str = repr(error)
    assert "MeatPiError" in repr_str or "Test error" in repr_str


def test_reraise_preserves_traceback():
    """Test that re-raising exceptions preserves context."""
    try:
        try:
            raise ValueError("Original error")
        except ValueError as err:
            raise MeatPiDataError("Wrapped error") from err
    except MeatPiDataError as exc:
        assert exc.__cause__ is not None
        assert isinstance(exc.__cause__, ValueError)
        assert str(exc.__cause__) == "Original error"


def test_exception_equality():
    """Test exception instances with same message are not equal."""
    error1 = MeatPiError("Same message")
    error2 = MeatPiError("Same message")
    # Exception instances are compared by identity, not value
    assert error1 is not error2
    assert str(error1) == str(error2)


def test_all_exceptions_exported():
    """Test that all exception classes are properly defined."""
    # Verify all expected exceptions exist and are classes
    assert isinstance(MeatPiError, type)
    assert isinstance(MeatPiConnectionError, type)
    assert isinstance(MeatPiDeviceNotFoundError, type)
    assert isinstance(MeatPiWebhookError, type)
    assert isinstance(MeatPiDataError, type)
    
    # Verify they're all Exception subclasses
    assert issubclass(MeatPiError, Exception)
    assert issubclass(MeatPiConnectionError, MeatPiError)
    assert issubclass(MeatPiDeviceNotFoundError, MeatPiError)
    assert issubclass(MeatPiWebhookError, MeatPiError)
    assert issubclass(MeatPiDataError, MeatPiError)
