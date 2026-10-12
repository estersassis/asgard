"""Console entry point."""

import argparse


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="asgard",
        description=(
            "Generate evidence-based SECURITY.md files from GitHub pull request discussions."
        ),
    )
    parser.parse_args(argv)


if __name__ == "__main__":
    main()
