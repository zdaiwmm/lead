from pathlib import Path

import pytest
from lead_app.creative_worker import font_path


def test_linux_font_inventory_without_dejavu(monkeypatch):
    latin = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
    cjk = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
    monkeypatch.delenv("LEAD_CREATIVE_FONT", raising=False)
    monkeypatch.setattr(Path, "is_file", lambda path: str(path) in {latin, cjk})
    assert font_path("Chamber") == latin
    assert font_path("Chamber 中文") == cjk
    monkeypatch.setattr(Path, "is_file", lambda path: str(path) == cjk)
    assert font_path("Chamber") == cjk
    monkeypatch.setattr(Path, "is_file", lambda path: str(path) == latin)
    with pytest.raises(ValueError, match="CREATIVE_CJK_FONT_REQUIRED"):
        font_path("中文")
