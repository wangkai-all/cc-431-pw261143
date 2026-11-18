"""``python -m app.pairing`` -- start the pairing service."""

import sys

from ..services import main_pairing

if __name__ == "__main__":
    main_pairing(sys.argv[1:])
