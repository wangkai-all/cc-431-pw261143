"""``python -m app.stats`` -- start the report service."""

import sys

from ..services import main_reports

if __name__ == "__main__":
    main_reports(sys.argv[1:])
