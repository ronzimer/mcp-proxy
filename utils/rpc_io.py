import json
import sys
from typing import Any, Dict


def read_json_message() -> Dict[str, Any] | None:
    """
    Read a single line from stdin, parse JSON.
    Returns None on EOF.
    """
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        # אם מגיעה שורה לא תקינה – מתעלמים
        return None


def write_json_message(message: Dict[str, Any]) -> None:
    """
    Write a JSON message as a single line to stdout.
    """
    serialized = json.dumps(message, ensure_ascii=False)
    sys.stdout.write(serialized + "\n")
    sys.stdout.flush()
