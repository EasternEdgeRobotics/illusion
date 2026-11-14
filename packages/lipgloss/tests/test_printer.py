"""lipgloss/printer.py against niimprint's real client, with the serial port faked.

FakeNiimbot stands in for SerialTransport and answers each request with an
encoded reply, so niimprint's own packet and field parsing runs. The reply
layouts are ones the fork parses, not captures off a D110.
"""

import asyncio
import struct
from types import SimpleNamespace

import niimprint.printer
import pytest
import serial
from niimprint.packet import NiimbotPacket
from niimprint.printer import BaseTransport, RequestCodeEnum

from lipgloss import printer
from lipgloss.print_queue import PrintQueue

IMAGE_ROW = 0x85

# Replies come back on the request code plus this, and plus 1 for anything not listed
REPLY_OFFSETS = {
    RequestCodeEnum.SET_LABEL_DENSITY: 16,
    RequestCodeEnum.SET_LABEL_TYPE: 16,
}


def heartbeat(closed=True, battery=4):
    """The 13 byte heartbeat, one of the layouts niimprint knows."""
    data = bytearray(13)
    data[9] = 1 if closed else 0
    data[10] = battery
    return bytes(data)


def rfid(barcode="02282280", total=50, used=0):
    serial_number = b"PZ1G3122"
    return (
        b"\x01" + bytes(7)
        + bytes([len(barcode)]) + barcode.encode()
        + bytes([len(serial_number)]) + serial_number
        + struct.pack(">HHB", total, used, 1)
    )


NO_ROLL = b"\x00"


class FakeNiimbot(BaseTransport):
    def __init__(self):
        self.heartbeat = heartbeat()
        self.rfid = rfid()
        # Never answers, as the printer does when it has gone to sleep
        self.asleep = False
        # Raise on this image row, as if the cable were pulled mid print
        self.unplug_at_row = None
        self.rows = 0
        self.requests = []
        self._replies = bytearray()
        # lipgloss closes the port by reaching for this attribute
        self._serial = SimpleNamespace(closed=False)
        self._serial.close = lambda: setattr(self._serial, "closed", True)

    def write(self, data):
        packet = NiimbotPacket.from_bytes(data)

        if packet.type == IMAGE_ROW:
            if self.rows == self.unplug_at_row:
                raise serial.SerialException("write failed: [Errno 6] Device not configured")

            self.rows += 1
            return

        self.requests.append(packet.type)

        if self.asleep:
            return

        reply = {
            RequestCodeEnum.HEARTBEAT: self.heartbeat,
            RequestCodeEnum.GET_RFID: self.rfid,
        }.get(packet.type, b"\x01")

        reply_code = packet.type + REPLY_OFFSETS.get(packet.type, 1)
        self._replies += NiimbotPacket(reply_code, reply).to_bytes()

    def read(self, length):
        data = bytes(self._replies[:length])
        del self._replies[:length]
        return data


@pytest.fixture
def niimbot(monkeypatch):
    fake = FakeNiimbot()
    monkeypatch.setattr(printer, "SerialTransport", lambda port: fake)
    # niimprint polls with sleeps, which only slow a fake down
    monkeypatch.setattr(niimprint.printer, "time", SimpleNamespace(sleep=lambda seconds: None))
    return fake


def test_missing_port_is_disconnected(tmp_path):
    # The real SerialTransport, so this is pyserial's own message being matched
    with pytest.raises(printer.PrinterUnavailable, match="disconnected"):
        printer.check_printer(str(tmp_path / "niimbot"))


def test_no_answer_is_asleep(niimbot):
    niimbot.asleep = True

    with pytest.raises(printer.PrinterUnavailable, match="asleep"):
        printer.check_printer("/dev/niimbot")


@pytest.mark.parametrize("state, reason", [
    ({"heartbeat": heartbeat(closed=False)}, "seems to be open"),
    ({"rfid": NO_ROLL}, "load a roll"),
    ({"rfid": rfid(total=50, used=50)}, "no labels left"),
])
def test_printer_not_ready(niimbot, state, reason):
    for name, value in state.items():
        setattr(niimbot, name, value)

    with pytest.raises(printer.PrinterUnavailable, match=reason):
        printer.check_printer("/dev/niimbot")

    assert niimbot._serial.closed


def test_known_roll_overrides_the_printers_count(niimbot):
    # The printer claims 50 on a 155 label roll, and 100 are already used
    niimbot.rfid = rfid(barcode="041025002", total=50, used=100)

    assert printer.media_remaining(printer.check_printer("/dev/niimbot")) == 55


def test_printer_info(niimbot):
    niimbot.rfid = rfid(total=50, used=5)
    niimbot.heartbeat = heartbeat(battery=3)

    assert printer.niimbot_printer_info("/dev/niimbot") == "Labels left: 45/50\nBattery Level: 3/4"
    assert niimbot._serial.closed


def test_printer_info_without_a_roll(niimbot):
    niimbot.rfid = NO_ROLL

    assert printer.niimbot_printer_info("/dev/niimbot") == "Unable to get printer info, labels might not be loaded."


def test_print_sends_every_row(niimbot, make_image):
    printer.niimbot_print(make_image(width=96, height=320), "/dev/niimbot", "d110")

    assert niimbot.rows == 320
    assert niimbot.requests[-1] == RequestCodeEnum.END_PRINT
    assert niimbot._serial.closed


def test_unplugged_mid_print_is_a_printer_fault(niimbot, make_image):
    niimbot.unplug_at_row = 100

    with pytest.raises(printer.PrinterUnavailable, match="Device not configured"):
        printer.niimbot_print(make_image(), "/dev/niimbot", "d110")

    assert niimbot._serial.closed


@pytest.mark.anyio
async def test_queue_pauses_on_a_sleeping_printer_and_resumes(niimbot, make_image):
    queue = PrintQueue("/dev/niimbot", "d110", delay=0, log=lambda message: None)
    queue.start()

    try:
        niimbot.asleep = True
        queue.add(make_image(), "Tape")

        async with asyncio.timeout(2):
            while not queue.paused:
                await asyncio.sleep(0.01)

        assert queue.pause_reason == "printer is likely asleep"

        niimbot.asleep = False
        assert (await queue.resume()).startswith("Print queue resumed, 1 label(s) to print, 50 left")

        async with asyncio.timeout(2):
            while queue.pending_jobs:
                await asyncio.sleep(0.01)

        assert niimbot.rows == 320
    finally:
        await queue.stop()
