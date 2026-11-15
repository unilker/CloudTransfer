# CloudTransfer

Moves files from Google Drive to OneDrive **without writing them to a disk**.
Each file is read from Drive in byte ranges, held one chunk at a time in
memory, and pushed straight into a OneDrive upload session. There is no
temporary directory, no partial file left behind, and a 200 GB file costs the
same memory as a 200 MB one.

```
Google Drive ──GET (Range)──▶ [ one chunk in RAM ] ──PUT (Content-Range)──▶ OneDrive
```

---

## What this cannot do, and why

A true cloud-to-cloud copy — where the two providers talk to each other and
nothing in between touches the data — **is not possible today**:

- The Google Drive API has no "send this file to a URL" operation.
- Microsoft's "upload from URL" API, which could have pulled a file from a
  public Drive link, **was retired**; it never left preview, was never
  supported for OneDrive for Business or SharePoint, and required making the
  source file publicly readable.

So something has to relay the bytes. This program is that relay, and it does
the one thing that *is* achievable: the bytes never land on a filesystem.

**Where you run it decides whose bandwidth is used:**

| Running on | Written to disk | Uses your connection |
|---|---|---|
| A server or container | No | **No** — your machine only serves the UI |
| Your own computer | No | Yes |

If the goal is "nothing touches my computer", deploy it (see
[Docker](#running-in-a-container)) and open the web interface from anywhere.

---

## What it handles

**Resume.** OneDrive is authoritative about how many bytes it holds. After a
pause, a crash, a dropped connection or a restarted process, each file
continues from exactly that byte — the upload session URL is stored in the
database and asked for its offset, rather than the transfer trusting its own
counter.

**Verification.** Hashes are computed as the bytes stream past, costing one
pass over data already in hand. Each file is checked twice: against Drive's own
`md5Checksum` (did we read it correctly?) and against OneDrive's stored hash
(did it store what we sent?). OneDrive for Business only reports QuickXorHash,
Microsoft's own 160-bit algorithm, so that is implemented here rather than
skipping verification for business accounts. When a transfer resumes mid-file
the hashes cover only part of the content, and the report says *not verified*
instead of guessing.

**Google Docs, Sheets and Slides.** Native Google formats have no bytes to
copy. They are exported to `.docx` / `.xlsx` / `.pptx` so they stay editable in
Office. This is the one path that buffers a whole file, because Graph needs the
total size up front and an export's size is unknown until it arrives — Google
caps exports at 10 MB, so the buffer is bounded. Forms, Sites and My Maps
cannot be exported at all and are reported as skipped, with the reason.

**Renames, never silent.** OneDrive rejects `" * : < > ? / \ |`, reserves
Windows device names, and treats names as unique per folder — while Drive
allows two identical siblings and a folder and file sharing a name. Every
rewrite is recorded on the file and appears in the job report.

**Throttling.** Both providers answer a 429 with `Retry-After`. That hint is
honoured instead of guessing, with exponential backoff and jitter otherwise.

---

## Installing

### Windows

Download `CloudTransfer-Setup-<version>.exe` from the
[releases page](https://github.com/unilker/CloudTransfer/releases) and run it.
It installs for the current user, so no administrator rights are needed, and
adds a Start Menu entry. Launching it opens the interface in your browser and
leaves an icon in the system tray to quit from.

Windows SmartScreen will warn that the publisher is unknown. The installer is
not code-signed, which needs a paid certificate; choose **More info -> Run
anyway**, or build it yourself (see [Building the installer](#building-the-installer)).

Settings, connected accounts and transfer history live in
`%LOCALAPPDATA%\CloudTransfer`, next to a log file worth reading if anything
misbehaves.

### From source, on any platform

```bash
python -m venv .venv && .venv/bin/pip install -e ".[desktop]"
```

```bash
.venv/bin/cloudtransfer desktop
```

That starts the local server, opens a browser and shows a tray icon. For a
plain server with none of that:

```bash
.venv/bin/cloudtransfer serve
```

### In a container

This is the mode where your own machine carries no transfer traffic at all.

```bash
docker compose up -d
```

Deploying somewhere reachable: set `CT_BASE_URL` to the `https://` address and
put TLS in front of it.

### Headless, one transfer at a time

```bash
cloudtransfer transfer --source <drive-folder-id> --dest root --conflict rename
```

Accounts have to be connected through the interface once; the tokens are then
reused. Exit code is non-zero if any file failed.

---

## First run

The application walks you through this, and nothing needs editing in a file.
Both providers require an application of your own before any program may reach
your files. There is no way around that: an unverified third-party app cannot
be handed access to your Drive or OneDrive on your behalf.

### Google

1. [Google Cloud Console](https://console.cloud.google.com/) -> create a project.
2. **APIs & Services -> Library** -> enable **Google Drive API**.
3. **OAuth consent screen** -> External -> add yourself as a test user.
4. **Credentials -> Create credentials -> OAuth client ID -> Desktop app.**
5. Paste the client ID and secret into the setup step.

Scope used: `drive.readonly`. This program never modifies your Drive.

### Microsoft

1. [Azure Portal](https://portal.azure.com/) -> **Microsoft Entra ID -> App
   registrations -> New registration**.
2. Account types: personal **and** work/school.
3. Redirect URI -> platform **Mobile and desktop applications** -> paste the
   address the setup page shows.
4. **API permissions -> Microsoft Graph -> Delegated**: `Files.ReadWrite`,
   `offline_access`, `User.Read`.
5. Paste the **Application (client) ID** into the setup step. A desktop
   registration needs no secret at all.

The flow uses PKCE, which is what makes a client ID shipped inside an installer
safe: an intercepted authorization code cannot be redeemed without the verifier
this process generated and never sent.

### Using environment variables instead

A server deployment can skip the wizard by setting `CT_GOOGLE_CLIENT_ID` and
friends (see `.env.example`). Values set that way appear in the interface as
read-only, since they belong to the deployment rather than to one user.

---

## Building the installer

PyInstaller cannot cross-compile, so a Windows installer has to be built on
Windows. The repository does that in CI: push a tag and the workflow builds it,
checks that the packaged executable really serves a page, and attaches the
installer to a GitHub release.

```bash
git tag v1.0.1 && git push origin v1.0.1
```

By hand, on a Windows machine:

```
python -m pip install -e ".[desktop,build]"
pyinstaller packaging/cloudtransfer.spec --noconfirm
iscc packaging/installer.iss
```

The spec also builds on macOS and Linux, producing an app bundle and a plain
folder; only the Inno Setup step is Windows-specific.

---

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `CT_BASE_URL` | `http://localhost:8080` | Must match the registered redirect URIs |
| `CT_GOOGLE_CLIENT_ID` / `_SECRET` | — | Required |
| `CT_MS_CLIENT_ID` / `_SECRET` | — | Required |
| `CT_MS_TENANT` | `common` | `common`, `consumers`, or a tenant GUID |
| `CT_SECRET_KEY` | generated | Encrypts stored refresh tokens |
| `CT_DATA_DIR` | `./data` | SQLite database and generated key |
| `CT_CHUNK_SIZE` | `10485760` | **Must be a multiple of 327680 (320 KiB)** |
| `CT_SIMPLE_UPLOAD_MAX` | `4194304` | Below this, one PUT instead of a session |
| `CT_MAX_PARALLEL_FILES` | `3` | Peak RAM ≈ chunk × parallel × 2 |
| `CT_MAX_RETRIES` | `6` | Per chunk, before a file is marked failed |
| `CT_VERIFY_HASHES` | `true` | End-to-end hash checking |
| `CT_ACKNOWLEDGE_ABUSE` | `false` | Transfer files Google has flagged |

The 320 KiB rule is Microsoft's: Graph rejects any other non-final chunk size.
It is validated at startup so you get a clear error rather than a 400 on every
upload.

---

## Limits worth knowing

- **Exports cap at 10 MB.** A Google Doc larger than that is skipped with a
  reason; export it by hand or leave it in Drive.
- **OneDrive caps a file at 250 GB** and a whole path at 400 characters. Both
  are checked during planning, so you find out before the transfer runs.
- **Drive-native formats become Office files.** The converted document is a
  different artifact, so Drive's checksum no longer applies to it; only the
  destination side is verified.
- **Shared drives** are readable if the connected account can see them. Folder
  shortcut loops are detected and not followed twice.
- **Nothing is deleted from Drive.** Access is read-only by design; this copies,
  it does not move.

---

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

```bash
.venv/bin/python -m pytest
```

```bash
.venv/bin/python -m ruff check .
```

The suite runs the real engine against in-memory providers that enforce the
actual Graph rules — the 320 KiB chunk multiple, offsets that must line up, a
total size that must match — so a passing test means the engine obeys the real
protocol, not a lenient imitation of it. QuickXorHash is cross-checked against
a second, independently written implementation.

| Area | File |
|---|---|
| Streaming, chunking, resume, verification | `tests/test_relay.py` |
| PKCE, public clients, stored credentials | `tests/test_oauth.py` |
| Port selection, console-less launch | `tests/test_desktop.py` |
| Drive adapter over mocked HTTP | `tests/test_gdrive.py` |
| OneDrive upload protocol | `tests/test_onedrive.py` |
| Tree walking and name resolution | `tests/test_planner.py` |
| Job lifecycle, pause/resume | `tests/test_worker.py` |
| REST surface | `tests/test_api.py` |

---

## Layout

```
cloudtransfer/
  config.py          settings, including the chunk-size rule
  quickxor.py        QuickXorHash, OneDrive for Business's only hash
  naming.py          Drive names → names OneDrive accepts
  http.py            one client, one retry policy
  oauth.py           authorization-code flow for both providers
  accounts.py        token storage and refresh
  providers/
    gdrive.py        source: listing, Range reads, exports
    onedrive.py      destination: upload sessions, chunked PUTs
  engine/
    planner.py       walk the tree, settle every destination name
    transfer.py      the relay itself
    worker.py        job execution, pause and resume
  web/               FastAPI app, REST API, interface
```
