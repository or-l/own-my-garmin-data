import pathlib
import sys

# Tests import repo-root modules (workout_gen, planning, domains, ...) the
# same way the scripts do: by putting the repo root on sys.path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
