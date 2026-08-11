from pathlib import Path


def test_no_runtime_v4_package_is_introduced():
    root = Path(__file__).resolve().parents[1] / "src" / "agent"
    assert not (root / "runtime_v4").exists()
