"""Short terminal commands: light 100 red, light 40, and light off."""

import argparse
import sys

import main as controller
from light_color import parse_color
from light_strength import parse_brightness


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0].casefold() == "league":
        import league_service
        return league_service.main(argv[1:])
    parser = argparse.ArgumentParser(
        prog="light",
        description="Control all your Hubspace lights, or select one with --light.",
        epilog="Examples: light 100 red | light 40 | light off | light league start | light league stop",
    )
    parser.add_argument("action", nargs="?", default="list", help="Brightness 0–100, on, off, list, colors, login, or league")
    parser.add_argument("color", nargs="?", help="Optional color name, '#RRGGBB', or R,G,B")
    parser.add_argument("--light", metavar="NAME", help="One exact light name or ID; defaults to all lights")
    args = parser.parse_args(argv)
    action = args.action.casefold()
    target = [args.light] if args.light is not None else ["--all"]

    if action in ("list", "colors", "login"):
        if args.color is not None or args.light is not None:
            parser.error(f"'{action}' does not take a color or --light.")
        return controller.main([action])
    if action in ("on", "off"):
        if args.color is not None:
            parser.error("To set a color, provide brightness too: light 100 red")
        return controller.main([action, *target])

    try:
        brightness = parse_brightness(action)
        if args.color is not None:
            parse_color(args.color)  # Reject mistakes before accessing credentials.
    except argparse.ArgumentTypeError as error:
        parser.error(str(error))
    if brightness == 0:
        if args.color is not None:
            parser.error("Use 'light 0' without a color to turn lights off.")
        return controller.main(["off", *target])

    set_target = ["--light", args.light] if args.light is not None else ["--all"]
    command = ["set", *set_target, str(brightness)]
    if args.color is not None:
        command.append(args.color)
    return controller.main(command)


if __name__ == "__main__":
    sys.exit(main())
