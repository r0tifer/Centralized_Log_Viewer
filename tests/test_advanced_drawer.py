import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from clv.widgets.advanced_drawer import AdvancedFiltersDrawer


def test_advanced_drawer_hidden_by_default() -> None:
    drawer = AdvancedFiltersDrawer()

    assert "-hidden" in drawer.classes
    assert not drawer.visible
