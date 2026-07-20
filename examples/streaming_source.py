"""A push-style source built on StreamingSource, developed with a replay tape.

Real streaming hardware (a BLE shunt, a CAN bus, an MQTT feed) *pushes* readings
at you rather than answering a poll(). ``StreamingSource`` adapts that to
battfeed's synchronous poll() seam: a subclass supplies a blocking
``run_reader(emit, should_stop)`` loop, the base runs it on a background thread,
and readings buffer until poll() drains them.

This example needs no hardware and no network. It builds an in-memory replay
*tape* of synthetic "instant readout" frames and drives the source's reader from
it with ``ReplayReader`` -- exactly the replay-first workflow battfeed uses to
develop and test streaming sources on a laptop. Run it directly:

    python examples/streaming_source.py
"""

import struct

from battfeed import BdfCsvSink, Harvester, StreamingSource
from battfeed.testing import ReplayReader, ReplayTape, check_source


def make_tape() -> ReplayTape:
    """A tape of fake frames: little-endian int32 millivolts, then int32 milliamps.

    A real tape is recorded once from live hardware with ``TapeRecorder`` and
    committed as a test fixture; here we synthesize one so the example is
    self-contained.
    """
    tape = ReplayTape()
    for i in range(10):
        millivolts = 3600 + i * 5  # a gently rising voltage
        milliamps = 250  # positive current -> charging (the BDF sign convention)
        tape.append(t=float(i), data=struct.pack("<ii", millivolts, milliamps))
    return tape


class ReplayedShuntSource(StreamingSource):
    """A stand-in for a BLE battery shunt, driven from a replay tape.

    One shunt is one physical object, so there are no routing keys and no
    source-supplied timebase: the harvester stamps each sample with the elapsed
    collection time, which is the correct pattern for a single-object stream.
    """

    def __init__(self, tape: ReplayTape) -> None:
        super().__init__("replayed-shunt")
        self._tape = tape

    def run_reader(self, emit, should_stop):
        # ReplayReader compresses time by default: the whole tape is delivered as
        # fast as emit() accepts it, and it stops promptly when should_stop() is
        # set. A live subclass would open the BLE scanner here instead.
        ReplayReader(self._tape).run(lambda frame: emit(self._decode(frame)), should_stop)

    @staticmethod
    def _decode(frame):
        millivolts, milliamps = struct.unpack("<ii", frame.data)
        return {
            "voltage_volt": millivolts / 1000.0,
            "current_ampere": milliamps / 1000.0,
        }


if __name__ == "__main__":
    tape = make_tape()

    # 1. Contract-check the source with no hardware -- what a test suite would do.
    check_source(ReplayedShuntSource(tape))
    print("check_source: OK")

    # 2. Collect from it through a Harvester, exactly like any other source.
    source = ReplayedShuntSource(tape)
    harvester = Harvester()
    harvester.register(source)
    sink = BdfCsvSink("LOCAL__ReplayedShunt__20260720_001.bdf.csv")
    stats = harvester.collect("replayed-shunt", duration_s=1, interval_s=0.2, sink=sink)
    sink.close()
    source.close()
    print(f"Wrote {stats.samples} samples to {sink.path}; columns: {stats.columns}")
