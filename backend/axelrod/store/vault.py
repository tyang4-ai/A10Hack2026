"""The vault: raw files, page text, reports and transcripts per run (CONTRACT.md section 10 paths).
Local mode writes under DATA_DIR, aws mode writes to the S3 bucket."""
from __future__ import annotations

from ..config import Settings
from ..contract import m


class Vault:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def put_bytes(self, run_id: str, relpath: str, data: bytes) -> str:
        """Store data at relpath inside the run's vault. Returns the vault path as the API shows it."""
        raise NotImplementedError("B-store-api")

    def put_text(self, run_id: str, relpath: str, text: str) -> str:
        """UTF-8 text version of put_bytes. Returns the vault path."""
        raise NotImplementedError("B-store-api")

    def get_bytes(self, run_id: str, relpath: str) -> bytes:
        raise NotImplementedError("B-store-api")

    def get_text(self, run_id: str, relpath: str) -> str:
        raise NotImplementedError("B-store-api")

    def tree(self, run_id: str) -> m.VaultNode:
        raise NotImplementedError("B-store-api")

    def file(self, run_id: str, path: str) -> m.VaultFile:
        raise NotImplementedError("B-store-api")
