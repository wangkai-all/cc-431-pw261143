"""``python -m app.observations`` -- start the observation service."""

import sys

from ..services import main_observations

if __name__ == "__main__":
    main_observations(sys.argv[1:])
