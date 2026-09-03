from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class PlaceholderPanel(QWidget):
    def __init__(self, name: str, note: str = "") -> None:
        super().__init__()
        label = QLabel(f"{name}\n\n{note or 'Not implemented yet.'}")
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("color: gray; font-size: 14pt")
        layout = QVBoxLayout(self)
        layout.addWidget(label)
