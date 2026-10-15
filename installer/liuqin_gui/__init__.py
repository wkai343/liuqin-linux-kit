"""Desktop installer. Importing this package never accesses a tablet."""
from pathlib import Path
import sys

# Shared package interfaces also ship in the standalone installer archive.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'shared'))
