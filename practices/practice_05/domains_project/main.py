import sys
import logging
from api import app
from cli import run_cli

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# app is imported for uvicorn / fastapi dev

def main() -> None:
    run_cli()

if __name__ == "__main__":
    main()

