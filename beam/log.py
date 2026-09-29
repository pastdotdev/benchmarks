"""One line per event, with the time and, when there is one, the conversation it is about."""

from datetime import datetime


def log(message: str, conversation: str | None = None) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    where = f" [conversation {conversation}]" if conversation else ""
    print(f"{now}{where} {message}", flush=True)


def duration(seconds: float) -> str:
    """Seconds as "42s", "3m05s" or "1h02m"."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
