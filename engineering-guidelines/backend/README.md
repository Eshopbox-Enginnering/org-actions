# Backend guideline sources

Upload the five original Markdown documents from Central Backlog #7218 into this folder, keeping their original filenames.

After uploading, change each `backend` entry in `../catalog.json` to use `engineering-guidelines/backend/<filename>.md` as its `source_url`. Do not create placeholder guideline content; keep the current attachment URLs until the originals are available.

The sync workflow copies these sources into `docs/engineering-guidelines/` in each selected backend repository. It replaces only selected destination files, without modifying other documentation.
