"""Ask the running bot to shut down gracefully (used by stop_copybot.bat)."""

import sys
import time

from src.config import load_config
from src.store import Store


def main() -> int:
    cfg = load_config("config.yaml")
    store = Store(cfg.storage.db_path)
    store.record_event(int(time.time() * 1000), "info", "stop_requested", "stop_copybot.bat")
    print("stop requested")
    return 0


if __name__ == "__main__":
    sys.exit(main())
