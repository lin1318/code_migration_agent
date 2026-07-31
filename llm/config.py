"""Load local model configuration without overriding process environment."""

from pathlib import Path

from dotenv import load_dotenv

PROJECT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def load_project_environment() -> None:
    load_dotenv(dotenv_path=PROJECT_ENV_FILE, override=False)
