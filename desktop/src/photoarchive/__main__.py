import argparse
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow


def main() -> int:
    parser = argparse.ArgumentParser(prog="photoarchive")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Open the window, close it after 1s, and exit 0. Used by smoke checks.",
    )
    args = parser.parse_args()

    app = QApplication(sys.argv)
    window = QMainWindow()
    window.setWindowTitle("Photo Archive")
    window.resize(800, 600)
    window.setCentralWidget(QLabel("Photo Archive"))
    window.show()

    if args.smoke:
        QTimer.singleShot(1000, app.quit)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
