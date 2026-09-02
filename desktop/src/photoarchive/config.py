from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    MASTERS_PHOTOS: Path = Field(..., description="Read-only master JPGs exported from the photo app")
    MASTERS_SCANS: Path = Field(..., description="Read-only master scans (JPG + TIFF)")
    WORKING_DIR: Path = Field(..., description="Derived working copies (writable)")
    QUARANTINE_DIR: Path = Field(..., description="Soft-deleted files (writable)")
    MANUAL_FIX_DIR: Path = Field(..., description="Cleanup rejects awaiting manual attention (writable)")
    THUMBS_DIR: Path = Field(..., description="Thumbnail cache (writable)")

    DATABASE_URL: str
    INFERENCE_URL: str
    INFERENCE_TOKEN: str
    WEB_API_URL: str
    WEB_API_TOKEN: str

    def check_masters_isolation(self) -> None:
        writable = {
            "WORKING_DIR": _norm(self.WORKING_DIR),
            "QUARANTINE_DIR": _norm(self.QUARANTINE_DIR),
            "MANUAL_FIX_DIR": _norm(self.MANUAL_FIX_DIR),
            "THUMBS_DIR": _norm(self.THUMBS_DIR),
        }
        for master_name, master_path in (
            ("MASTERS_PHOTOS", _norm(self.MASTERS_PHOTOS)),
            ("MASTERS_SCANS", _norm(self.MASTERS_SCANS)),
        ):
            for writable_name, writable_path in writable.items():
                if _overlaps(master_path, writable_path):
                    raise RuntimeError(
                        f"Refusing to start: {master_name}={master_path} overlaps with "
                        f"{writable_name}={writable_path}. Masters must be isolated from "
                        "writable directories."
                    )


def _norm(p: Path) -> Path:
    return Path(str(p).lower()).resolve() if _is_windows() else p.resolve()


def _is_windows() -> bool:
    import sys as _sys
    return _sys.platform.startswith("win")


def _overlaps(a: Path, b: Path) -> bool:
    if a == b:
        return True
    try:
        a.relative_to(b)
        return True
    except ValueError:
        pass
    try:
        b.relative_to(a)
        return True
    except ValueError:
        pass
    return False


def load() -> Settings:
    settings = Settings()
    settings.check_masters_isolation()
    return settings
