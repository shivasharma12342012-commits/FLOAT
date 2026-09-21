#!/usr/bin/env python3
"""Float — start here.

    python main.py                 start Float, open the browser
    python main.py --no-browser    start without opening a tab
    python main.py --port 9000     use a different port
    python main.py set-password you@school.edu.in   reset someone's password from the terminal
    python main.py make-admin you@school.edu.in     promote an existing account
    python main.py --version

Everything Float needs lives next to this file once it has run once:
a `data/` folder holding `float.sqlite3` (your accounts, classes and chat
history) and `data/files` (documents Float has made for you, until you save
them to your Desktop or Drive). Delete `data/` to start over completely.
"""

from __future__ import annotations

import argparse
import sys
import time
import webbrowser

from float import APP_NAME, APP_TAGLINE, VERSION
from float import config, db, security, server


def _cmd_run(args: argparse.Namespace) -> None:
    host = args.host or config.settings.host
    port = args.port or config.settings.port
    srv = server.serve(host, port)
    url = f"http://{host}:{srv.server_port}"

    print(f"{APP_NAME} {VERSION} — {APP_TAGLINE}")
    print(f"Running at {url}")
    print("Close this window, or press Ctrl+C, to sign everyone out and stop Float.")

    if not args.no_browser:
        time.sleep(0.35)
        webbrowser.open(url)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nStopping Float.")


def _cmd_set_password(args: argparse.Namespace) -> None:
    config.ensure_dirs()
    db.init()
    user = db.get_user_by_email(args.email.strip().lower())
    if user is None:
        print(f"No account for {args.email}.", file=sys.stderr)
        raise SystemExit(1)
    password = args.password or security.new_token(10)
    problems = security.password_problems(password)
    if problems:
        print(" ".join(problems), file=sys.stderr)
        raise SystemExit(1)
    db.set_password(int(user["id"]), password)
    db.update_user(int(user["id"]), must_change=1)
    db.end_all_sessions(int(user["id"]))
    print(f"New password for {args.email}: {password}")
    print("They'll be asked to set their own on next sign-in.")


def _cmd_make_admin(args: argparse.Namespace) -> None:
    config.ensure_dirs()
    db.init()
    user = db.get_user_by_email(args.email.strip().lower())
    if user is None:
        print(f"No account for {args.email}.", file=sys.stderr)
        raise SystemExit(1)
    db.update_user(int(user["id"]), role="admin")
    print(f"{args.email} is now an admin.")


_KNOWN_COMMANDS = {"run", "set-password", "make-admin"}


def main() -> None:
    parser = argparse.ArgumentParser(prog="float", description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="Start Float (the default).")
    run_p.add_argument("--host", default=None)
    run_p.add_argument("--port", type=int, default=None)
    run_p.add_argument("--no-browser", action="store_true")
    run_p.set_defaults(func=_cmd_run)

    pass_p = sub.add_parser("set-password", help="Reset a teacher's password from the terminal.")
    pass_p.add_argument("email")
    pass_p.add_argument("--password", default=None, help="Omit to generate one.")
    pass_p.set_defaults(func=_cmd_set_password)

    admin_p = sub.add_parser("make-admin", help="Promote an existing account to admin.")
    admin_p.add_argument("email")
    admin_p.set_defaults(func=_cmd_make_admin)

    # `python main.py [--host ...] [--port ...] [--no-browser]` with no
    # subcommand named should just start Float — that's the common case a
    # teacher or admin actually types.
    raw = sys.argv[1:]
    if not raw or raw[0] not in _KNOWN_COMMANDS:
        args = run_p.parse_args(raw)
        args.func = _cmd_run
    else:
        args = parser.parse_args(raw)

    args.func(args)


if __name__ == "__main__":
    main()
