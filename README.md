# Storage Organizer

A preview-first, dependency-free Python command-line tool for organizing local files and comparing mounted cloud storage with local drives. It inventories files, previews configurable moves, finds exact duplicates by SHA-256, compares backup folders, catalogs archives, and can localize cloud-only files with verified copies. It never permanently deletes files.

## Safety model

- Scans only folders explicitly listed in the JSON config.
- Skips symlinks, system metadata, app-managed libraries, code environments, and cloud-provider folders by default.
- `plan` is a preview. Files move only with `apply --yes` on a saved plan.
- Duplicate cleanup is a reversible move into `Storage Quarantine - Review`; it is never a permanent delete.
- Every applied move is written to a journal. `rollback` previews a reversal and requires `--yes` to perform it.
- Cross-volume moves and destination overwrites are refused.
- ZIPs and multipart archives are cataloged or moved as files; they are never extracted or renamed by the built-in rules.

## Requirements

Python 3.9 or newer. No packages to install.

## First run

```sh
git clone https://github.com/Ishannilawar2002/storage-organizer-cli.git
cd storage-organizer-cli
```

```sh
python3 storage_organizer.py init --config storage-organizer.json
```

Edit `storage-organizer.json` and add the folders you want scanned to `roots`. The starter config only includes Desktop and Downloads; add mounted drives explicitly, for example:

```json
"roots": [
  "~/Desktop",
  "~/Downloads",
  "/Volumes/External HDD",
  "/Volumes/Backup SSD"
]
```

Then preview file counts and large files:

```sh
python3 storage_organizer.py inventory --config storage-organizer.json
python3 storage_organizer.py archives --config storage-organizer.json
```

## Preview and apply organization rules

The starter rules sort Desktop screenshots and recordings into `YYYY-MM` folders, and route loose ZIP/RAR/7z files from the Desktop into `Archives/To Sort`.

```sh
python3 storage_organizer.py plan --config storage-organizer.json --out my-plan.json
python3 storage_organizer.py apply --plan my-plan.json
python3 storage_organizer.py apply --plan my-plan.json --yes
```

The first `apply` command only previews. The second makes the moves and creates a journal under `~/.storage-organizer/journals/`.

Rules support filename globs, regular expressions with named groups, extension lists, minimum/maximum sizes, and maximum depth. A `rename` template is optional; without one, the original filename is preserved. For example:

```json
{
  "name": "Sort dated PDFs",
  "root": "~/Downloads",
  "match": {
    "extensions": [".pdf"],
    "regex": "^(?P<year>20\\d{2})-(?P<month>\\d{2})-"
  },
  "destination": "~/Documents/Receipts/{year}/{year}-{month}"
}
```

For a folder rename or a whole-folder move, add an explicit entry to `moves`:

```json
"moves": [
  {
    "name": "Label the old phone archive",
    "source": "/Volumes/External HDD/Old Photo Archive",
    "destination": "/Volumes/External HDD/Backups/Photos/Old Photo Archive"
  }
]
```

Destinations must stay inside one of the configured roots and on the same volume as the source.

## Duplicate review

This hashes all candidate file contents, so identical files with different names can still match. Files are grouped by size before hashing to reduce unnecessary reads. Protected paths are skipped.

```sh
python3 storage_organizer.py duplicates --config storage-organizer.json --out duplicates.json
python3 storage_organizer.py quarantine-plan --config storage-organizer.json --report duplicates.json --out quarantine.json
python3 storage_organizer.py apply --plan quarantine.json
python3 storage_organizer.py apply --plan quarantine.json --yes
```

Review the quarantine before removing anything. To restore a run, use the journal printed by `apply`:

```sh
python3 storage_organizer.py rollback --journal ~/.storage-organizer/journals/journal-YYYYMMDD-HHMMSS.json
python3 storage_organizer.py rollback --journal ~/.storage-organizer/journals/journal-YYYYMMDD-HHMMSS.json --yes
```

## Google Drive and HDD reconciliation

This mode compares a mounted Google Drive folder (or another mounted cloud folder) with a local HDD folder by full SHA-256 content. It can find exact cloud copies already present on the HDD and prepare verified copies for cloud content that is not present locally. The original cloud files stay in place.

First, mount the provider with its desktop app and add the specific mounted cloud folder and HDD destination to `roots` in your config. For Google Drive for desktop on macOS, the folder is usually beneath `~/Library/CloudStorage/GoogleDrive-.../My Drive`; use the actual folder name shown on your Mac. Keep the HDD destination explicit, for example `/Volumes/External HDD`.

Run a read-only comparison:

```sh
python3 storage_organizer.py reconcile \
  --cloud-root "$HOME/Library/CloudStorage/GoogleDrive-you@example.com/My Drive" \
  --local-root "/Volumes/External HDD" \
  --config storage-organizer.json \
  --out google-drive-hdd-report.json
```

The report groups content found in both places, cloud-only content, HDD-only content, and exact cloud paths that could be reviewed for removal. Candidate paths are informational; this tool does not remove files from Google Drive or any provider.

Prepare a copy plan for content found only in the mounted cloud folder:

```sh
python3 storage_organizer.py localize-plan \
  --report google-drive-hdd-report.json \
  --config storage-organizer.json \
  --out localize-to-hdd-plan.json
python3 storage_organizer.py apply --plan localize-to-hdd-plan.json
```

Review the listed destinations and conflicts. To make the copies, rerun `apply` with `--yes`. It preserves the cloud folder's relative paths, copies one copy of each unique file content, verifies each copy against its planned size and SHA-256, attempts to retain standard file metadata, and records it in the journal. Existing files with different content are conflicts and are left alone. Rollback previews and can remove only the created copies that still have the journaled content:

```sh
python3 storage_organizer.py rollback --journal ~/.storage-organizer/journals/journal-YYYYMMDD-HHMMSS.json
python3 storage_organizer.py rollback --journal ~/.storage-organizer/journals/journal-YYYYMMDD-HHMMSS.json --yes
```

Hashing reads the contents of every file it scans. For streamed cloud files, the provider may download file data into its local cache and use network bandwidth; this tool does not fetch cloud data through an API. It only sees files exposed by the mounted folder and readable as ordinary files. Google One data outside Drive, such as Google Photos, Gmail, and device backups, is not included. Native Google Docs may need to be exported to ordinary files before their document contents can be compared. Reports contain local paths and filenames, so keep them private.

## Backup comparisons and archive catalog

Compare two folder trees by relative path, size, and timestamp. Add `--hash` to compare matching files byte-for-byte:

```sh
python3 storage_organizer.py clone-check \
  "/Volumes/External HDD/Backup A" "/Volumes/External HDD/Backup B" \
  --config storage-organizer.json --hash
```

`archives` creates a JSON catalog of ZIP, RAR, TAR, DMG, ISO, and related files, including likely multipart groups. Other commands do not connect to provider APIs; mount or sync folders locally before including them in `roots`.

## Customize protections

Edit `ignore_dir_names`, `ignore_globs`, `protected_dir_names`, `protected_globs`, `ignore_file_globs`, `archive_extensions`, and `keeper_preference` in the JSON config. Protected data is excluded from moves and duplicate quarantine. Keep roots narrow and review every generated plan before applying it.

## Development

The project uses only the Python standard library. Run its test suite from the repository root:

```sh
python3 -m unittest discover -s tests -v
```
