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

__all__ = ["MoveCode"]

import enum


class MoveCode(enum.IntFlag):
    AZIMUTH_POSITIVE = 0x01
    AZIMUTH_NEGATIVE = 0x02
    MAIN_DOOR_CLOSING = 0x04
    MAIN_DOOR_OPENING = 0x08
    DROPOUT_DOOR_CLOSING = 0x10
    DROPOUT_DOOR_OPENING = 0x20
    AZIMUTH_HOMING = 0x40
    ESTOP = 0x80
