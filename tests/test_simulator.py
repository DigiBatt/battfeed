from __future__ import annotations

from battfeed.sources.simulator import SimulatedCellSource


def test_simulator_is_deterministic():
    a = SimulatedCellSource(steps_to_empty=100)
    b = SimulatedCellSource(steps_to_empty=100)
    stream_a = [a.poll()[0] for _ in range(5)]
    stream_b = [b.poll()[0] for _ in range(5)]
    assert stream_a == stream_b


def test_simulator_discharge_profile_and_sign_convention():
    source = SimulatedCellSource(steps_to_empty=50)
    samples = [source.sample_at(step) for step in range(51)]

    voltages = [sample["voltage_volt"] for sample in samples]
    assert voltages[0] == 3.0
    assert voltages[-1] == 2.0
    assert all(later <= earlier for earlier, later in zip(voltages, voltages[1:]))
    # Curvature: the first half of the discharge drops less than the second half.
    assert (voltages[0] - voltages[25]) < (voltages[25] - voltages[50])

    # BDF sign convention: discharge current is negative.
    assert all(sample["current_ampere"] == -0.002 for sample in samples)
    assert all(24.0 < sample["surface_temperature_celsius"] < 26.0 for sample in samples)

    # No wall-clock timebase: the harvester stamps test_time_second.
    assert "test_time_second" not in samples[0]
    assert source.metadata()["source"] == "simulator"
