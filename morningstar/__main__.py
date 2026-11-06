import multiprocessing
import sys


def main() -> int:
    multiprocessing.freeze_support()
    from morningstar.app import run

    return run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
