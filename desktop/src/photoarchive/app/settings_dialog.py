from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
)

from ..config import Settings, _parse_master_roots

log = logging.getLogger(__name__)


class SettingsDialog(QDialog):
    """Edits the .env values. Reload happens on the caller side after save."""

    def __init__(
        self,
        settings: Settings,
        env_path: Path,
        photos_exist: bool,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self._settings = settings
        self._env_path = env_path
        self._photos_exist = photos_exist

        form = QFormLayout()

        self._master_roots = QPlainTextEdit(settings.MASTER_ROOTS)
        self._master_roots.setPlaceholderText(
            "label=path[|kind]; separated. Kind is 'digital' (default) or 'scan'."
        )
        if photos_exist:
            self._master_roots.setReadOnly(True)
            self._master_roots.setToolTip(
                "Root paths and labels are locked once any photo references them."
            )
        form.addRow("MASTER_ROOTS", self._master_roots)

        self._working = QLineEdit(str(settings.WORKING_DIR))
        self._quarantine = QLineEdit(str(settings.QUARANTINE_DIR))
        self._manual_fix = QLineEdit(str(settings.MANUAL_FIX_DIR))
        self._thumbs = QLineEdit(str(settings.THUMBS_DIR))
        form.addRow("WORKING_DIR", self._working)
        form.addRow("QUARANTINE_DIR", self._quarantine)
        form.addRow("MANUAL_FIX_DIR", self._manual_fix)
        form.addRow("THUMBS_DIR", self._thumbs)

        self._db = QLineEdit(settings.DATABASE_URL)
        self._inference_url = QLineEdit(settings.INFERENCE_URL)
        self._inference_token = QLineEdit(settings.INFERENCE_TOKEN)
        self._inference_token.setEchoMode(QLineEdit.Password)
        self._web_url = QLineEdit(settings.WEB_API_URL)
        self._web_token = QLineEdit(settings.WEB_API_TOKEN)
        self._web_token.setEchoMode(QLineEdit.Password)
        form.addRow("DATABASE_URL", self._db)
        form.addRow("INFERENCE_URL", self._inference_url)
        form.addRow("INFERENCE_TOKEN", self._inference_token)
        form.addRow("WEB_API_URL", self._web_url)
        form.addRow("WEB_API_TOKEN", self._web_token)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)

        note = QLabel(
            "Saves to the .env file. Restart the app to pick up changes.\n"
            "Root paths/labels are locked once photos reference them."
        )
        note.setStyleSheet("color: gray")

        outer = QVBoxLayout(self)
        outer.addLayout(form)
        outer.addWidget(note)
        outer.addWidget(buttons)
        self.resize(700, 500)

    def _on_save(self) -> None:
        try:
            roots_text = self._master_roots.toPlainText().strip()
            if not self._photos_exist:
                # Validate parse only when we're actually allowed to change it.
                list(_parse_master_roots(roots_text))
        except ValueError as e:
            QMessageBox.critical(self, "Invalid MASTER_ROOTS", str(e))
            return

        values = {
            "MASTER_ROOTS": roots_text,
            "WORKING_DIR": self._working.text().strip(),
            "QUARANTINE_DIR": self._quarantine.text().strip(),
            "MANUAL_FIX_DIR": self._manual_fix.text().strip(),
            "THUMBS_DIR": self._thumbs.text().strip(),
            "DATABASE_URL": self._db.text().strip(),
            "INFERENCE_URL": self._inference_url.text().strip(),
            "INFERENCE_TOKEN": self._inference_token.text().strip(),
            "WEB_API_URL": self._web_url.text().strip(),
            "WEB_API_TOKEN": self._web_token.text().strip(),
        }
        try:
            _rewrite_env(self._env_path, values)
        except OSError as e:
            QMessageBox.critical(self, "Could not save .env", str(e))
            return
        QMessageBox.information(
            self, "Saved", f"Settings written to {self._env_path}.\nRestart the app to reload."
        )
        self.accept()


def _rewrite_env(path: Path, values: dict[str, str]) -> None:
    """Preserve comments and unrelated keys; overwrite the ones we manage."""
    existing_lines: list[str] = []
    if path.exists():
        existing_lines = path.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in values:
            out.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, val in values.items():
        if key not in seen:
            out.append(f"{key}={val}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
