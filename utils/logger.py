import sys
from datetime import datetime


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log_info(msg: str) -> None:
    sys.stderr.write(f"[{_now()}] [INFO] {msg}\n")
    sys.stderr.flush()


def log_error(msg: str) -> None:
    sys.stderr.write(f"[{_now()}] [ERROR] {msg}\n")
    sys.stderr.flush()


def log_debug(msg: str) -> None:
    # אם תרצה כיבוי/הדלקת DEBUG – אפשר לשים פה flag
    sys.stderr.write(f"[{_now()}] [DEBUG] {msg}\n")
    sys.stderr.flush()
