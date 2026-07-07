from __future__ import annotations

import sys

import pytest

from battfeed.sources.wmi_battery import WmiBatterySource


def test_missing_wmi_dependency_raises_helpful_importerror(monkeypatch):
    monkeypatch.setitem(sys.modules, "wmi", None)  # force `import wmi` to fail
    with pytest.raises(ImportError, match=r"battfeed\[wmi\]"):
        WmiBatterySource()


def test_availability_reports_non_windows_platforms(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert WmiBatterySource.availability() == "requires Windows"
