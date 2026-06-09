"""Command-line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from odytor import __version__, commands
from odytor.config import DEFAULT_OUTPUT_DIR
from odytor.errors import OdytorError
from odytor.gh_client import GitHubClient
from odytor.models import CommentWindow, Target
from odytor.output import save_output
from odytor.progress import Progress

EXAMPLES = """examples:
  tools/odytor/bin/odytor --print --pr 3128
  tools/odytor/bin/odytor --review --pr 3128
  tools/odytor/bin/odytor --labels
  tools/odytor/bin/odytor --print --issue 2523 --repo owner/name --save

This repo-local first slice only reads GitHub data through gh.
It does not change GitHub state. See tools/odytor/GUIDE.md.
"""

TARGET_ACTIONS = ("print", "review")
DEFERRED_COMMANDS = ("audit", "approve", "approve-merge")
DEFERRED_FLAGS = ("--followups", "--stale", "--milestone")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="odytor",
        description="Read-only Odysseus reviewer helper: export GitHub items, "
        "prepare review context, and list labels.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("-p", "--print", action="store_true", dest="print_target",
                         help="Print the selected target (--pr/--issue/--discussion).")
    actions.add_argument("--review", action="store_true",
                         help="Print a local review-prep summary, not a verdict.")
    actions.add_argument("--labels", action="store_true",
                         help="List repository labels (read-only).")

    targets = parser.add_mutually_exclusive_group()
    targets.add_argument("--pr", type=positive_number, metavar="NUMBER")
    targets.add_argument("--issue", type=positive_number, metavar="NUMBER")
    targets.add_argument("--discussion", type=positive_number, metavar="NUMBER")

    parser.add_argument("--repo", metavar="OWNER/NAME",
                        help="Override the GitHub repo detected from the current git clone.")
    parser.add_argument("--comments", type=positive_int, metavar="N",
                        help="Limit conversation comments for --print.")
    parser.add_argument("--order", choices=("latest", "oldest"), default="oldest",
                        help="Conversation comment order for --print.")
    parser.add_argument("--save", action="store_true",
                        help="Save report output to a timestamped text file.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                        metavar="PATH", help="Directory used by --save (default: system temp dir).")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Suppress progress/status output on stderr.")
    return parser


def positive_number(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("target number must be positive")
    return number


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def selected_action(args: argparse.Namespace) -> str:
    if args.print_target:
        return "print"
    if args.review:
        return "review"
    if args.labels:
        return "labels"
    raise ValueError("no action selected")


def selected_target(args: argparse.Namespace) -> Target | None:
    for kind in ("pr", "issue", "discussion"):
        number = getattr(args, kind)
        if number is not None:
            return Target(kind=kind, number=number)
    return None


def validate_selection(
    parser: argparse.ArgumentParser,
    action: str,
    args: argparse.Namespace,
    raw_args: Sequence[str] = (),
) -> Target | None:
    """Resolve and validate the target before any network or dependency work."""
    if action != "print":
        if _option_supplied(raw_args, "--comments"):
            parser.error("--comments is only valid with --print")
        if _option_supplied(raw_args, "--order"):
            parser.error("--order is only valid with --print")

    target = selected_target(args)
    if action in TARGET_ACTIONS and target is None:
        parser.error(f"--{action} needs a target: --pr, --issue, or --discussion")
    if action not in TARGET_ACTIONS and target is not None:
        parser.error(f"--{action} does not take a target (--pr/--issue/--discussion)")
    return target


def _option_supplied(raw_args: Sequence[str], option: str) -> bool:
    return any(value == option or value.startswith(f"{option}=") for value in raw_args)


def _normalize_repo_argument(raw_args: Sequence[str]) -> list[str]:
    normalized = list(raw_args)
    for index, value in enumerate(normalized[:-1]):
        repo_value = normalized[index + 1]
        if value == "--repo" and repo_value.startswith("-") and "/" in repo_value:
            normalized[index:index + 2] = [f"--repo={repo_value}"]
            break
    return normalized


def validate_output_dir(parser: argparse.ArgumentParser, args: argparse.Namespace) -> Path:
    output_dir = args.output_dir.expanduser()
    if args.save and output_dir.exists() and not output_dir.is_dir():
        parser.error(f"--output-dir is not a directory: {output_dir}")
    return output_dir


def dispatch(
    action: str,
    client: GitHubClient,
    target: Target | None,
    progress: Progress,
    window: CommentWindow,
) -> tuple[str, str]:
    if action == "print":
        return commands.run_print(client, target, window, progress)
    if action == "review":
        return commands.run_review(client, target, progress)
    if action == "labels":
        return commands.run_labels(client, progress)
    raise OdytorError(f"Unsupported action: {action}")


def main(argv: Sequence[str] | None = None) -> int:
    raw = _normalize_repo_argument(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if raw and raw[0] in DEFERRED_COMMANDS:
        parser.error(f"'{raw[0]}' is not included in this read-only release")
    deferred_flag = next((flag for flag in DEFERRED_FLAGS if flag in raw), None)
    if deferred_flag:
        parser.error(f"'{deferred_flag}' is deferred and not included in this release")
    args = parser.parse_args(raw)
    action = selected_action(args)
    target = validate_selection(parser, action, args, raw)
    output_dir = validate_output_dir(parser, args)

    progress = Progress(enabled=not args.quiet)
    window = CommentWindow(limit=args.comments, order=args.order)

    try:
        client = commands.make_client(args.repo, progress)
        content, slug = dispatch(action, client, target, progress, window)
        print(content, end="")
        if args.save:
            progress.step("Saving output...")
            path = save_output(output_dir, slug, content)
            if not args.quiet:
                print(f"Saved to: {path}", file=sys.stderr)
        progress.done()
    except OdytorError as error:
        print(f"odytor: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"odytor: could not write output: {error}", file=sys.stderr)
        return 1
    return 0
