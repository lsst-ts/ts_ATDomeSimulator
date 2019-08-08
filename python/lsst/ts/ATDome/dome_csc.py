# This file is part of ts_ATDome.
#
# Developed for the LSST Data Management System.
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

__all__ = ["ATDomeCsc"]

import asyncio
import enum
import math
import pathlib

from astropy.coordinates import Angle
import astropy.units as u

from lsst.ts import salobj
from lsst.ts.idl.enums.ATDome import AzimuthCommandedState, AzimuthState, \
    ShutterDoorCommandedState, ShutterDoorState
from .utils import angle_diff
from .mock_controller import MockDomeController
from .status import ShortStatus, RemainingStatus

_LOCAL_HOST = "127.0.0.1"


class MoveCode(enum.IntFlag):
    AZPOSITIVE = 1
    AZNEGATIVE = 2
    MAINDOORCLOSING = 4
    MAINDOOROPENING = 8
    DROPOUTDOORCLOSING = 16
    DROPOUTDOOROPENING = 32
    HOMING = 64
    ESTOP = 128


class Axis(enum.Flag):
    AZ = enum.auto()
    DROPOUTDOOR = enum.auto()
    MAINDOOR = enum.auto()


class ATDomeCsc(salobj.ConfigurableCsc):
    """AuxTel dome CSC

    Parameters
    ----------
    index : `int` or `None`
        SAL component index, or 0 or None if the component is not indexed.
    initial_state : `salobj.State` or `int` (optional)
        The initial state of the CSC. This is provided for unit testing,
        as real CSCs should start up in `lsst.ts.salobj.StateSTANDBY`,
        the default.
    initial_simulation_mode : `int` (optional)
        Initial simulation mode.
    mock_port : `int` (optional)
        Port for mock controller TCP/IP interface. If `None` then use the
        port specified by the configuration. Only used in simulation mode.

    Raises
    ------
    salobj.ExpectedError
        If initial_state or initial_simulation_mode is invalid.

    Notes
    -----
    **Simulation Modes**

    Supported simulation modes (TODO DM-19530 update these values):

    * 0: regular operation
    * 1: simulation mode: start a mock TCP/IP ATDome controller and talk to it

    **Error Codes**

    * 1: could not connect to TCP/IP ATDome controller
    * 2: read from TCP/IP ATDome controller timed out
    """
    def __init__(self, index, config_dir=None, initial_state=salobj.State.STANDBY,
                 initial_simulation_mode=0, mock_port=None):
        schema_path = pathlib.Path(__file__).resolve().parents[4].joinpath("schema", "ATDome.yaml")

        self.reader = None
        self.writer = None
        self.move_code = 0
        self.mock_ctrl = None  # mock controller, or None of not constructed
        self.status_interval = 0.2  # delay between short status commands (sec)
        self.n_short_status = 0
        self.short_per_full = 5  # number of short status between full status
        self.az_tolerance = Angle(0.2, u.deg)  # tolerance for "in position"
        self.status_sleep_task = None  # sleep in status_loop
        self.status_task = None  # status_loop
        self.connect_task = None  # wait while connecting
        self.cmd_lock = asyncio.Lock()
        self.config = None
        self.mock_port = mock_port
        self.defer_simulation_mode_until_configured = False
        super().__init__("ATDome", index=index, schema_path=schema_path, config_dir=config_dir,
                         initial_state=initial_state, initial_simulation_mode=initial_simulation_mode)

    async def do_moveAzimuth(self, data):
        """Implement the ``moveAzimuth`` command."""
        self.assert_enabled("moveAzimuth")
        if self.evt_azimuthState.data.homing:
            raise salobj.ExpectedError("Cannot move azimuth while homing")
        azimuth = data.azimuth
        if azimuth < 0 or azimuth > 360:
            raise salobj.ExpectedError(f"azimuth={azimuth} deg; must be in range [0, 360]")
        await self.run_command(f"{azimuth:0.3f} MV")
        self.evt_azimuthCommandedState.set_put(commandedState=AzimuthCommandedState.GOTOPOSITION,
                                               azimuth=azimuth, force_output=True)
        self.cancel_status_sleep()

    async def do_closeShutter(self, data):
        """Implement the ``closeShutter`` command."""
        self.assert_enabled("closeShutter")
        await self.run_command("SC")
        self.evt_dropoutDoorCommandedState.set_put(commandedState=ShutterDoorCommandedState.CLOSED,
                                                   force_output=True)
        self.evt_mainDoorCommandedState.set_put(commandedState=ShutterDoorCommandedState.CLOSED,
                                                force_output=True)
        self.cancel_status_sleep()

    async def do_openShutter(self, data):
        """Implement the ``openShutter`` command."""
        self.assert_enabled("openShutter")
        await self.run_command("SO")
        self.evt_dropoutDoorCommandedState.set_put(commandedState=ShutterDoorCommandedState.OPENED,
                                                   force_output=True)
        self.evt_mainDoorCommandedState.set_put(commandedState=ShutterDoorCommandedState.OPENED,
                                                force_output=True)
        self.cancel_status_sleep()

    async def do_stopMotion(self, data):
        """Implement the ``stopMotion`` command."""
        self.assert_enabled("stopMotion")
        self.evt_azimuthCommandedState.set_put(commandedState=AzimuthCommandedState.STOP,
                                               force_output=True)
        self.evt_dropoutDoorCommandedState.set_put(commandedState=ShutterDoorCommandedState.STOP,
                                                   force_output=True)
        self.evt_mainDoorCommandedState.set_put(commandedState=ShutterDoorCommandedState.STOP,
                                                force_output=True)
        await self.run_command("ST")
        self.cancel_status_sleep()

    async def do_homeAzimuth(self, data):
        """Implement the ``homeAzimuth`` command."""
        self.assert_enabled("homeAzimuth")
        if self.evt_azimuthState.data.homing:
            raise salobj.ExpectedError("Already homing")
        self.evt_azimuthCommandedState.set_put(commandedState=AzimuthCommandedState.HOME,
                                               azimuth=math.nan, force_output=True)
        await self.run_command("HM")
        self.cancel_status_sleep()

    async def do_moveShutterDropoutDoor(self, data):
        """Implement the ``moveShutterDropoutDoor`` command."""
        self.assert_enabled("moveShutterDropoutDoor")
        if self.evt_mainDoorState.data.state != ShutterDoorState.OPENED:
            raise salobj.ExpectedError("Cannot move the dropout door until the main door is fully open.")
        if data.open:
            self.evt_dropoutDoorCommandedState.set_put(commandedState=ShutterDoorCommandedState.OPENED,
                                                       force_output=True)
            await self.run_command("DN")
        else:
            self.evt_dropoutDoorCommandedState.set_put(commandedState=ShutterDoorCommandedState.CLOSED,
                                                       force_output=True)
            await self.run_command("UP")
        self.cancel_status_sleep()

    async def do_moveShutterMainDoor(self, data):
        """Implement the ``moveShutterMainDoor`` command."""
        self.assert_enabled("moveShutterMainDoor")
        if data.open:
            self.evt_mainDoorCommandedState.set_put(commandedState=ShutterDoorCommandedState.OPENED,
                                                    force_output=True)
            await self.run_command("OP")
        else:
            if self.evt_dropoutDoorState.data.state not in (
                    ShutterDoorState.CLOSED,
                    ShutterDoorState.OPENED):
                raise salobj.ExpectedError("Cannot close the main door "
                                           "until the dropout door is fully closed or fully open.")
            self.evt_mainDoorCommandedState.set_put(commandedState=ShutterDoorCommandedState.CLOSED,
                                                    force_output=True)
            await self.run_command("CL")
        self.cancel_status_sleep()

    async def run_command(self, cmd):
        """Send a command to the TCP/IP controller and process its replies.

        Parameters
        ----------
        cmd : `str`
            The command to send, e.g. "5.0 MV", "SO" or "?".
        """
        if not self.connected:
            if self.want_connection and self.connect_task is not None and not self.connect_task.done():
                await self.connect_task
            else:
                raise RuntimeError("Not connected and not trying to connect")
        async with self.cmd_lock:
            self.writer.write(f"{cmd}\r\n".encode())
            await self.writer.drain()
            expected_lines = {  # excluding final ">" line
                "?": 5,
                "+": 25,
            }.get(cmd, 0)

            try:
                read_bytes = await asyncio.wait_for(self.reader.readuntil(">".encode()),
                                                    timeout=self.config.read_timeout)
            except Exception as e:
                if isinstance(e, asyncio.streams.IncompleteReadError):
                    err_msg = "TCP/IP controller exited"
                else:
                    err_msg = "TCP/IP read failed"
                self.log.exception(err_msg)
                await self.disconnect()
                self.fault(code=2, report=f"{err_msg}: {e}")
                return

            data = read_bytes.decode()
            lines = data.split("\n")[:-1]  # strip final > line
            lines = [elt.strip() for elt in lines]
            if len(lines) != expected_lines:
                self.log.warning(f"Command {cmd} returned {data}; expected {expected_lines} lines")
                return
            if cmd == "?":
                if self.handle_short_status(lines):
                    self.evt_settingsAppliedDomeController.put()
            elif cmd == "+":
                self.handle_full_status(lines)

    def compute_in_position_mask(self, move_code):
        """Compute in_position_mask.

        self.tel_position.data must be current.

        Parameters
        ----------
        move_code : `int`
            Motion code: the integer from line 5 of short status.

        Returns
        -------
        in_position_mask : `MoveCode`
            A bit mask with 1 for each axis that is in position.
        """
        mask = Axis(0)
        az_halted = move_code & (MoveCode.AZPOSITIVE | MoveCode.AZNEGATIVE) == 0
        if az_halted and \
                self.evt_azimuthCommandedState.data.commandedState == AzimuthCommandedState.GOTOPOSITION:
            daz = angle_diff(self.tel_position.data.azimuthPosition,
                             self.evt_azimuthCommandedState.data.azimuth)
            if abs(daz) < self.az_tolerance:
                mask |= Axis.AZ

        dropout_halted = move_code & (MoveCode.DROPOUTDOORCLOSING | MoveCode.DROPOUTDOOROPENING) == 0
        if dropout_halted:
            if self.evt_dropoutDoorCommandedState.data.commandedState == ShutterDoorCommandedState.OPENED:
                if self.tel_position.data.dropoutDoorOpeningPercentage == 100:
                    mask |= Axis.DROPOUTDOOR
            elif self.evt_dropoutDoorCommandedState.data.commandedState == ShutterDoorCommandedState.CLOSED:
                if self.tel_position.data.dropoutDoorOpeningPercentage == 0:
                    mask |= Axis.DROPOUTDOOR

        dropout_halted = move_code & (MoveCode.DROPOUTDOORCLOSING | MoveCode.DROPOUTDOOROPENING) == 0
        if dropout_halted:
            if self.evt_dropoutDoorCommandedState.data.commandedState == ShutterDoorCommandedState.OPENED:
                if self.tel_position.data.dropoutDoorOpeningPercentage == 100:
                    mask |= Axis.DROPOUTDOOR
            elif self.evt_dropoutDoorCommandedState.data.commandedState == ShutterDoorCommandedState.CLOSED:
                if self.tel_position.data.dropoutDoorOpeningPercentage == 0:
                    mask |= Axis.DROPOUTDOOR

        main_halted = move_code & (MoveCode.MAINDOORCLOSING | MoveCode.MAINDOOROPENING) == 0
        if main_halted:
            if self.evt_mainDoorCommandedState.data.commandedState == ShutterDoorCommandedState.OPENED:
                if self.tel_position.data.mainDoorOpeningPercentage == 100:
                    mask |= Axis.MAINDOOR
            elif self.evt_mainDoorCommandedState.data.commandedState == ShutterDoorCommandedState.CLOSED:
                if self.tel_position.data.mainDoorOpeningPercentage == 0:
                    mask |= Axis.MAINDOOR

        return mask

    def compute_az_state(self, move_code):
        """Compute the state field for the azimuthState event.

        Parameters
        ----------
        move_code : `int`
            Motion code: the integer from line 5 of short status.

        Returns
        -------
        state : `int`
            The appropriate `AzimuthState` enum value.
        """
        if move_code & MoveCode.AZPOSITIVE:
            state = AzimuthState.MOVINGCW
        elif move_code & MoveCode.AZNEGATIVE:
            state = AzimuthState.MOVINGCCW
        else:
            state = AzimuthState.NOTINMOTION
        return state

    def compute_door_state(self, open_pct, is_main, move_code):
        """Compute data for the shutterState event.

        Parameters
        ----------
        open_pct : `float`
            Percent opening.
        is_main : `bool`
            True if the main door, False if the dropout door.
        move_code : `int`
            Motion code: the integer from line 5 of short status.
        """
        closing_code = MoveCode.MAINDOORCLOSING if is_main else MoveCode.DROPOUTDOORCLOSING
        opening_code = MoveCode.MAINDOOROPENING if is_main else MoveCode.DROPOUTDOOROPENING
        door_mask = closing_code | opening_code
        door_state = None
        if move_code & door_mask == 0:
            if open_pct == 0:
                door_state = ShutterDoorState.CLOSED
            elif open_pct == 100:
                door_state = ShutterDoorState.OPENED
            else:
                door_state = ShutterDoorState.PARTIALLYOPENED
        elif move_code & closing_code:
            door_state = ShutterDoorState.CLOSING
        elif move_code & opening_code:
            door_state = ShutterDoorState.OPENING
        if door_state is None:
            raise RuntimeError(f"Could not parse main door state from move_code={move_code}")
        return door_state

    @staticmethod
    def get_config_pkg():
        return "ts_config_attcs"

    async def configure(self, config):
        self.config = config
        self.evt_settingsAppliedDomeTcp.set_put(
            host=self.config.host,
            port=self.config.port,
            connectionTimeout=self.config.connection_timeout,
            readTimeout=self.config.read_timeout,
            force_output=True,
        )
        if self.defer_simulation_mode_until_configured:
            self.defer_simulation_mode_until_configured = False
            await self._handle_simulation_mode(self.simulation_mode)

    async def connect(self):
        """Connect to the dome controller's TCP/IP port.
        """
        self.log.debug("connect")
        if self.config is None:
            raise RuntimeError("Not yet configured")
        if self.connected:
            raise RuntimeError("Already connected")
        if self.connect_task is not None:
            self.log.warning("Connect called while already connecting; ignoring the second call")
            return
        host = _LOCAL_HOST if self.simulation_mode == 1 else self.config.host
        try:
            async with self.cmd_lock:
                if self.simulation_mode != 0:
                    if self.mock_ctrl is None:
                        raise RuntimeError("In simulation mode but no mock controller found.")
                    port = self.mock_ctrl.port
                else:
                    port = self.config.port
                self.connect_task = asyncio.open_connection(host=host, port=port)
                self.reader, self.writer = await asyncio.wait_for(self.connect_task,
                                                                  timeout=self.config.connection_timeout)
                # drop welcome message
                await asyncio.wait_for(self.reader.readuntil(">".encode()), timeout=self.config.read_timeout)
            self.log.debug("connected")
        except Exception as e:
            err_msg = f"Could not open connection to host={host}, port={self.config.port}"
            self.log.exception(err_msg)
            self.summary_state = salobj.State.FAULT
            self.evt_errorCode.set_put(errorCode=1, errorReport=f"{err_msg}: {e}", force_output=True)
            return
        finally:
            self.connect_task = None

        self.status_task = asyncio.ensure_future(self.status_loop())

    @property
    def connected(self):
        if None in (self.reader, self.writer):
            return False
        return True

    async def disconnect(self):
        """Disconnect from the dome controller's TCP/IP port.
        """
        self.log.debug("disconnect")
        writer = self.writer
        self.reader = None
        self.writer = None
        if writer:
            try:
                writer.write_eof()
                await asyncio.wait_for(writer.drain(), timeout=2)
            finally:
                writer.close()
        self.cancel_status_sleep()
        if self.status_task is not None:
            await asyncio.wait_for(self.status_task, timeout=self.config.read_timeout*2)

    def handle_short_status(self, lines):
        """Handle output of "?" command.

        Parameters
        ----------
        lines : `iterable` of `str`
            Lines of output from "?", the short status command, or the
            first 5 lines of output from the full status command "+".

        Returns
        -------
        settingsAppliedDomeController : `bool`
            True if ``self.evt_settingsAppliedDomeController`` updated.
        """
        status = ShortStatus(lines)

        self.tel_position.data.mainDoorOpeningPercentage = status.main_door_pct
        self.tel_position.data.dropoutDoorOpeningPercentage = status.dropout_door_pct
        settings_updated = self.evt_settingsAppliedDomeController.set(
            autoShutdownEnabled=status.auto_shutdown_enabled)

        self.tel_position.set_put(azimuthPosition=status.az_pos.deg)

        move_code = status.move_code
        self.evt_azimuthState.set_put(
            state=self.compute_az_state(move_code),
            homing=bool(move_code & MoveCode.HOMING))

        dropout_door_state = self.compute_door_state(
            open_pct=self.tel_position.data.dropoutDoorOpeningPercentage,
            is_main=False,
            move_code=move_code)
        main_door_state = self.compute_door_state(
            open_pct=self.tel_position.data.mainDoorOpeningPercentage,
            is_main=True,
            move_code=move_code)
        self.evt_dropoutDoorState.set_put(state=dropout_door_state)
        self.evt_mainDoorState.set_put(state=main_door_state)

        self.evt_emergencyStop.set_put(active=move_code & MoveCode.ESTOP > 0)

        in_position_mask = self.compute_in_position_mask(move_code)

        def in_position(mask):
            return in_position_mask & mask == mask

        azimuth_in_position = in_position(Axis.AZ)
        shutter_in_position = in_position(Axis.DROPOUTDOOR | Axis.MAINDOOR)
        self.evt_azimuthInPosition.set_put(inPosition=azimuth_in_position)
        self.evt_shutterInPosition.set_put(inPosition=shutter_in_position)
        self.evt_allAxesInPosition.set_put(inPosition=azimuth_in_position and shutter_in_position)

        return settings_updated

    def handle_full_status(self, lines):
        """Handle output of "+" command.
        """
        status = RemainingStatus(lines)

        # The first five lines are identical to short status.
        # Unfortunately they include one item of data for the
        # settingsAppliedDomeController event: autoShutdownEnabled;
        # settings_updated is set True if that changes
        settings_updated = self.handle_short_status(lines[0:5])

        self.evt_emergencyStop.set_put(active=status.estop_active)
        self.evt_scbLink.set_put(active=status.scb_link_ok)

        self.evt_settingsAppliedDomeController.set_put(
            rainSensorEnabled=status.rain_sensor_enabled,
            cloudSensorEnabled=status.cloud_sensor_enabled,
            tolerance=status.tolerance.deg,
            homeAzimuth=status.home_azimuth.deg,
            highSpeedDistance=status.high_speed.deg,
            watchdogTimer=status.watchdog_timer,
            reversalDelay=status.reversal_delay,
            force_output=settings_updated,
        )

        self.is_first_status = False

    async def implement_simulation_mode(self, simulation_mode):
        if simulation_mode not in (0, 1):
            raise salobj.ExpectedError(
                f"Simulation_mode={simulation_mode} must be 0 or 1")

        if self.simulation_mode == simulation_mode:
            return

        if self.config is None:
            self.log.debug("defer_simulation_mode_until_configured")
            self.defer_simulation_mode_until_configured = True
            return

        await self._handle_simulation_mode(simulation_mode)

    async def _handle_simulation_mode(self, simulation_mode):
        try:
            async with self.cmd_lock:
                await self.disconnect()
                await self.stop_mock_ctrl()
                if simulation_mode == 1:
                    if self.mock_port is not None:
                        port = self.mock_port
                    else:
                        port = self.config.port
                    self.mock_ctrl = MockDomeController(port=port)
                    await asyncio.wait_for(self.mock_ctrl.start(), timeout=2)
        except Exception as e:
            self.log.exception(e)
            raise

    def report_summary_state(self):
        super().report_summary_state()
        if self.connected != self.want_connection:
            if self.want_connection:
                asyncio.ensure_future(self.connect())
            else:
                asyncio.ensure_future(self.disconnect())

    def cancel_status_sleep(self):
        """Cancel the sleep between status updates in ``status_loop``.

        If connected this triggers an immediate status request.
        If disconnected this causes the status loop to quit
        and ``status_task`` to finish.
        If the status loop is not running then this has no effect.
        """
        if self.status_sleep_task is not None and not self.status_sleep_task.done():
            self.status_sleep_task.cancel()

    async def start(self):
        await super().start()
        self.evt_azimuthCommandedState.set_put(commandedState=AzimuthCommandedState.UNKNOWN,
                                               azimuth=math.nan, force_output=True)
        self.evt_dropoutDoorCommandedState.set_put(commandedState=ShutterDoorCommandedState.UNKNOWN,
                                                   force_output=True)
        self.evt_mainDoorCommandedState.set_put(commandedState=ShutterDoorCommandedState.UNKNOWN,
                                                force_output=True)

    async def status_loop(self):
        """Read and report status from the TCP/IP controller.
        """
        if self.status_sleep_task and not self.status_sleep_task.done():
            self.status_sleep_task.cancel()
        while self.connected:
            try:
                if self.n_short_status % self.short_per_full == 0:
                    self.n_short_status = 0
                    await self.run_command("+")
                else:
                    await self.run_command("?")
                self.n_short_status += 1
            except Exception as e:
                self.log.warning(f"Status request failed: {e}")
            try:
                self.status_sleep_task = await asyncio.sleep(self.status_interval)
            except asyncio.CancelledError:
                pass

    async def close_tasks(self):
        """Disconnect from the TCP/IP controller and stop the mock controller.
        """
        await super().close_tasks()
        await self.disconnect()
        await self.stop_mock_ctrl()

    async def stop_mock_ctrl(self):
        """Stop the mock controller, if present.

        Safe to call even if there is no mock controller.
        """
        mock_ctrl = self.mock_ctrl
        self.mock_ctrl = None
        if mock_ctrl:
            await mock_ctrl.stop()

    @property
    def want_connection(self):
        return self.summary_state in (salobj.State.DISABLED, salobj.State.ENABLED)

    @classmethod
    def add_arguments(cls, parser):
        super(ATDomeCsc, cls).add_arguments(parser)
        parser.add_argument("-i", "--index", type=int, default=1,
                            help="SAL index; use the default value "
                                 "unless you sure you know what you are doing")
        parser.add_argument("-s", "--simulate", action="store_true",
                            help="Run in simuation mode?")

    @classmethod
    def add_kwargs_from_args(cls, args, kwargs):
        super(ATDomeCsc, cls).add_kwargs_from_args(args, kwargs)
        kwargs["index"] = args.index
        kwargs["initial_simulation_mode"] = 1 if args.simulate else 0
