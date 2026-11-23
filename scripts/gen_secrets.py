"""Fill missing/empty keys in .env from .env.example with generated passwords.

Rules:
- Only ever adds a value where none exists; existing values are never changed.
- Never touches GROQ_API_KEY or GROQ_API_KEY_2 (a human pastes them in).
- Never prints a secret value, only key names.
- Idempotent: a second run changes nothing.

Usage: uv run python scripts/gen_secrets.py [--env .env] [--example .env.example]
"""

import argparse
import secrets
from pathlib import Path

NEVER_GENERATE = {"GROQ_API_KEY", "GROQ_API_KEY_2"}
# Some services insist on a format. Everything else is a URL-safe random token.
HEX64 = {"LANGFUSE_ENCRYPTION_KEY"}  # Langfuse: 32 bytes as 64 hex characters


def new_value(key: str) -> str:
    if key in HEX64:
        return secrets.token_hex(32)
    if key == "LANGFUSE_PUBLIC_KEY":
        return "pk-lf-" + secrets.token_hex(16)
    if key == "LANGFUSE_SECRET_KEY":
        return "sk-lf-" + secrets.token_hex(16)
    if key.startswith("LITELLM_KEY_") or key == "LITELLM_MASTER_KEY":
        return "sk-" + secrets.token_urlsafe(32)  # LiteLLM keys must start with sk-
    return secrets.token_urlsafe(32)


def keys_in(text: str) -> list[str]:
    return [line.split("=", 1)[0].strip() for line in text.splitlines() if "=" in line and not line.lstrip().startswith("#")]


def fill(env_path: Path, example_path: Path) -> list[str]:
    """Return the names of the keys that were generated."""
    wanted = keys_in(example_path.read_text(encoding="utf-8"))
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    present: dict[str, int] = {}
    for i, line in enumerate(lines):
        if "=" in line and not line.lstrip().startswith("#"):
            present[line.split("=", 1)[0].strip()] = i

    generated = []
    for key in wanted:
        if key in NEVER_GENERATE:
            if key not in present:
                lines.append(f"{key}=")  # placeholder for the human; still no value
            continue
        new = f"{key}={new_value(key)}"
        if key not in present:
            lines.append(new)
        elif lines[present[key]].split("=", 1)[1].strip() == "":
            lines[present[key]] = new
        else:
            continue
        generated.append(key)

    new_text = "\n".join(lines) + "\n"
    old_text = env_path.read_text(encoding="utf-8") if env_path.exists() else None
    if old_text is None or new_text.splitlines() != old_text.splitlines():
        env_path.write_text(new_text, encoding="utf-8")
    return generated


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", type=Path, default=Path(".env"))
    ap.add_argument("--example", type=Path, default=Path(".env.example"))
    args = ap.parse_args()
    generated = fill(args.env, args.example)
    print("generated:", ", ".join(generated) if generated else "nothing (all keys already set)")


if __name__ == "__main__":
    main()
