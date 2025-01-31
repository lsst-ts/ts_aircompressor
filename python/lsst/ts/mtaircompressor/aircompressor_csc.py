# This file is part of ts_mtaircompressor.
#
# Developed for the Vera Rubin Observatory Telescope and Site Systems.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

__all__ = ["MTAirCompressorCsc", "run_mtaircompressor"]

import argparse
import asyncio
import typing

import pymodbus.exceptions
from lsst.ts import salobj, utils
from pymodbus.client.tcp import AsyncModbusTcpClient as ModbusClient
from pymodbus.constants import Endian
from pymodbus.payload import BinaryPayloadDecoder
from pymodbus.server.async_io import ModbusTcpServer

from . import __version__
from .aircompressor_model import MTAirCompressorModel
from .config_schema import CONFIG_SCHEMA
from .enums import ErrorCode
from .simulator import create_server_and_run_on_background
from .utils import status_bit_to_bools

"""Telemetry period. Telemetry shall be reported every n seconds."""
POLL_PERIOD = 1

"""Sleep for this number of seconds before reconnecting."""
SLEEP_RECONNECT = 5

"""Sleep for this number of seconds after catching an exception."""
SLEEP_EXCEPTION = 2


class MTAirCompressorCsc(salobj.ConfigurableCsc):
    """MTAirCompressor CsC

    Parameters
    ----------
    index : `int`
        CSC index.
    config_dir : `str` (optional)
        Directory of configuration files, or None for the standard
        configuration directory (obtained from `get_default_config_dir`).
        This is provided for unit testing.
    initial_state : `lsst.ts.salobj.State`
        CSC initial state.
    override : `str`, optional
        Configuration override file to apply if ``initial_state`` is
        `State.DISABLED` or `State.ENABLED`.
    simulation_mode : `int`, optional
        CSC simulation mode. 0 - no simulation, 1 - software simulation (no
        mock modbus needed).
    """

    enable_cmdline_state = True
    valid_simulation_modes: typing.Sequence[int] = (0, 1)
    version = __version__

    def __init__(
        self,
        index: int,
        config_dir: str | None = None,
        initial_state: salobj.State = salobj.State.STANDBY,
        override: str = "",
        simulation_mode: int = 0,
    ) -> None:
        super().__init__(
            name="MTAirCompressor",
            index=index,
            config_schema=CONFIG_SCHEMA,
            config_dir=config_dir,
            initial_state=initial_state,
            override=override,
            simulation_mode=simulation_mode,
        )

        self.grace_period: float | None = None
        self.host: str | None = None
        self.port: int | None = None
        self.unit: int | None = None

        self.connection: ModbusClient | None = None
        self.model: MTAirCompressorModel | None = None
        self.simulator: ModbusTcpServer | None = None
        self.simulator_task = utils.make_done_future()
        # True if compressor can be started remotely. Used before start command
        # is issued to clearly indicate the problem
        self._start_by_remote: bool = False
        # This will be reseted to None only after connection is properly
        # re-established.  Don't reset it in def connect, as it is needed in
        # poll_loop to report time waiting for reconnection. None when not
        # failed, TAI when failure was firstly detected
        self._failed_tai: float | None = None

        self.poll_task = utils.make_done_future()

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        """Adds custom --grace-period, --host, --port and --unit arguments."""
        parser.add_argument(
            "--grace-period",
            type=int,
            default=None,
            help="TCP/IP connection grace period in seconds. Default to 60 minutes (3600 seconds)",
        )
        parser.add_argument(
            "--host",
            type=str,
            default=None,
            help="hostname of the compressor ModbusRTU/TCP convertor."
            "Unless specified, m1m3cam-aircomp0X.cp.lsst.org, where X is compressor index",
        )
        parser.add_argument(
            "--port",
            type=int,
            default=None,
            help="TCP/IP port of the compressor ModbusRTU/TCP convertor."
            "Defaults to 502 (default Modbus TCP/IP port)",
        )
        parser.add_argument(
            "--unit", type=int, default=None, help="modbus unit address"
        )

    @classmethod
    def add_kwargs_from_args(
        cls, args: argparse.Namespace, kwargs: dict[str, typing.Any]
    ) -> None:
        """Process custom --grace-period, --host, --port and --unit
        arguments."""
        cls.grace_period = args.grace_period
        cls.host = args.host
        cls.port = args.port
        cls.unit = args.unit

    async def configure(self, config: typing.Any) -> None:
        instance = [i for i in config.instances if i["sal_index"] == self.salinfo.index]
        if len(instance) == 0:
            raise RuntimeError(
                f"Cannot find configuration for index {self.salinfo.index},"
                "at least sal_index entry must be provided"
            )
        elif len(instance) > 1:
            raise RuntimeError(
                f"Multiple configuration instances matches index {self.salinfo.index},"
                "please check configuration file"
            )
        our_instance = instance[0]
        if self.grace_period is None:
            self.grace_period = our_instance.get("grace_period", 3600)
        if self.host is None:
            self.host = our_instance.get(
                "host", f"m1m3cam-aircomp{self.salinfo.index:02d}.cp.lsst.org"
            )
        if self.port is None:
            self.port = our_instance.get("port", 502)
        if self.unit is None:
            self.unit = our_instance.get("unit", self.salinfo.index)

    @staticmethod
    def get_config_pkg() -> str:
        return "ts_config_mttcs"

    async def _close_own_tasks(self) -> None:
        if self.simulation_mode == 1 and self.simulator is not None:
            await self.simulator.shutdown()
            self.simulator_task.cancel()
        self.poll_task.cancel()
        await self.disconnect()

    async def close_tasks(self) -> None:
        await self._close_own_tasks()
        await super().close_tasks()

    async def log_modbus_exception(
        self, exception: typing.Any, msg: str = "", ignore_timeouts: bool = False
    ) -> None:
        if isinstance(exception, pymodbus.exceptions.ConnectionException):
            await self.disconnect()

        if not ignore_timeouts:
            if self.summary_state != salobj.State.FAULT and (
                self._failed_tai is None
                or self.grace_period is None
                or utils.current_tai() < self._failed_tai + self.grace_period
            ):
                # TimeoutError doesn't provide details, so provide them here
                # TODO: Python 3.11 shall merge TimeoutError and
                # asyncio.TimeoutError
                if isinstance(exception, (asyncio.TimeoutError, TimeoutError)):
                    self.log.error("TimeoutError. " + msg)
                else:
                    self.log.error(str(exception))
                if self._failed_tai is None:
                    self.log.warning(
                        "Lost compressor connection, will try to reconnect for"
                        f" {self.grace_period} seconds"
                    )
                    self._failed_tai = utils.current_tai()
                return

        try:
            await self.fault(exception.original_code, msg)
        except AttributeError:
            if isinstance(exception, pymodbus.exceptions.ConnectionException):
                await self.fault(ErrorCode.COULD_NOT_CONNECT, msg + str(exception))
            else:
                await self.fault(ErrorCode.MODBUS_ERROR, msg + str(exception))

        self._failed_tai = None

    async def connect(self) -> None:
        if self.connection is None:
            self.connection = ModbusClient(host=self.host, port=self.port)
        assert self.connection is not None
        await self.connection.connect()
        if self.model is None:
            assert self.unit is not None
            self.model = MTAirCompressorModel(self.connection, self.unit)
        await self.evt_connectionStatus.set_write(connected=True)
        await self.update_compressor_info()
        self.log.info(f"Connected to {self.host}:{self.port}")

    async def disconnect(self) -> None:
        await self.evt_connectionStatus.set_write(connected=False)
        self.model = None
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    async def end_start(self, data: salobj.type_hints.BaseMsgType) -> None:
        """Enables communication with the compressor."""
        if self.simulation_mode == 1:
            self.unit = 1

            (
                self.simulator,
                self.simulator_task,
                self.host,
                self.port,
            ) = await create_server_and_run_on_background()

        try:
            await self.connect()
            if self.poll_task.done():
                self.poll_task = asyncio.create_task(self.poll_loop())
        except (
            pymodbus.exceptions.ModbusException,
            asyncio.TimeoutError,
        ) as ex:
            await self.log_modbus_exception(ex, "Starting up:", True)
            return

    async def begin_standby(self, data: salobj.type_hints.BaseMsgType) -> None:
        await self._close_own_tasks()

    def _expected_error(self, msg: str) -> None:
        self.log.error(msg)
        raise salobj.ExpectedError(msg)

    async def do_reset(self, data: salobj.type_hints.BaseMsgType) -> None:
        """Reset compressor faults."""
        self.assert_enabled()
        try:
            assert self.model is not None
            await self.model.reset()
            self.log.info("Compressor reset.")
        except (
            pymodbus.exceptions.ModbusException,
            asyncio.TimeoutError,
        ) as ex:
            self._expected_error(f"Cannot reset compressor: {str(ex)}")

    async def do_powerOn(self, data: salobj.type_hints.BaseMsgType) -> None:
        """Powers on compressor."""
        self.assert_enabled()
        try:
            assert self.model is not None
            await self.model.power_on()
            self.log.info("Compressor powered on.")
        except (
            pymodbus.exceptions.ModbusException,
            asyncio.TimeoutError,
        ) as ex:
            self._expected_error(f"Cannot power on compressor: {str(ex)}")

    async def do_powerOff(self, data: salobj.type_hints.BaseMsgType) -> None:
        self.assert_enabled()
        try:
            assert self.model is not None
            await self.model.power_off()
            self.log.info("Compressor powered off.")
        except (
            pymodbus.exceptions.ModbusException,
            asyncio.TimeoutError,
        ) as ex:
            self._expected_error(f"Cannot power off compressor: {str(ex)}")

    async def update_status(self) -> None:
        """Read compressor status - 3 status registers starting from address
        0x30."""
        assert self.model is not None
        status = await self.model.get_status()

        await self.evt_status.set_write(
            **status_bit_to_bools(
                [
                    "readyToStart",
                    "operating",
                    "startInhibit",
                    "motorStartPhase",
                    "offLoad",
                    "onLoad",
                    "softStop",
                    "runOnTimer",
                    "fault",
                    "warning",
                    "serviceRequired",
                    "minAllowedSpeedAchieved",
                    "maxAllowedSpeedAchieved",
                ],
                status[0],
            ),
            **status_bit_to_bools(
                [
                    "startByRemote",
                    "startWithTimerControl",
                    "startWithPressureRequirement",
                    "startAfterDePressurise",
                    "startAfterPowerLoss",
                    "startAfterDryerPreRun",
                ],
                status[2],
            ),
        )

        self._start_by_remote = status[2] & 0x01 == 0x01

    async def update_errorsWarnings(self) -> None:
        assert self.model is not None
        errorsWarnings = await self.model.get_error_registers()

        await self.evt_errors.set_write(
            **status_bit_to_bools(
                [
                    "powerSupplyFailureE400",
                    "emergencyStopActivatedE401",
                    "highMotorTemperatureM1E402",
                    "compressorDischargeTemperatureE403",
                    "startTemperatureLowE404",
                    "dischargeOverPressureE405",
                    "linePressureSensorB1E406",
                    "dischargePressureSensorB2E407",
                    "dischargeTemperatureSensorR2E408",
                    "controllerHardwareE409",
                    "coolingE410",
                    "oilPressureLowE411",
                    "externalFaultE412",
                    "dryerE413",
                    "condensateDrainE414",
                    "noPressureBuildUpE415",
                ],
                errorsWarnings[0],
            ),
            **status_bit_to_bools(
                ["heavyStartupE416"],
                errorsWarnings[1],
            ),
            **status_bit_to_bools(
                [
                    "preAdjustmentVSDE500",
                    "preAdjustmentE501",
                    "lockedVSDE502",
                    "writeFaultVSDE503",
                    "communicationVSDE504",
                    "stopPressedVSDE505",
                    "stopInputEMVSDE506",
                    "readFaultVSDE507",
                    "stopInputVSDEME508",
                    "seeVSDDisplayE509",
                    "speedBelowMinLimitE510",
                ],
                errorsWarnings[6],
            ),
        )

        await self.evt_warnings.set_write(
            **status_bit_to_bools(
                [
                    "serviceDueA600",
                    "dischargeOverPressureA601",
                    "compressorDischargeTemperatureA602",
                    None,
                    None,
                    None,
                    "linePressureHighA606",
                    "controllerBatteryEmptyA607",
                    "dryerA608",
                    "condensateDrainA609",
                    "fineSeparatorA610",
                    "airFilterA611",
                    "oilFilterA612",
                    "oilLevelLowA613",
                    "oilTemperatureHighA614",
                    "externalWarningA615",
                ],
                errorsWarnings[8],
            ),
            **status_bit_to_bools(
                [
                    "motorLuricationSystemA616",
                    "input1A617",
                    "input2A618",
                    "input3A619",
                    "input4A620",
                    "input5A621",
                    "input6A622",
                    "fullSDCardA623",
                ],
                errorsWarnings[9],
            ),
            **status_bit_to_bools(
                ["temperatureHighVSDA700"],
                errorsWarnings[14],
            ),
        )

    async def update_compressor_info(self) -> None:
        """Read compressor info - serial number and software version."""

        def to_string(arr: typing.Any) -> str:
            return "".join(map(chr, arr))

        assert self.model is not None
        info = await self.model.get_compressor_info()
        await self.evt_compressorInfo.set_write(
            softwareVersion=to_string(info[0:14]),
            serialNumber=to_string(info[14:23]),
        )

    async def update_analog_data(self) -> None:
        """Read compressor analog (telemetry-worth) data."""
        assert self.model is not None
        analog = await self.model.get_analog_data()
        # skip analog[8], Compressor power consumption, not available on the
        # compressor
        del analog[8]
        decoder = BinaryPayloadDecoder.fromRegisters(
            analog, wordorder=Endian.BIG, byteorder=Endian.BIG
        )

        await self.tel_analogData.set_write(
            force_output=True,
            waterLevel=decoder.decode_16bit_int(),
            targetSpeed=decoder.decode_16bit_uint(),
            motorCurrent=decoder.decode_16bit_uint() / 10.0,
            heatsinkTemperature=decoder.decode_16bit_int(),
            dclinkVoltage=decoder.decode_16bit_uint(),
            motorSpeedPercentage=decoder.decode_16bit_uint(),
            motorSpeedRPM=decoder.decode_16bit_uint(),
            motorInput=decoder.decode_16bit_uint() / 10.0,
            # unavailable on LRS model - see above for analog reduction
            # compressorPowerConsumption=decoder.decode_16bit_uint() / 10.0,
            compressorVolumePercentage=decoder.decode_16bit_uint(),
            compressorVolume=decoder.decode_16bit_uint() / 10.0,
            groupVolume=decoder.decode_16bit_uint() / 10.0,
            stage1OutputPressure=decoder.decode_16bit_int(),
            linePressure=decoder.decode_16bit_int(),
            stage1OutputTemperature=decoder.decode_16bit_int(),
        )

    async def update_timer(self) -> None:
        """Read compressors timers."""
        assert self.model is not None
        timers = await self.model.get_timers()
        decoder = BinaryPayloadDecoder.fromRegisters(
            timers, wordorder=Endian.BIG, byteorder=Endian.BIG
        )

        await self.evt_timerInfo.set_write(
            runningHours=decoder.decode_32bit_uint(),
            loadedHours=decoder.decode_32bit_uint(),
            lowestServiceCounter=decoder.decode_16bit_int(),
            runOnTimer=decoder.decode_16bit_int(),
            # unavailable on LRS model
            # loadedHours50Percent=decoder.decode_32bit_int()),
        )

    async def telemetry_loop(self) -> None:
        """Runs telemetry loop."""
        timerUpdate = 0
        try:
            while True:
                await self.update_status()
                await self.update_errorsWarnings()
                await self.update_analog_data()

                if timerUpdate <= 0:
                    await self.update_timer()
                    timerUpdate = 60
                else:
                    timerUpdate -= 1

                await asyncio.sleep(1)

        except (
            pymodbus.exceptions.ModbusException,
            asyncio.TimeoutError,
        ) as ex:
            await self.log_modbus_exception(ex)

        except Exception as ex:
            await self.fault(1, f"Error in telemetry loop: {ex}, type {type(ex)}")

    async def poll_loop(self) -> None:
        while True:
            try:
                if self._failed_tai is not None:
                    if self.model is None:
                        await self.connect()
                    assert self.model is not None
                    await self.model.get_compressor_info()
                    self.log.info(
                        "Compressor connection is back after "
                        f"{utils.current_tai() - self._failed_tai:.1f} seconds"
                    )
                    self._failed_tai = None
                elif self.disabled_or_enabled:
                    await self.telemetry_loop()
                elif self.summary_state in (salobj.State.STANDBY, salobj.State.FAULT):
                    pass
                else:
                    self.log.critical(f"Unhandled state: {self.summary_state}")

                await asyncio.sleep(POLL_PERIOD)

            except (
                pymodbus.exceptions.ModbusException,
                asyncio.TimeoutError,
                TimeoutError,
            ) as ex:
                await self.log_modbus_exception(ex, "While reconnecting:")
                await self.disconnect()
                await asyncio.sleep(SLEEP_RECONNECT)
            except Exception as ex:
                self.log.exception(f"Exception in poll loop: {str(ex)}")
                await self.disconnect()
                await asyncio.sleep(SLEEP_EXCEPTION)

            if self.summary_state == salobj.State.FAULT:
                await self.disconnect()
                # end loop
                return


def run_mtaircompressor() -> None:
    """Run the MTAirCompressor CSC."""
    asyncio.run(MTAirCompressorCsc.amain(True))
