"""List and control your Hubspace lights. Run without arguments to list them."""

import argparse
import asyncio
import getpass
import json
import logging
import os
from pathlib import Path
import sys
import warnings

import keyring
from aiohttp import ClientSession, ClientTimeout
from hubspaceng.models.devices.lights.base import BaseLightDevice

from errors import SetupError
from hubspace_connection import HubspaceConnection
from session_cache import SessionCache
import light_color
import light_strength
import light_transport

# Upstream debug logs can contain authentication responses. Keep them private.
hubspace_logger = logging.getLogger("hubspaceng")
hubspace_logger.handlers = [logging.NullHandler()]
hubspace_logger.propagate = False

ROOT = Path(__file__).resolve().parent
KEYCHAIN_SERVICE = "hubspace-lights"


def username():
    configured = {}
    config_path = ROOT / "config.json"
    if config_path.exists():
        configured = json.loads(config_path.read_text())
    value = os.environ.get("HUBSPACE_USERNAME") or configured.get("username")
    if not value:
        raise SetupError("Add your Hubspace email to config.json or set HUBSPACE_USERNAME.")
    return value


def select_light(lights, target):
    matches = [light for light in lights if light.id == target]
    if not matches:
        matches = [light for light in lights if (light.name or "").casefold() == target.casefold()]
    if len(matches) != 1:
        raise SetupError("Use an exact light name or ID from the list. Duplicate names require an ID.")
    return matches[0]


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("list", help="List lights and their current state (the default)")
    commands.add_parser("login", help="Check a password and save it in this computer's credential store")
    commands.add_parser("colors", help="Show editable named color presets (no login needed)")
    for command in ("on", "off", "brightness", "color", "temperature", "set"):
        action = commands.add_parser(command)
        target = action.add_mutually_exclusive_group(required=True)
        if command == "set":
            target.add_argument("--light", help="Exact light name or ID; quote names containing spaces")
        else:
            target.add_argument("light", nargs="?", help="Exact light name or ID; quote names containing spaces")
        target.add_argument("--all", dest="all_lights", action="store_true", help="Apply to every supported light in your account")
        if command in ("brightness", "set"):
            action.add_argument("value", type=light_strength.parse_brightness, help="Brightness from 0 to 100")
            if command == "set":
                action.add_argument("color", nargs="?", type=light_color.parse_color, help="Optional color; also turns the light on")
        elif command == "color":
            action.add_argument("value", type=light_color.parse_color, help="Color name, '#RRGGBB', or R,G,B (0-255 each)")
        elif command == "temperature":
            action.add_argument("value", help="White temperature in kelvin (such as 2700) or a device's named setting")
    return parser.parse_args(argv)


async def show_lights(lights):
    if not lights:
        print("Connected, but this library did not find any supported lights.")
        return
    print(f"Connected. Found {len(lights)} light(s):\n")
    for light in sorted(lights, key=lambda item: (item.name or "").casefold()):
        state = "unknown"
        if light.power is not None:
            state = "on" if await light.is_on() else "off"
        level = ""
        if light.brightness is not None:
            level = f" | brightness: {await light.get_brightness()}"
        print(f"{light.name or '(unnamed)'} | {state}{level}\n  ID: {light.id}")
        print(f"  {light_color.describe_color_support(light)}")


async def run(args, email, password):
    async with ClientSession(timeout=ClientTimeout(total=30)) as session:
        cache = SessionCache(email, ROOT / ".cache")
        connection = HubspaceConnection(email, password, session, cache)
        try:
            await asyncio.wait_for(
                connection.authenticate(force=args.command == "login" or bool(os.environ.get("HUBSPACE_PASSWORD"))),
                timeout=45,
            )
        except Exception as error:
            raise SetupError(
                f"Hubspace sign-in failed ({type(error).__name__}). "
                "Check the password in the Hubspace app. This older library may also need a login update."
            ) from None

        if args.command == "login":
            try:
                keyring.set_password(KEYCHAIN_SERVICE, email, password)
            except keyring.errors.KeyringError:
                raise SetupError("Sign-in succeeded, but the password could not be saved to the credential store.") from None
            print("Sign-in succeeded. Password saved in this computer's credential store.")

        try:
            lights = await asyncio.wait_for(
                connection.load_lights(fresh_state=args.command in (None, "list", "login")), timeout=45
            )
        except Exception as error:
            raise SetupError(
                f"Sign-in succeeded, but loading devices failed ({type(error).__name__}). "
                "The library may not recognize one of your devices."
            ) from None
        if args.command in (None, "list", "login"):
            await show_lights(lights)
            return

        if args.all_lights:
            targets = lights
        else:
            try:
                targets = [select_light(lights, args.light)]
            except SetupError:
                if not connection.used_cached_devices:
                    raise
                # A newly added or renamed light should work immediately.
                lights = await asyncio.wait_for(connection.load_lights(refresh=True), timeout=45)
                targets = [select_light(lights, args.light)]
        if not targets:
            raise SetupError("No supported lights found to control.")
        prepared = [(light, light_transport.build_updates(args, light)) for light in targets]

        # One combined request per light; keep upstream's safe request order.
        for light, updates in prepared:
            await light_transport.apply_updates(light, updates)
            if args.command in ("on", "off"):
                print(f"Sent {args.command} command to {light.name or light.id}.")
            elif args.command == "brightness":
                print(f"Sent brightness {args.value} to {light.name or light.id}.")
            elif args.command == "color":
                print(f"Sent RGB color {args.value} to {light.name or light.id}.")
            elif args.command == "temperature":
                print(f"Sent white temperature {args.value} to {light.name or light.id}.")
            elif args.command == "set":
                detail = f" and RGB color {args.color}" if args.color is not None else ""
                print(f"Set {light.name or light.id} to {args.value}%{detail}, on.")


def main(argv=None):
    args = arguments(argv)
    if args.command == "colors":
        for name, rgb in light_color.COLOR_PRESETS.items():
            print(f"{name}: {','.join(str(channel) for channel in rgb)}")
        print("Edit COLOR_PRESETS in light_color.py to add or change named colors.")
        return 0
    try:
        email = username()
        if args.command == "login":
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                try:
                    password = getpass.getpass("Hubspace password (hidden): ")
                except getpass.GetPassWarning:
                    raise SetupError("For hidden password entry, run python main.py login in a terminal using this project's environment.") from None
        else:
            password = os.environ.get("HUBSPACE_PASSWORD") or keyring.get_password(KEYCHAIN_SERVICE, email)
        if not password:
            raise SetupError("No saved password. Run: python main.py login")
        asyncio.run(run(args, email, password))
        return 0
    except SetupError as error:
        print(f"Error: {error}", file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        print("Cancelled.", file=sys.stderr)
    except Exception as error:
        # Never dump raw cloud responses, request headers, or authentication URLs.
        print(f"Operation failed ({type(error).__name__}). Check your connection and try again.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
