"""Interactive first-time setup; only the email is written to config.json."""

import json
import os
import sys

import main as controller


def main():
    path = controller.ROOT / "config.json"
    try:
        configured = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(configured, dict):
            raise ValueError("Expected an object")
        default = configured.get("username", "")
        prompt = f"Hubspace email [{default}]: " if default else "Hubspace email: "
        email = input(prompt).strip() or default
        if not isinstance(email, str) or "@" not in email or any(c.isspace() for c in email):
            print("Enter the email you use in the Hubspace phone app.", file=sys.stderr)
            return 1
        configured["username"] = email
        path.write_text(json.dumps(configured, indent=2) + "\n", encoding="utf-8")
        print("Your password will be entered invisibly and saved in Windows Credential Manager.")
        # The explicit setup choice takes precedence over inherited variables
        # for this setup process. No system environment settings are changed.
        os.environ["HUBSPACE_USERNAME"] = email
        return controller.main(["login"])
    except (EOFError, KeyboardInterrupt):
        print("Setup cancelled.")
        return 1
    except Exception as error:
        print(f"Setup failed ({type(error).__name__}). Check config.json and try again.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
