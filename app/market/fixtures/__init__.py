from pathlib import Path


def bundled_fixture_path(name: str = "deterministic_session.json") -> Path:
    path = Path(__file__).with_name(name)
    if not path.is_file():
        raise FileNotFoundError(f"unknown bundled market fixture: {name}")
    return path


__all__ = ["bundled_fixture_path"]
