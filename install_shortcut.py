"""Install `light` in this project's virtual environment without changing your shell settings."""

from pathlib import Path
import shlex


def install():
    project = Path(__file__).resolve().parent
    python = project / ".venv" / "bin" / "python"
    if not python.is_file():
        raise SystemExit("Create this project's .venv and install requirements.txt first.")
    command = project / ".venv" / "bin" / "light"
    # Run the source file directly so edits work immediately, regardless of
    # editable-package import hooks or Finder hidden-file metadata.
    launcher = (
        "#!/bin/sh\n"
        f"exec {shlex.quote(str(python))} "
        f"{shlex.quote(str(project / 'light_cli.py'))} \"$@\"\n"
    )
    command.write_text(launcher)
    command.chmod(0o755)
    print("Installed light. Activate .venv, then run: light --help")


if __name__ == "__main__":
    install()
