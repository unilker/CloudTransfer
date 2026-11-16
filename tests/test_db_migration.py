"""An existing database must survive a new column.

``create_all`` creates missing tables and never touches existing ones, so
before ``_add_missing_columns`` a new model field broke every install that
already had a database. That database holds the connected Google and
Microsoft accounts, so the cost of getting this wrong is someone being asked
to set the program up again - or, worse, an upgrade that simply refuses to
start.
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import select

from cloudtransfer.config import Settings
from cloudtransfer.db import dispose_db, init_db, session_scope
from cloudtransfer.models import Job

# The jobs table as it shipped before plan_complete existed.
OLD_JOBS_TABLE = """
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    status VARCHAR(32),
    direction VARCHAR(32) NOT NULL,
    source_folder_id VARCHAR(256) NOT NULL,
    source_folder_name VARCHAR(512),
    source_drive_id VARCHAR(256),
    dest_folder_id VARCHAR(256) NOT NULL,
    dest_folder_path TEXT,
    selection JSON,
    conflict_policy VARCHAR(16),
    verify BOOLEAN,
    include_trashed BOOLEAN,
    total_files INTEGER,
    total_bytes BIGINT,
    done_files INTEGER,
    done_bytes BIGINT,
    failed_files INTEGER,
    skipped_files INTEGER,
    error TEXT,
    created_at DATETIME,
    started_at DATETIME,
    finished_at DATETIME
)
"""


@pytest.fixture
def old_database(tmp_path):
    """A database from before the current models, with a row worth keeping."""
    settings = Settings(data_dir=tmp_path)
    con = sqlite3.connect(settings.db_path)
    con.execute(OLD_JOBS_TABLE)
    con.execute(
        "INSERT INTO jobs (id, status, direction, source_folder_id, dest_folder_id,"
        " total_files, done_files) VALUES (1, 'completed', 'onedrive_to_gdrive',"
        " 'src', 'dst', 7, 7)"
    )
    con.commit()
    con.close()
    return settings


async def test_a_missing_column_is_added_in_place(old_database):
    await init_db(old_database)
    await dispose_db()

    con = sqlite3.connect(old_database.db_path)
    columns = {row[1]: row for row in con.execute("PRAGMA table_info(jobs)")}
    con.close()

    assert "plan_complete" in columns, "the new column was never added"
    # NOT NULL with a default, or the insert of any existing row would fail.
    notnull, default = columns["plan_complete"][3], columns["plan_complete"][4]
    assert notnull == 1
    assert default == "0"


async def test_the_rows_already_there_are_not_disturbed(old_database):
    await init_db(old_database)

    async with session_scope(old_database) as session:
        job = (await session.execute(select(Job))).scalars().one()
        assert job.id == 1
        assert job.status == "completed"
        assert job.total_files == 7
        assert job.done_files == 7
        # Backfilled rather than left null, so existing jobs read as counted.
        assert job.plan_complete is False

    await dispose_db()


async def test_tables_the_old_database_never_had_are_created(old_database):
    """The accounts table is the one that must not be lost or half-built."""
    await init_db(old_database)
    await dispose_db()

    con = sqlite3.connect(old_database.db_path)
    tables = {row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    con.close()

    assert {"accounts", "file_tasks", "jobs", "oauth_apps"} <= tables


async def test_running_it_twice_changes_nothing(old_database):
    """Startup runs this every time, so it has to be a no-op once applied."""
    await init_db(old_database)
    await dispose_db()
    await init_db(old_database)
    await dispose_db()

    con = sqlite3.connect(old_database.db_path)
    names = [row[1] for row in con.execute("PRAGMA table_info(jobs)")]
    con.close()

    assert names.count("plan_complete") == 1
