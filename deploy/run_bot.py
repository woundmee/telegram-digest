"""Launch the bot with project-local environment settings."""

import os
from pathlib import Path
import sys


def main() -> None:
    os.chdir(Path(__file__).resolve().parent.parent)
    os.execv(
        "/bin/bash",
        [
            "bash",
            "-ec",
            'set -a; . ./.env; set +a; exec "$1" -u -m telegram_digest',
            "telegram-digest",
            sys.executable,
        ],
    )


if __name__ == "__main__":
    main()
