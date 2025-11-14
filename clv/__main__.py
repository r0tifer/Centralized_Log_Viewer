"""Entry point module for running CLV via `python -m clv` or PyInstaller."""

from clv.app import run


def main() -> None:
    run()


if __name__ == "__main__":
    main()
