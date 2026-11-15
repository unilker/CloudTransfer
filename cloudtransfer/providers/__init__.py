"""Provider adapters. Each side of a transfer is a source or a destination."""

from .base import DestProvider, RemoteItem, SourceProvider, UploadSession
from .gdrive import GoogleDriveDest, GoogleDriveSource
from .onedrive import OneDriveDest, OneDriveSource

__all__ = [
    "DestProvider",
    "GoogleDriveDest",
    "GoogleDriveSource",
    "OneDriveDest",
    "OneDriveSource",
    "RemoteItem",
    "SourceProvider",
    "UploadSession",
]
