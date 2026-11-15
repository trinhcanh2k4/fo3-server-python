"""Persistent server setting for duplicate-player lineups."""
import json
import os
from pathlib import Path
import tempfile
import threading

PATH = Path(__file__).resolve().parent / 'gamedata' / 'duplicate_player_settings.json'
LOCK = threading.RLock()


def get_allowed():
    """Duplicate lineups remain allowed by default to preserve existing behavior."""
    with LOCK:
        try:
            value = json.loads(PATH.read_text(encoding='utf-8')).get('allow_duplicates')
            return value if type(value) is bool else True
        except (OSError, ValueError, TypeError, AttributeError):
            return True


def set_allowed(value):
    if type(value) is not bool:
        raise ValueError('allow_duplicates must be true or false')
    with LOCK:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=PATH.parent,
                                             prefix='duplicate_players_', suffix='.tmp', delete=False) as f:
                name = f.name
                json.dump({'allow_duplicates': value}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, PATH)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)
    return value
