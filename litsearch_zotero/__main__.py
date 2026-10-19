"""Show the available Zotero extension commands without accessing Zotero."""

from __future__ import annotations

import argparse
import importlib
import sys


COMMANDS = {"attach-pdfs": "attach_pdfs", "audit-missing-pdfs": "audit_missing_pdfs",
            "dynamic-citations": "dynamic_citations", "word-plugin": "word_plugin"}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in COMMANDS:
        name = COMMANDS[argv[0]]
        module = importlib.import_module("litsearch_zotero." + name)
        if name in {"dynamic_citations", "word_plugin"}:
            return module.main(argv[1:])
        previous = sys.argv
        try:
            sys.argv = [module.__file__, *argv[1:]]
            return module.main() or 0
        finally:
            sys.argv = previous
    parser = argparse.ArgumentParser(description="Local-only Zotero extension utilities.")
    parser.add_argument("command", nargs="?", choices=tuple(COMMANDS))
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
