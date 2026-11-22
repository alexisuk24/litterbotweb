"""pylitterbot's warning about enum values it doesn't know appears once per value, not every sync."""
import logging

from pylitterbot.enums import HopperStatus
from pylitterbot.utils import to_enum

import lr_clips


def test_each_unknown_enum_value_is_logged_once(caplog):
    root = logging.getLogger()
    f = lr_clips.UnknownEnumOnce()
    root.addFilter(f)
    try:
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                to_enum("NOT_A_STATUS", HopperStatus)
            to_enum("SOMETHING_NEW", HopperStatus)
            logging.warning("an unrelated warning")
            logging.warning("an unrelated warning")
    finally:
        root.removeFilter(f)
    messages = [r.getMessage() for r in caplog.records]
    assert messages == ["Value 'NOT_A_STATUS' not found in enum HopperStatus",
                        "Value 'SOMETHING_NEW' not found in enum HopperStatus",
                        "an unrelated warning", "an unrelated warning"]
