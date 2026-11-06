"""Entry point (used by PyInstaller and `python main.py`)."""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from morningstar.app import run

    sys.exit(run(sys.argv))
