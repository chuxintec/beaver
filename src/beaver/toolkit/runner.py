"""`beaver tool <id> [args]`: run one received GREA tool with its original exit codes."""
import runpy
import sys
import traceback
from beaver.toolkit import TOOLS


def main(argv):
    if not argv or argv[0] in ('-h', '--help'):
        print('usage: beaver tool <id> [args...]\n\ntools: ' + ', '.join(TOOLS))
        return 0 if argv else 2
    tool, rest = argv[0], argv[1:]
    if tool not in TOOLS:
        print('unknown tool: ' + tool, file=sys.stderr)
        return 2
    saved = sys.argv
    sys.argv = [tool, *rest]
    try:
        runpy.run_module('beaver.toolkit.' + tool, run_name='__main__')
    except SystemExit as exit:
        code = exit.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    except Exception:
        # Exit 1 is reserved for intentional findings; crashes are 2, as in GREA.
        traceback.print_exc()
        return 2
    finally:
        sys.argv = saved
    return 0
