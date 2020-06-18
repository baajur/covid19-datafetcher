import sys
import os
from fetcher.lib import main

if __name__ == "__main__":
    print("Version: ", sys.version_info)

    # read command line args, but don't bother too much
    state = sys.argv[1] if len(sys.argv) > 1 else None
    main(state)
