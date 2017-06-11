"""LISY platform for System 1 and System 80."""
import asyncio
from typing import Generator

from mpf.platforms.interfaces.driver_platform_interface import DriverPlatformInterface, PulseSettings, HoldSettings
from mpf.platforms.interfaces.segment_display_platform_interface import SegmentDisplayPlatformInterface

from mpf.platforms.interfaces.switch_platform_interface import SwitchPlatformInterface

from mpf.core.logging import LogMixin

from mpf.platforms.lisy.defines import LisyDefines

from mpf.platforms.interfaces.light_platform_interface import LightPlatformSoftwareFade

from mpf.core.platform import SwitchPlatform, LightsPlatform, DriverPlatform, SwitchSettings, DriverSettings, \
    DriverConfig, SwitchConfig, SegmentDisplayPlatform


class LisySwitch(SwitchPlatformInterface):

    """A switch in the LISY platform."""

    pass


class LisyDriver(DriverPlatformInterface):

    """A driver in the LISY platform."""

    def __init__(self, config, number, platform):
        """Initialise driver."""
        super().__init__(config, number)
        self.platform = platform
        self._pulse_ms = -1

    def _configure_pulse_ms(self, pulse_ms):
        """Configure pulse ms for this driver if it changed."""
        if pulse_ms != self._pulse_ms:
            self._pulse_ms = pulse_ms
            self.platform.send_byte(LisyDefines.SolenoidsSetSolenoidPulseTime, bytes(
                [int(self.number),
                 int(pulse_ms / 256),
                 pulse_ms % 256
                 ]))

    def pulse(self, pulse_settings: PulseSettings):
        """Pulse driver."""
        self._configure_pulse_ms(pulse_settings.duration)
        self.platform.send_byte(LisyDefines.SolenoidsPulseSolenioid, bytes([int(self.number)]))

    def enable(self, pulse_settings: PulseSettings, hold_settings: HoldSettings):
        """Enable driver."""
        del hold_settings
        self._configure_pulse_ms(pulse_settings.duration)
        self.platform.send_byte(LisyDefines.SolenoidsSetSolenoidToOn, bytes([int(self.number)]))

    def disable(self):
        """Disable driver."""
        self.platform.send_byte(LisyDefines.SolenoidsSetSolenoidToOff, bytes([int(self.number)]))

    def get_board_name(self):
        """Return board name."""
        return "LISY"


class LisyLight(LightPlatformSoftwareFade):

    """A light in the LISY platform."""

    def __init__(self, number, platform):
        """Initialise Lisy Light."""
        super().__init__(platform.machine.clock.loop, 50)
        self.number = number
        self.platform = platform

    def set_brightness(self, brightness: float):
        """Turn lamp on or off."""
        if brightness > 0:
            self.platform.send_byte(LisyDefines.LampsSetLampOn, bytes([self.number]))
        else:
            self.platform.send_byte(LisyDefines.LampsSetLampOff, bytes([self.number]))


class LisyDisplay(SegmentDisplayPlatformInterface):

    """A segment display in the LISY platform."""

    def __init__(self, number: int, platform: "LisyHardwarePlatform"):
        """Initialise segment display."""
        super().__init__(number)
        self.platform = platform

    def set_text(self, text: str):
        """Set text to display."""
        self.platform.send_string(LisyDefines.DisplaysSetDisplay0To + self.number, text)


