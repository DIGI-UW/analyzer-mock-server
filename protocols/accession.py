import itertools
import re
from typing import Dict


SITE_YEAR_NUM_PREFIX = "DEV01"
MAX_SAMPLE_ID_LEN = 20
SITE_YEAR_NUM_RE = re.compile(r"^DEV01\d{15}$")
LANE_CODE_RE = re.compile(r"^\d{2}$")


def validate_accession(sample_id: str, context: str) -> str:
    if not isinstance(sample_id, str) or not SITE_YEAR_NUM_RE.fullmatch(sample_id):
        raise ValueError(
            f"{context} must be a valid SiteYearNum accession "
            f"({SITE_YEAR_NUM_PREFIX} + 15 digits, total {MAX_SAMPLE_ID_LEN} chars); got: {sample_id!r}"
        )
    return sample_id


def validate_lane_code(lane_code: str, context: str) -> str:
    if not isinstance(lane_code, str) or not LANE_CODE_RE.fullmatch(lane_code):
        raise ValueError(
            f"{context} must be a 2-digit lane code used to mint SiteYearNum accessions; got: {lane_code!r}"
        )
    return lane_code


def next_site_year_num(counter_map: Dict[str, itertools.count], lane_code: str, context: str) -> str:
    lane = validate_lane_code(lane_code, context)
    if lane not in counter_map:
        counter_map[lane] = itertools.count(1)
    seq = next(counter_map[lane])
    sample_id = f"DEV0126{lane}{seq:011d}"
    return validate_accession(sample_id, f"{context} generated accession")
