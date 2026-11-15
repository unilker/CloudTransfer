"""Database models: connected accounts, transfer jobs, per-file tasks.

Job and file state lives in SQLite rather than memory so a transfer survives a
restart. A file task carries its OneDrive ``upload_url``, which is what makes
resume possible: Graph keeps the upload session alive for days and will tell us
exactly how many bytes it already has.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


# -- status vocabularies ----------------------------------------------------

class JobStatus:
    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"
    CANCELLED = "cancelled"

    TERMINAL = {COMPLETED, COMPLETED_WITH_ERRORS, FAILED, CANCELLED}
    ACTIVE = {PENDING, PLANNING, RUNNING}


class TaskStatus:
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class Direction:
    """Which way a job moves files."""

    GDRIVE_TO_ONEDRIVE = "gdrive_to_onedrive"
    ONEDRIVE_TO_GDRIVE = "onedrive_to_gdrive"

    ALL = (GDRIVE_TO_ONEDRIVE, ONEDRIVE_TO_GDRIVE)

    LABELS = {
        GDRIVE_TO_ONEDRIVE: "Google Drive to OneDrive",
        ONEDRIVE_TO_GDRIVE: "OneDrive to Google Drive",
    }

    #: provider key of each side, for picking adapters and naming policies
    SIDES = {
        GDRIVE_TO_ONEDRIVE: ("gdrive", "onedrive"),
        ONEDRIVE_TO_GDRIVE: ("onedrive", "gdrive"),
    }


class ConflictPolicy:
    """What to do when the destination already holds a file of the same name."""

    RENAME = "rename"    # keep both; OneDrive-style " (1)" suffix
    REPLACE = "replace"  # overwrite the destination copy
    SKIP = "skip"        # leave the destination alone, record as skipped

    ALL = (RENAME, REPLACE, SKIP)


# -- tables -----------------------------------------------------------------

class Account(Base):
    """A connected Google or Microsoft account, one row per provider."""

    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("provider", name="uq_accounts_provider"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    display_name: Mapped[str] = mapped_column(String(256), default="")
    email: Mapped[str] = mapped_column(String(256), default="")

    access_token_enc: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_enc: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[float] = mapped_column(Float, default=0.0)
    scope: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    @property
    def label(self) -> str:
        return self.email or self.display_name or self.provider

    @property
    def token_expired(self) -> bool:
        return time.time() >= (self.expires_at or 0) - 60


class OAuthAppRow(Base):
    """The registered Google/Microsoft application, set up inside the app.

    Keeping these in the database rather than only in environment variables is
    what lets the packaged desktop build configure itself: the first-run wizard
    writes them here, so there is no file for the user to edit. Environment
    variables still win nothing and lose nothing - they act as a fallback for
    server deployments.
    """

    __tablename__ = "oauth_apps"
    __table_args__ = (UniqueConstraint("provider", name="uq_oauth_apps_provider"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Empty for a public (desktop) client, which has no secret at all.
    client_secret_enc: Mapped[str] = mapped_column(Text, default="")
    tenant: Mapped[str] = mapped_column(String(64), default="common")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Job(Base):
    """One transfer request: a Drive folder (or file set) into a OneDrive folder."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.PENDING, index=True)
    direction: Mapped[str] = mapped_column(
        String(32), default=Direction.GDRIVE_TO_ONEDRIVE, nullable=False
    )

    source_folder_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_folder_name: Mapped[str] = mapped_column(String(512), default="")
    source_drive_id: Mapped[str | None] = mapped_column(String(256), nullable=True)

    dest_folder_id: Mapped[str] = mapped_column(String(256), nullable=False)
    dest_folder_path: Mapped[str] = mapped_column(Text, default="")

    # Specific items inside the source folder to move. Empty or absent means
    # the whole folder, which is what most transfers want.
    selection: Mapped[list | None] = mapped_column(JSON, nullable=True)

    conflict_policy: Mapped[str] = mapped_column(String(16), default=ConflictPolicy.RENAME)
    verify: Mapped[bool] = mapped_column(Boolean, default=True)
    include_trashed: Mapped[bool] = mapped_column(Boolean, default=False)

    total_files: Mapped[int] = mapped_column(Integer, default=0)
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    done_files: Mapped[int] = mapped_column(Integer, default=0)
    done_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    failed_files: Mapped[int] = mapped_column(Integer, default=0)
    skipped_files: Mapped[int] = mapped_column(Integer, default=0)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tasks: Mapped[list[FileTask]] = relationship(
        back_populates="job", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def is_active(self) -> bool:
        return self.status in JobStatus.ACTIVE

    @property
    def direction_label(self) -> str:
        return Direction.LABELS.get(self.direction, self.direction)

    @property
    def percent(self) -> float:
        if self.total_bytes:
            return min(100.0, self.done_bytes * 100.0 / self.total_bytes)
        if self.total_files:
            settled = self.done_files + self.failed_files + self.skipped_files
            return min(100.0, settled * 100.0 / self.total_files)
        return 0.0


class FileTask(Base):
    """One file inside a job, with everything needed to resume it."""

    __tablename__ = "file_tasks"
    __table_args__ = (
        Index("ix_file_tasks_job_status", "job_id", "status"),
        Index("ix_file_tasks_job_source", "job_id", "source_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default=TaskStatus.PENDING)

    # Source side
    source_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_name: Mapped[str] = mapped_column(String(512), default="")
    source_mime: Mapped[str] = mapped_column(String(256), default="")
    source_size: Mapped[int] = mapped_column(BigInteger, default=0)
    source_md5: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_sha256: Mapped[str | None] = mapped_column(String(128), nullable=True)
    export_mime: Mapped[str | None] = mapped_column(String(256), nullable=True)
    size_is_estimate: Mapped[bool] = mapped_column(Boolean, default=False)

    # Destination side
    rel_path: Mapped[str] = mapped_column(Text, default="")
    dest_name: Mapped[str] = mapped_column(String(512), default="")
    dest_parent_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    dest_item_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    rename_notes: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Progress / resume
    bytes_done: Mapped[int] = mapped_column(BigInteger, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    upload_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    upload_expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)

    verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    hash_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped[Job] = relationship(back_populates="tasks")

    @property
    def percent(self) -> float:
        if not self.source_size:
            return 100.0 if self.status == TaskStatus.DONE else 0.0
        return min(100.0, self.bytes_done * 100.0 / self.source_size)
