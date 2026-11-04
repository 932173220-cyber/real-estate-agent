"""Real-estate desktop Agent and its source-bundled dependencies."""
from pathlib import Path
import sys

# ``python -m real_estate_desktop.eval.run`` bypasses the desktop launcher.
# Make the bundled memory component available to that entry point as well.
_vendor_dir = Path(__file__).resolve().parent.parent / 'vendor'
if _vendor_dir.is_dir() and str(_vendor_dir) not in sys.path:
    sys.path.insert(0, str(_vendor_dir))