class LisyHardwarePlatform(SwitchPlatform, LightsPlatform, DriverPlatform, SegmentDisplayPlatform, LogMixin):

    """LISY platform."""

    def __init__(self, machine):
        """Initialise platform."""
        super().__init__(machine)
        self.config = None
        self._writer = None
        self._reader = None
        self._poll_task = None
        self._number_of_lamps = None
        self._number_of_solenoids = None
        self._number_of_displays = None
        self._inputs = dict()
        self._system_type = None
        self.features['max_pulse'] = 65536

    @asyncio.coroutine
    def initialize(self):
        """Initialise platform."""
        self.config = self.machine.config_validator.validate_config("lisy", self.machine.config['lisy'])

        self.configure_logging("lisy", self.config['console_log'], self.config['file_log'])

        if self.config['connection'] == "serial":
            self.log.info("Connecting to %s at %sbps", self.config['port'], self.config['baud'])
            connector = self.machine.clock.open_serial_connection(
                url=self.config['port'], baudrate=self.config['baud'], limit=0)
        else:
            self.log.info("Connecting to %s:%s", self.config['network_host'], self.config['network_port'])
            connector = self.machine.clock.open_connection(self.config['network_host'], self.config['network_port'])

        self._reader, self._writer = yield from connector

        # reset platform
        self.send_byte(LisyDefines.GeneralReset)
        return_code = yield from self.read_byte()
        if return_code != 0:
            raise AssertionError("Reset of LISY failed. Got {} instead of 0".format(return_code))

        # get type (system 1 vs system 80)
        self.send_byte(LisyDefines.InfoGetConnectedLisyHardware)
        type_str = yield from self.read_string()

        if type_str == b'LISY1':
            self._system_type = 1
        elif type_str == b'LISY80':
            self._system_type = 80
        else:
            raise AssertionError("Invalid LISY hardware version {}".format(type_str))

        # get number of lamps
        self.send_byte(LisyDefines.InfoGetNumberOfLamps)
        self._number_of_lamps = yield from self.read_byte()

        # get number of solenoids
        self.send_byte(LisyDefines.InfoGetNumberOfSolenoids)
        self._number_of_solenoids = yield from self.read_byte()

        # get number of displays
        self.send_byte(LisyDefines.InfoGetNumberOfDisplays)
        self._number_of_displays = yield from self.read_byte()

        # initially read all switches
        for row in range(8):
            for col in range(8):
                number = row * 10 + col
                self.send_byte(LisyDefines.SwitchesGetStatusOfSwitch, bytes([number]))
                state = yield from self.read_byte()
                if state > 1:
                    raise AssertionError("Invalid switch {}. Got response: {}".format(number, state))

                self._inputs[str(number)] = state == 1

        self._poll_task = self.machine.clock.loop.create_task(self._poll())
        self._poll_task.add_done_callback(self._done)

    def stop(self):
        """Stop platform."""
        if self._poll_task:
            self._poll_task.cancel()

        if self._reader:
            self._writer.close()

    @staticmethod
    def _done(future):
        try:
            future.result()
        except asyncio.CancelledError:
            pass

    @asyncio.coroutine
    def _poll(self):
        while True:
            self.send_byte(LisyDefines.SwitchesGetChangedSwitches)
            status = yield from self.read_byte()
            if status == 127:
                # no changes. sleep 1ms
                yield from asyncio.sleep(.001, loop=self.machine.clock.loop)
            else:
                # bit 7 is state
                switch_state = 1 if status & 0b10000000 else 0
                # bits 0-6 are the switch number
                switch_num = status & 0b01111111

                # tell the switch controller about the new state
                self.machine.switch_controller.process_switch_by_num(str(switch_num), switch_state, self)

                # store in dict as well
                self._inputs[str(switch_num)] = bool(switch_state)

    def set_pulse_on_hit_and_enable_and_release_rule(self, enable_switch: SwitchSettings, coil: DriverSettings):
        """No rules on LISY."""
        raise AssertionError("Hardware rules are not support in LISY.")

    def set_pulse_on_hit_and_enable_and_release_and_disable_rule(self, enable_switch: SwitchSettings,
                                                                 disable_switch: SwitchSettings, coil: DriverSettings):
        """No rules on LISY."""
        raise AssertionError("Hardware rules are not support in LISY.")

    def set_pulse_on_hit_and_release_rule(self, enable_switch: SwitchSettings, coil: DriverSettings):
        """No rules on LISY."""
        raise AssertionError("Hardware rules are not support in LISY.")

    def set_pulse_on_hit_rule(self, enable_switch: SwitchSettings, coil: DriverSettings):
        """No rules on LISY."""
        raise AssertionError("Hardware rules are not support in LISY.")

    def clear_hw_rule(self, switch: SwitchSettings, coil: DriverSettings):
        """No rules on LISY."""
        raise AssertionError("Hardware rules are not support in LISY.")

    def configure_light(self, number: str, subtype: str, platform_settings: dict) -> LightPlatformSoftwareFade:
        """Configure light on LISY."""
        del platform_settings, subtype

        if self._system_type == 80:
            if 0 < int(number) >= self._number_of_lamps:
                raise AssertionError("LISY only has {} lamps. Cannot configure lamp {} (zero indexed).".
                                     format(self._number_of_lamps, number))
        else:
            if 1 < int(number) > self._number_of_lamps:
                raise AssertionError("LISY only has {} lamps. Cannot configure lamp {} (one indexed).".
                                     format(self._number_of_lamps, number))

        return LisyLight(int(number), self)

    def parse_light_number_to_channels(self, number: str, subtype: str):
        """Return a single light."""
        return [
            {
                "number": number,
            }
        ]

    def configure_switch(self, number: str, config: SwitchConfig, platform_config: dict) -> SwitchPlatformInterface:
        """Configure a switch."""
        if (int(number) % 10) > 7 or 0 < int(number) > 77:
            raise AssertionError("Invalid switch number {}".format(number))

        return LisySwitch(config=config, number=number)

    def get_hw_switch_states(self):
        """Return current switch states."""
        return self._inputs

    def configure_driver(self, config: DriverConfig, number: str, platform_settings: dict) -> DriverPlatformInterface:
        """Configure a driver."""
        return LisyDriver(config=config, number=number, platform=self)

    def configure_segment_display(self, number: str) -> SegmentDisplayPlatformInterface:
        """Configure a segment display."""
        if 0 < int(number) >= self._number_of_displays:
            raise AssertionError("Invalid display number {}. Hardware only supports {} displays (indexed with 0)".
                                 format(number, self._number_of_displays))

        return LisyDisplay(int(number), self)

    def send_byte(self, cmd: int, byte: bytes=None):
        """Send a command with optional payload."""
        if byte is not None:
            cmd_str = bytes([cmd])
            cmd_str += byte
            self.log.debug("Sending %s %s", cmd, byte)
            self._writer.write(cmd_str)
        else:
            self.log.debug("Sending %s", cmd)
            self._writer.write(bytes([cmd]))

    def send_string(self, cmd: int, string: str):
        """Send a command with null terminated string."""
        self.log.debug("Sending %s %s", cmd, string)
        self._writer.write(bytes([cmd]) + string.encode() + bytes([0]))

    @asyncio.coroutine
    def read_byte(self) -> Generator[int, None, int]:
        """Read one byte."""
        self.log.debug("Reading one byte")
        data = yield from self._reader.readexactly(1)
        self.log.debug("Received %s", ord(data))
        return ord(data)

    @asyncio.coroutine
    def read_string(self) -> Generator[int, None, bytes]:
        """Read zero terminated string."""
        self.log.debug("Reading zero terminated string")
        data = yield from self._reader.readuntil(b'\x00')
        # remove terminator
        data = data[:-1]
        self.log.debug("Received %s", data)
        return data
