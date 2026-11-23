"""L0: secrets hygiene. Runs against real files and real git, nothing mocked."""

import subprocess
from pathlib import Path

from scripts.gen_secrets import fill

ROOT = Path(__file__).resolve().parents[2]


def test_env_is_git_ignored() -> None:
    out = subprocess.run(["git", "check-ignore", ".env"], cwd=ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ".env"


def test_env_example_is_tracked_and_has_no_values() -> None:
    ignored = subprocess.run(["git", "check-ignore", "-q", ".env.example"], cwd=ROOT)
    assert ignored.returncode == 1, ".env.example must NOT be ignored"
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            assert value.strip() == "", f"value committed in .env.example: {key}"


def test_gen_secrets_fills_preserves_and_is_idempotent(tmp_path: Path) -> None:
    example = tmp_path / ".env.example"
    example.write_text("GROQ_API_KEY=\nA_PASSWORD=\nB_PASSWORD=\n", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("GROQ_API_KEY=human-pasted\nB_PASSWORD=keep-me\n", encoding="utf-8")

    assert fill(env, example) == ["A_PASSWORD"]
    first = env.read_text(encoding="utf-8")
    assert "GROQ_API_KEY=human-pasted" in first
    assert "B_PASSWORD=keep-me" in first
    a_line = next(ln for ln in first.splitlines() if ln.startswith("A_PASSWORD="))
    a_value = a_line.split("=", 1)[1]
    assert len(a_value) >= 32

    assert fill(env, example) == []
    assert env.read_text(encoding="utf-8") == first


def test_gen_secrets_never_generates_groq_key(tmp_path: Path) -> None:
    example = tmp_path / ".env.example"
    example.write_text("GROQ_API_KEY=\n", encoding="utf-8")
    env = tmp_path / ".env"
    assert fill(env, example) == []
    assert env.read_text(encoding="utf-8") == "GROQ_API_KEY=\n"
