"""Tests for S3StorageBackend.

These mock the boto3 client boundary (moto/real AWS credentials aren't
available in this environment) and verify OUR code calls boto3 correctly:
right bucket/key/content-type, presigned URL requested with the right params,
and that upload/delete failures are handled as documented. This validates our
integration logic; it does not prove AWS S3 itself behaves as expected.
Operators should smoke-test against their real bucket before relying on this
in production (same caveat as ACRCloud — see ARCHITECTURE.md §6).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from app.services.storage.s3_backend import S3StorageBackend


@pytest.fixture
def mock_boto_client() -> MagicMock:
    client = MagicMock()
    client.generate_presigned_url.return_value = "https://bucket.s3.amazonaws.com/instasongbot/song.mp3?X-Amz-Signature=abc"
    return client


async def test_upload_calls_boto_with_correct_bucket_and_key(mock_boto_client: MagicMock, tmp_path: Path) -> None:
    with patch("boto3.client", return_value=mock_boto_client):
        backend = S3StorageBackend(bucket="my-bucket", region="us-east-1")

    source = tmp_path / "song.mp3"
    source.write_bytes(b"data")

    stored = await backend.upload(source, filename="song.mp3", content_type="audio/mpeg")

    mock_boto_client.upload_file.assert_called_once_with(
        str(source), "my-bucket", "instasongbot/song.mp3", ExtraArgs={"ContentType": "audio/mpeg"}
    )
    mock_boto_client.generate_presigned_url.assert_called_once_with(
        "get_object", Params={"Bucket": "my-bucket", "Key": "instasongbot/song.mp3"}, ExpiresIn=3600
    )
    assert stored.url == "https://bucket.s3.amazonaws.com/instasongbot/song.mp3?X-Amz-Signature=abc"
    assert stored.expires_in_seconds == 3600


async def test_upload_guesses_content_type_when_not_provided(mock_boto_client: MagicMock, tmp_path: Path) -> None:
    with patch("boto3.client", return_value=mock_boto_client):
        backend = S3StorageBackend(bucket="my-bucket", region="us-east-1")

    source = tmp_path / "video.mp4"
    source.write_bytes(b"data")

    await backend.upload(source, filename="video.mp4", content_type="")

    _, kwargs = mock_boto_client.upload_file.call_args
    assert kwargs["ExtraArgs"]["ContentType"] == "video/mp4"


async def test_upload_uses_custom_key_prefix(mock_boto_client: MagicMock, tmp_path: Path) -> None:
    with patch("boto3.client", return_value=mock_boto_client):
        backend = S3StorageBackend(bucket="my-bucket", region="us-east-1", key_prefix="custom/prefix")

    source = tmp_path / "img.jpg"
    source.write_bytes(b"data")
    await backend.upload(source, filename="img.jpg", content_type="image/jpeg")

    args, _ = mock_boto_client.upload_file.call_args
    assert args[2] == "custom/prefix/img.jpg"


async def test_upload_propagates_client_error(mock_boto_client: MagicMock, tmp_path: Path) -> None:
    mock_boto_client.upload_file.side_effect = ClientError({"Error": {"Code": "500", "Message": "boom"}}, "PutObject")
    with patch("boto3.client", return_value=mock_boto_client):
        backend = S3StorageBackend(bucket="my-bucket", region="us-east-1")

    source = tmp_path / "song.mp3"
    source.write_bytes(b"data")

    with pytest.raises(ClientError):
        await backend.upload(source, filename="song.mp3", content_type="audio/mpeg")


async def test_delete_calls_boto_with_correct_key(mock_boto_client: MagicMock) -> None:
    with patch("boto3.client", return_value=mock_boto_client):
        backend = S3StorageBackend(bucket="my-bucket", region="us-east-1")

    await backend.delete("song.mp3")

    mock_boto_client.delete_object.assert_called_once_with(Bucket="my-bucket", Key="instasongbot/song.mp3")


async def test_delete_swallows_client_error(mock_boto_client: MagicMock) -> None:
    mock_boto_client.delete_object.side_effect = ClientError({"Error": {"Code": "404", "Message": "gone"}}, "DeleteObject")
    with patch("boto3.client", return_value=mock_boto_client):
        backend = S3StorageBackend(bucket="my-bucket", region="us-east-1")

    await backend.delete("song.mp3")  # must not raise


def test_requires_bucket_name() -> None:
    with pytest.raises(ValueError, match="S3_BUCKET"):
        S3StorageBackend(bucket="", region="us-east-1")


def test_uses_custom_endpoint_for_s3_compatible_providers(mock_boto_client: MagicMock) -> None:
    with patch("boto3.client", return_value=mock_boto_client) as client_factory:
        S3StorageBackend(bucket="my-bucket", region="us-east-1", endpoint_url="https://minio.example.com")

    _, kwargs = client_factory.call_args
    assert kwargs["endpoint_url"] == "https://minio.example.com"
