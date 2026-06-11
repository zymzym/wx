from gfs_guardian.config import REGIONS


def test_only_china_region_is_enabled():
    assert len(REGIONS) == 1
    assert REGIONS[0].name == "china"
    assert REGIONS[0].bbox == "73,18,135,54"
    assert REGIONS[0].out_dir == "data"
