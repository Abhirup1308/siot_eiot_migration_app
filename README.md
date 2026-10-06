# SIoT → EIoT Migration Studio

A local Streamlit application for migrating historical time-series data from SIoT Cold Store to EIoT. It discovers source indicators, maps them to target indicators, converts exports to Parquet, and uploads selected files through validation and production approval steps. SQLite stores progress and an activity history.

## Run locally from VS Code

**Prerequisites:** Python 3.13, access to the source and target SAP services, and their credentials. The supplied development environment used Python 3.13.3. No separate database server or frontend build is required.

After cloning or pulling the repository, open the folder containing `app.py` and `requirements.txt` in VS Code. Open **Terminal → New Terminal** and run the commands for your operating system.

### 1. First-time setup

**Windows — PowerShell**

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

**macOS / Linux**

```bash
python3.13 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

Create `.env` only on first setup; keep your existing file when pulling updates. Create a fresh virtual environment on each machine instead of copying another machine's `.venv`.

### 2. Configure the connections

Open `.env` and fill in the client IDs, client secrets, token URLs, service base URLs, and API keys for:

- **SIoT Cold Store:** source export and download access.
- **SIoT IndicatorService:** source metadata access.
- **EIoT:** target metadata and file-upload access.

Get these values from the SAP tenant/service owner. Set both APM base URLs explicitly for the intended environments. See [Configuration](docs/HANDOVER.md#configuration) for the exact fields and authentication options.

### 3. Start the app

**Windows — PowerShell**

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py --server.address localhost
```

**macOS / Linux**

```bash
./.venv/bin/python -m streamlit run app.py --server.address localhost
```

Open [http://localhost:8501](http://localhost:8501). Stop the app with **Ctrl+C**. Use the same start command on later visits; rerun the installation command if dependencies change after a pull. Restart after changing `.env`.

For VS Code's editor, use **Python: Select Interpreter** and select `.venv`. Terminal activation is unnecessary with the commands above.

**“Systems configured” means the required settings are present; it does not test the SAP connections.**

## APIs used

| API | Purpose |
| --- | --- |
| OAuth token endpoints | Obtain separate access tokens for Cold Store, SIoT metadata, and EIoT using client credentials. |
| SIoT `IndicatorService/v1/Indicators` | Discover source Technical Objects, positions, categories, and characteristics. |
| SIoT Cold Store | Initiate historical exports, check export status, and download archives. |
| EIoT `IndicatorService/v1/Indicators` | Find matching target indicators. |
| EIoT `EIoTMetadataSyncService/v1/TechnicalObjects(...)` | Resolve target `managedObjectId`, `measuringNodeId`, data types, and sync status. |
| EIoT `FileUploadService` | Upload validation/production Parquet files and check processing results. |

The app calls these services directly from Python. It does not expose its own business REST API. Exact methods, paths, authentication, and response fields are in the [API reference](docs/HANDOVER.md#api-reference).

## Overall workflow

1. **Scope:** Create a migration using pasted Technical Object IDs or an Excel workbook and an inclusive date range. Run **Discover selected**.
2. **Extraction:** Select position/date jobs and start exports. Monitor SAP status; ready exports download automatically by default and are unpacked into CSVs.
3. **Transform:** Select downloaded CSVs. Map source indicators to EIoT and create Parquet files. Review mapped and skipped combinations.
4. **Validation:** Create a sample, download its review report, upload it, and wait for EIoT status `Processed`. Record customer approval after checking the data in EIoT.
5. **Production:** Select transformed outputs, prepare upload files, review the manifest, and approve the production load.
6. **Upload:** Send selected files in batches. Wait for the current batch to finish processing before sending the next. Resolve failed files and retry them.
7. **Close out:** Reconcile the requested scope with the reports, save the evidence, and clean temporary files after completion.

**Validation is a real EIoT upload.** Validation rows are also included in production files. Confirm the tenant's duplicate/overwrite behaviour before loading. A production status of `COMPLETE` means all prepared production files reached `Processed`; it does not prove that every originally requested object was migrated.

## Handover and repository notes

Read [docs/HANDOVER.md](docs/USER_GUIDE.md) for operating instructions, mapping rules, recovery, reports, and implementation limits.

Progress is stored in `data/migration.sqlite3`, with files under `data/runs/<run-id>/`. Preserve both to resume work. The supplied `.gitignore` excludes `.env`, `.venv/`, `data/`, and Python cache files. Commit `.env.example`; keep actual credentials and migration data out of Git.
