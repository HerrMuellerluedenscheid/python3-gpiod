"""
MIT License

Copyright (c) 2020 Hyeonki Hong <hhk7734@gmail.com>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
from ctypes import memmove, pointer, set_errno, sizeof
from datetime import datetime, timedelta
from errno import EBUSY, EINVAL, EIO, ENODEV, ENOENT, ENOTTY, EPERM
from fcntl import ioctl
from os import (
    access,
    close as os_close,
    lstat,
    major,
    minor,
    open as os_open,
    O_CLOEXEC,
    O_RDWR,
    read as os_read,
    R_OK,
    scandir,
)
from os.path import basename
import select
from select import POLLIN, POLLNVAL, POLLPRI
from stat import S_ISCHR
from typing import List

from .gpio_h import *

# pylint: disable=too-few-public-methods


# Forward declaration
class gpiod_chip:
    pass


# Forward declaration
class gpiod_line:
    pass


# Forward declaration
class gpiod_line_bulk:
    pass


GPIOD_LINE_BULK_MAX_LINES = 64


class gpiod_line_bulk:
    # pylint: disable=function-redefined
    def __init__(self):
        # gpiod_line_bulk_init(bulk)
        self._lines = []

    # pylint: disable=missing-function-docstring

    def add(self, line: gpiod_line):
        # gpiod_line_bulk_add(bulk, line)
        if self.num_lines < GPIOD_LINE_BULK_MAX_LINES:
            self._lines.append(line)

    @property
    def num_lines(self) -> int:
        # gpiod_line_bulk_num_lines(bulk)
        return len(self._lines)

    def __getitem__(self, offset):
        # gpiod_line_bulk_get_line(bulk, offset)
        return self._lines[offset]

    def __iter__(self):
        return iter(self._lines)


GPIOD_LINE_DIRECTION_INPUT = 1
GPIOD_LINE_DIRECTION_OUTPUT = 2

GPIOD_LINE_ACTIVE_STATE_HIGH = 1
GPIOD_LINE_ACTIVE_STATE_LOW = 2

GPIOD_LINE_REQUEST_DIRECTION_AS_IS = 1
GPIOD_LINE_REQUEST_DIRECTION_INPUT = 2
GPIOD_LINE_REQUEST_DIRECTION_OUTPUT = 3
GPIOD_LINE_REQUEST_EVENT_FALLING_EDGE = 4
GPIOD_LINE_REQUEST_EVENT_RISING_EDGE = 5
GPIOD_LINE_REQUEST_EVENT_BOTH_EDGES = 6

GPIOD_LINE_REQUEST_FLAG_OPEN_DRAIN = 0b001
GPIOD_LINE_REQUEST_FLAG_OPEN_SOURCE = 0b010
GPIOD_LINE_REQUEST_FLAG_ACTIVE_LOW = 0b100


class gpiod_line_request_config:
    def __init__(self):
        self.consumer = ""
        self.request_type = 0
        self.flags = 0


GPIOD_LINE_EVENT_RISING_EDGE = 1
GPIOD_LINE_EVENT_FALLING_EDGE = 2


class gpiod_line_event:
    def __init__(self):
        self.ts = None
        self.event_type = 0


# core.c

_LINE_FREE = 0
_LINE_REQUESTED_VALUES = 1
_LINE_REQUESTED_EVENTS = 2


class line_fd_handle:
    def __init__(self, fd):
        self.fd = fd

    def __del__(self):
        # line_fd_decref(line)
        os_close(self.fd)


class gpiod_line:
    # pylint: disable=function-redefined, too-many-instance-attributes
    def __init__(self, chip: gpiod_chip):
        self.offset = 0
        self.direction = 0
        self.active_state = 0
        self.used = False
        self.open_source = False
        self.open_drain = False
        self.state = 0
        self.up_to_date = False
        self.chip = chip
        self.fd_handle = None
        # size 32
        self.name = ""
        # size 32
        self.consumer = ""


class gpiod_chip:
    # pylint: disable=function-redefined
    def __init__(self, num_lines: int, fd: int, name: str, label: str):
        self.lines = [None] * num_lines
        self._num_lines = num_lines
        self._fd = fd
        # size 32
        self._name = name
        # size 32
        self._label = label

    # pylint: disable=missing-function-docstring

    @property
    def num_lines(self):
        # ::gpiod_chip_num_lines(chip)
        return self._num_lines

    @property
    def fd(self):
        return self._fd

    @property
    def name(self):
        # ::gpiod_chip_name(chip)
        return self._name

    @property
    def label(self):
        # ::gpiod_chip_label(chip)
        return self._label


# Function

# core.c


def _is_gpiochip_cdev(path: str) -> bool:
    try:
        statbuf = lstat(path)
    except FileNotFoundError:
        return False

    # Is it a character device?
    if not S_ISCHR(statbuf.st_mode):
        # Passing a file descriptor not associated with a character
        # device to ioctl() makes it set errno to ENOTTY. Let's do
        # the same in order to stay compatible with the versions of
        # libgpiod from before the introduction of this routine.
        set_errno(ENOTTY)
        return False

    # Do we have a corresponding sysfs attribute?
    name = basename(path)
    sysfsp = "/sys/bus/gpio/devices/{}/dev".format(name)
    if not access(sysfsp, R_OK):
        # This is a character device but not the one we're after.
        # Before the introduction of this function, we'd fail with
        # ENOTTY on the first GPIO ioctl() call for this file
        # descriptor. Let's stay compatible here and keep returning
        # the same error code.
        set_errno(ENOTTY)
        return False

    # Make sure the major and minor numbers of the character device
    # correspond with the ones in the dev attribute in sysfs.
    devstr = "{}:{}".format(major(statbuf.st_rdev), minor(statbuf.st_rdev))

    try:
        with open(sysfsp, "r") as fd:
            sysfsdev = fd.read(len(devstr))
    except FileNotFoundError:
        return False

    if sysfsdev != devstr:
        set_errno(ENODEV)
        return False

    return True


def gpiod_chip_open(path: str) -> gpiod_chip:
    """
    @brief Open a gpiochip by path.

    @param path: Path to the gpiochip device file.

    @return GPIO chip handle or None if an error occurred.
    """
    info = gpiochip_info()

    try:
        fd = os_open(path, O_RDWR | O_CLOEXEC)
    except FileNotFoundError:
        return None

    # We were able to open the file but is it really a gpiochip character
    # device?
    if not _is_gpiochip_cdev(path):
        os_close(fd)
        return None

    status = ioctl(fd, GPIO_GET_CHIPINFO_IOCTL, info)
    if status < 0:
        os_close(fd)
        return None

    if info.label[0] == "\0":
        label = "unknown"
    else:
        label = info.label.decode()

    return gpiod_chip(
        num_lines=info.lines, fd=fd, name=info.name.decode(), label=label
    )


def gpiod_chip_close(chip: gpiod_chip):
    """
    @brief Close a GPIO chip handle and release all allocated resources.

    @param chip: The GPIO chip object.
    """
    if len(chip.lines) > 0:
        for i in range(chip.num_lines):
            line = chip.lines[i]
            if line is not None:
                gpiod_line_release(line)

    os_close(chip.fd)
    # How to free the chip object?
    del chip


def gpiod_chip_get_line(chip: gpiod_chip, offset: int) -> gpiod_line:
    """
    @brief Get the handle to the GPIO line at given offset.

    @param chip:   The GPIO chip object.
    @param offset: The offset of the GPIO line.

    @return The GPIO line handle or None if an error occured.
    """
    if offset < 0 or offset >= chip.num_lines:
        set_errno(EINVAL)
        return None

    if chip.lines[offset] is None:
        line = gpiod_line(chip)
        line.fd_handle = None
        line.offset = offset

        chip.lines[offset] = line

    status = gpiod_line_update(chip.lines[offset])
    if status < 0:
        return None

    return chip.lines[offset]


def _line_maybe_update(line: gpiod_line):
    status = gpiod_line_update(line)
    if status < 0:
        line.up_to_date = False


def gpiod_line_update(line: gpiod_line) -> int:
    """
    @brief Re-read the line info.

    @param line: GPIO line object.

    @return 0 is the operation succeeds. In case of an error this routine
            returns -1 and sets the last error number.

    The line info is initially retrieved from the kernel by
    gpiod_chip_get_line(). Users can use this line to manually re-read the line
    info.
    """
    info = gpioline_info()

    info.line_offset = line.offset

    status = ioctl(line.chip.fd, GPIO_GET_LINEINFO_IOCTL, info)
    if status < 0:
        return -1

    line.direction = (
        GPIOD_LINE_DIRECTION_OUTPUT
        if info.flags & GPIOLINE_FLAG_IS_OUT
        else GPIOD_LINE_DIRECTION_INPUT
    )
    line.active_state = (
        GPIOD_LINE_ACTIVE_STATE_LOW
        if info.flags & GPIOLINE_FLAG_ACTIVE_LOW
        else GPIOD_LINE_ACTIVE_STATE_HIGH
    )
    line.used = bool(info.flags & GPIOLINE_FLAG_KERNEL)
    line.open_drain = bool(info.flags & GPIOLINE_FLAG_OPEN_DRAIN)
    line.open_source = bool(info.flags & GPIOLINE_FLAG_OPEN_SOURCE)

    line.name = info.name.decode()
    line.consumer = info.consumer.decode()

    line.up_to_date = True

    return 0


def _line_bulk_same_chip(bulk: gpiod_line_bulk) -> bool:
    if bulk.num_lines == 1:
        return True

    first_chip = bulk[0].chip

    for it in bulk:
        if it.chip != first_chip:
            set_errno(EINVAL)
            return False

    return True


def _line_bulk_all_requested(bulk: gpiod_line_bulk) -> bool:
    for it in bulk:
        if not gpiod_line_is_requested(it):
            set_errno(EPERM)
            return False

    return True


def _line_bulk_all_free(bulk: gpiod_line_bulk) -> bool:
    for it in bulk:
        if not gpiod_line_is_free(it):
            set_errno(EBUSY)
            return False

    return True


def _line_request_values(
    bulk: gpiod_line_bulk,
    config: gpiod_line_request_config,
    default_vals: List[int],
) -> int:
    if config.request_type != GPIOD_LINE_REQUEST_DIRECTION_OUTPUT and (
        config.flags
        & (
            GPIOD_LINE_REQUEST_FLAG_OPEN_DRAIN
            | GPIOD_LINE_REQUEST_FLAG_OPEN_SOURCE
        )
    ):
        set_errno(EINVAL)
        return -1

    if (config.flags & GPIOD_LINE_REQUEST_FLAG_OPEN_DRAIN) and (
        config.flags & GPIOD_LINE_REQUEST_FLAG_OPEN_SOURCE
    ):
        set_errno(EINVAL)
        return -1

    # pylint: disable=no-member
    req = gpiohandle_request()

    if config.flags & GPIOD_LINE_REQUEST_FLAG_OPEN_DRAIN:
        req.flags |= GPIOHANDLE_REQUEST_OPEN_DRAIN
    if config.flags & GPIOD_LINE_REQUEST_FLAG_OPEN_SOURCE:
        req.flags |= GPIOHANDLE_REQUEST_OPEN_SOURCE
    if config.flags & GPIOD_LINE_REQUEST_FLAG_ACTIVE_LOW:
        req.flags |= GPIOHANDLE_REQUEST_ACTIVE_LOW

    if config.request_type == GPIOD_LINE_REQUEST_DIRECTION_INPUT:
        req.flags |= GPIOHANDLE_REQUEST_INPUT
    elif config.request_type == GPIOD_LINE_REQUEST_DIRECTION_OUTPUT:
        req.flags |= GPIOHANDLE_REQUEST_OUTPUT

    req.lines = bulk.num_lines

    for i in range(bulk.num_lines):
        req.lineoffsets[i] = bulk[i].offset
        if (
            config.request_type == GPIOD_LINE_REQUEST_DIRECTION_OUTPUT
            and default_vals
        ):
            req.default_values[i] = 1 if default_vals[i] else 0

    if config.consumer:
        req.consumer_label = config.consumer[:32].encode()

    fd = bulk[0].chip.fd

    status = ioctl(fd, GPIO_GET_LINEHANDLE_IOCTL, req)
    if status < 0:
        return -1

    line_fd = line_fd_handle(req.fd)

    for it in bulk:
        it.state = _LINE_REQUESTED_VALUES
        # line_set_fd(line, line_fd) -> line_fd_incref(line)
        it.fd_handle = line_fd
        _line_maybe_update(it)

    return 0


def _line_request_event_single(
    line: gpiod_line, config: gpiod_line_request_config
) -> int:
    # pylint: disable=no-member
    req = gpioevent_request()
    if config.consumer:
        req.consumer_label = config.consumer[:32].encode()

    req.lineoffset = line.offset
    req.handleflags |= GPIOHANDLE_REQUEST_INPUT

    if config.flags & GPIOD_LINE_REQUEST_FLAG_OPEN_DRAIN:
        req.flags |= GPIOHANDLE_REQUEST_OPEN_DRAIN
    if config.flags & GPIOD_LINE_REQUEST_FLAG_OPEN_SOURCE:
        req.flags |= GPIOHANDLE_REQUEST_OPEN_SOURCE
    if config.flags & GPIOD_LINE_REQUEST_FLAG_ACTIVE_LOW:
        req.flags |= GPIOHANDLE_REQUEST_ACTIVE_LOW

    if config.request_type == GPIOD_LINE_REQUEST_EVENT_RISING_EDGE:
        req.eventflags |= GPIOEVENT_REQUEST_RISING_EDGE
    elif config.request_type == GPIOD_LINE_REQUEST_EVENT_FALLING_EDGE:
        req.eventflags |= GPIOEVENT_REQUEST_FALLING_EDGE
    elif config.request_type == GPIOD_LINE_REQUEST_EVENT_BOTH_EDGES:
        req.eventflags |= GPIOEVENT_REQUEST_BOTH_EDGES

    status = ioctl(line.chip.fd, GPIO_GET_LINEEVENT_IOCTL, req)
    if status < 0:
        return -1

    line_fd = line_fd_handle(req.fd)

    line.state = _LINE_REQUESTED_EVENTS
    line.fd_handle = line_fd
    _line_maybe_update(line)

    return 0


def _line_request_events(
    bulk: gpiod_line_bulk, config: gpiod_line_request_config
) -> int:
    for i in range(bulk.num_lines):
        status = _line_request_event_single(bulk[i], config)
        if status < 0:
            for j in range(i):
                gpiod_line_release(bulk[j])

            return -1

    return 0


def gpiod_line_request(
    line: gpiod_line, config: gpiod_line_request_config, default_val: int
) -> int:
    """
    @brief Reserve a single line.

    @param line:        GPIO line object.
    @param config:      Request options.
    @param default_val: Initial line value - only relevant if we're setting
                        the direction to output.

    @return 0 if the line was properly reserved. In case of an error this
    routine returns -1 and sets the last error number.

    If this routine succeeds, the caller takes ownership of the GPIO line until
    it's released.
    """
    bulk = gpiod_line_bulk()

    bulk.add(line)

    return gpiod_line_request_bulk(bulk, config, [default_val])


def _line_request_is_direction(request: int) -> bool:
    return request in [
        GPIOD_LINE_REQUEST_DIRECTION_AS_IS,
        GPIOD_LINE_REQUEST_DIRECTION_INPUT,
        GPIOD_LINE_REQUEST_DIRECTION_OUTPUT,
    ]


def _line_request_is_events(request: int) -> bool:
    return request in [
        GPIOD_LINE_REQUEST_EVENT_FALLING_EDGE,
        GPIOD_LINE_REQUEST_EVENT_RISING_EDGE,
        GPIOD_LINE_REQUEST_EVENT_BOTH_EDGES,
    ]


def gpiod_line_request_bulk(
    bulk: gpiod_line_bulk,
    config: gpiod_line_request_config,
    default_vals: List[int],
) -> int:
    """
    @brief Reserve a set of GPIO lines.

    @param bulk:         Set of GPIO lines to reserve.
    @param config:       Request options.
    @param default_vals: Initial line values - only relevant if we're setting
                         the direction to output.

    @return 0 if the all lines were properly requested. In case of an error
            this routine returns -1 and sets the last error number.

    If this routine succeeds, the caller takes ownership of the GPIO lines
    until they're released. All the requested lines must be prodivided by the
    same gpiochip.
    """
    if not _line_bulk_same_chip(bulk) or not _line_bulk_all_free(bulk):
        return -1

    if _line_request_is_direction(config.request_type):
        return _line_request_values(bulk, config, default_vals)
    if _line_request_is_events(config.request_type):
        return _line_request_events(bulk, config)

    set_errno(EINVAL)
    return -1


def gpiod_line_release(line: gpiod_line):
    """
    @brief Release a previously reserved line.

    @param line: GPIO line object.
    """
    bulk = gpiod_line_bulk()

    bulk.add(line)

    gpiod_line_release_bulk(bulk)


def gpiod_line_release_bulk(bulk: gpiod_line_bulk):
    """
    @brief Release a set of previously reserved lines.

    @param bulk: Set of GPIO lines to release.

    If the lines were not previously requested together, the behavior is
    undefined.
    """
    for it in bulk:
        # line_fd_decref(line)
        it.fd_handle = None
        it.state = _LINE_FREE


def gpiod_line_is_requested(line: gpiod_line) -> bool:
    """
    @brief Check if the calling user has ownership of this line.

    @param line: GPIO line object.

    @return True if given line was requested, false otherwise.
    """
    return (
        line.state == _LINE_REQUESTED_VALUES
        or line.state == _LINE_REQUESTED_EVENTS
    )


def gpiod_line_is_free(line: gpiod_line) -> bool:
    """
    @brief Check if the calling user has neither requested ownership of this
           line nor configured any event notifications.

    @param line: GPIO line object.

    @return True if given line is free, false otherwise.
    """
    return line.state == _LINE_FREE


def gpiod_line_get_value(line: gpiod_line) -> int:
    """
    @brief Read current value of a single GPIO line.

    @param line: GPIO line object.

    @return 0 or 1 if the operation succeeds. On error this routine returns -1
            and sets the last error number.
    """
    bulk = gpiod_line_bulk()
    value = [0]

    bulk.add(line)

    status = gpiod_line_get_value_bulk(bulk, value)
    if status < 0:
        return -1

    return value[0]


def gpiod_line_get_value_bulk(bulk: gpiod_line_bulk, values: List[int]) -> int:
    """
    @brief Read current values of a set of GPIO lines.

    @param bulk:   Set of GPIO lines to reserve.
    @param values: An array big enough to hold line_bulk->num_lines values.

    @return 0 is the operation succeeds. In case of an error this routine
            returns -1 and sets the last error number.

    If succeeds, this routine fills the values array with a set of values in
    the same order, the lines are added to line_bulk. If the lines were not
    previously requested together, the behavior is undefined.
    """
    data = gpiohandle_data()

    if not _line_bulk_same_chip(bulk) or not _line_bulk_all_requested(bulk):
        return -1

    fd = bulk[0].fd_handle.fd

    status = ioctl(fd, GPIOHANDLE_GET_LINE_VALUES_IOCTL, data)
    if status < 0:
        return -1

    for i in range(bulk.num_lines):
        values[i] = data.values[i]

    return 0


def gpiod_line_set_value(line: gpiod_line, value: int) -> int:
    """
    @brief Set the value of a single GPIO line.

    @param line:  GPIO line object.
    @param value: New value.

    @return 0 is the operation succeeds. In case of an error this routine
            returns -1 and sets the last error number.
    """
    bulk = gpiod_line_bulk()

    bulk.add(line)

    return gpiod_line_set_value_bulk(bulk, [value])


def gpiod_line_set_value_bulk(bulk: gpiod_line_bulk, values: List[int]) -> int:
    """
    @brief Set the values of a set of GPIO lines.

    @param bulk:   Set of GPIO lines to reserve.
    @param values: An array holding line_bulk->num_lines new values for lines.

    @return 0 is the operation succeeds. In case of an error this routine
            returns -1 and sets the last error number.

    If the lines were not previously requested together, the behavior is
    undefined.
    """
    data = gpiohandle_data()

    if not _line_bulk_same_chip(bulk) or not _line_bulk_all_requested(bulk):
        return -1

    for i in range(bulk.num_lines):
        data.values[i] = 1 if values[i] else 0

    fd = bulk[0].fd_handle.fd

    status = ioctl(fd, GPIOHANDLE_SET_LINE_VALUES_IOCTL, data)
    if status < 0:
        return -1

    return 0


def gpiod_line_event_wait(line: gpiod_line, timeout: timedelta) -> int:
    """
    @brief Wait for an event on a single line.

    @param line:    GPIO line object.
    @param timeout: Wait time limit.

    @return 0 if wait timed out, -1 if an error occurred, 1 if an event
            occurred.
    """
    bulk = gpiod_line_bulk()

    bulk.add(line)

    return gpiod_line_event_wait_bulk(bulk, timeout, None)


def gpiod_line_event_wait_bulk(
    bulk: gpiod_line_bulk, timeout: timedelta, event_bulk: gpiod_line_bulk
) -> int:
    """
    @brief Wait for events on a set of lines.

    @param bulk:       Set of GPIO lines to monitor.
    @param timeout:    Wait time limit.
    @param event_bulk: Bulk object in which to store the line handles on which
                       events occurred. Can be None.

    @return 0 if wait timed out, -1 if an error occurred, 1 if at least one
            event occurred.
    """
    if not _line_bulk_same_chip(bulk) or not _line_bulk_all_requested(bulk):
        return -1

    poll = select.poll()
    fd_to_line = {}

    for it in bulk:
        poll.register(it.fd_handle.fd, POLLIN | POLLPRI)
        fd_to_line[it.fd_handle.fd] = it

    timeout_ms = (
        (timeout.days * 86_400_000)
        + (timeout.seconds * 1_000)
        + (timeout.microseconds / 1000.0)
    )

    revents = poll.poll(timeout_ms)

    if revents is None:
        return -1
    if len(revents) == 0:
        return 0

    for it in revents:
        fd = it[0]
        revent = it[1]
        if revent:
            if revent & POLLNVAL:
                set_errno(EINVAL)
                return -1

            if event_bulk is not None:
                event_bulk.add(fd_to_line[fd])

    return 1


def gpiod_line_event_read(line: gpiod_line, event: gpiod_line_event) -> int:
    """
    @brief Read the last event from the GPIO line.

    @param line:  GPIO line object.
    @param event: Buffer to which the event data will be copied.

    @return 0 if the event was read correctly, -1 on error.

    @note This function will block if no event was queued for this line.
    """
    if line.state != _LINE_REQUESTED_EVENTS:
        set_errno(EPERM)
        return -1

    return gpiod_line_event_read_fd(line.fd_handle.fd, event)


def gpiod_line_event_get_fd(line: gpiod_line) -> int:
    """
    @brief Get the event file descriptor.

    @param line: GPIO line object.

    @return Number of the event file descriptor or -1 if the user tries to
            retrieve the descriptor from a line that wasn't configured for
            event monitoring.

    Users may want to poll the event file descriptor on their own. This routine
    allows to access it.
    """
    if line.state != _LINE_REQUESTED_EVENTS:
        set_errno(EPERM)
        return -1

    return line.fd_handle.fd


def gpiod_line_event_read_fd(fd: int, event: gpiod_line_event) -> int:
    """
    @brief Read the last GPIO event directly from a file descriptor.

    @param fd:    File descriptor.
    @param event: Buffer in which the event data will be stored.

    @return 0 if the event was read correctly, -1 on error.

    Users who directly poll the file descriptor for incoming events can also
    directly read the event data from it using this routine. This function
    translates the kernel representation of the event to the libgpiod format.
    """
    evdata = gpioevent_data()

    try:
        rd = os_read(fd, sizeof(evdata))
    except OSError:
        return -1

    print(rd)

    if len(rd) != sizeof(evdata):
        set_errno(EIO)
        return -1

    memmove(pointer(evdata), rd, sizeof(evdata))

    event.event_type = (
        GPIOD_LINE_EVENT_RISING_EDGE
        if evdata.id == GPIOEVENT_EVENT_RISING_EDGE
        else GPIOD_LINE_EVENT_FALLING_EDGE
    )

    sec = evdata.timestamp / 1_000_000_000
    event.ts = datetime(year=1970, month=1, day=1) + timedelta(
        days=sec // 86400,
        seconds=sec % 86400,
        microseconds=(evdata.timestamp % 1_000_000_000) // 1000,
    )

    return 0


# helpers.c


def gpiod_chip_open_by_name(name: str) -> gpiod_chip:
    """
    @brief Open a gpiochip by name.

    @param name: Name of the gpiochip to open.

    @return GPIO chip handle or None if an error occurred.
    """
    return gpiod_chip_open("/dev/" + str(name))


def gpiod_chip_open_by_number(num: int) -> gpiod_chip:
    """
    @brief Open a gpiochip by number.

    @param num: Number of the gpiochip.

    @return GPIO chip handle or None if an error occurred.
    """
    return gpiod_chip_open("/dev/gpiochip" + str(num))


def gpiod_chip_open_by_label(label: str) -> gpiod_chip:
    """
    @brief Open a gpiochip by label.

    @param label: Label of the gpiochip to open.

    @return GPIO chip handle or None if the chip with given label was not found
            or an error occured.

    @note If the chip cannot be found but no other error occurred, errno is set
          to ENOENT.
    """
    chip_iter = iter(gpiod_chip_iter())
    if chip_iter is None:
        return None

    for chip in chip_iter:
        if chip.label == label:
            # gpiod_chip_iter_free_noclose
            return chip

    set_errno(ENOENT)
    # gpiod_chip_iter_free

    return None


def gpiod_chip_open_lookup(descr) -> gpiod_chip:
    """
    @brief Open a gpiochip based on the best guess what the path is.

    @param descr: String describing the gpiochip.

    @return GPIO chip handle or None if an error occurred.

    This routine tries to figure out whether the user passed it the path to the
    GPIO chip, its name, label or number as a string. Then it tries to open it
    using one of the gpiod_chip_open** variants.
    """
    try:
        num = int(descr)
        return gpiod_chip_open_by_number(num)
    except ValueError:
        pass

    chip = gpiod_chip_open_by_label(descr)

    if not bool(chip):
        if descr[:5] != "/dev/":
            return gpiod_chip_open_by_name(descr)

        return gpiod_chip_open(descr)

    return chip


def gpiod_chip_find_line(chip: gpiod_chip, name: str) -> gpiod_line:
    pass


# iter.c


class gpiod_chip_iter:
    def __init__(self):
        self.chips = []
        self.offset = 0

    def __iter__(self):
        """
        gpiod_chip_iter_new()

        @brief Create a new gpiochip iterator.

        @return A new chip iterator object or None if an error occurred.
        """
        dirs = []
        for it in scandir("/dev"):
            if it.name[:8] == "gpiochip":
                dirs.append(it.path)

        if len(dirs) == 0:
            return None

        for it in dirs:
            chip = gpiod_chip_open(it)
            if chip is None:
                for c in self.chips:
                    gpiod_chip_close(c)
                self.chips.clear()
                return None

            self.chips.append(chip)

        return self

    def __next__(self):
        """
        gpiod_chip_iter_next()

        @brief Get the next gpiochip handle.

        @return The next open gpiochip handle or raise StopIteration if no more
                chips are present in the system.

        @note The previous chip handle will be closed.
        """
        if self.offset > 0:
            gpiod_chip_close(self.chips[self.offset - 1])
            self.chips[self.offset - 1] = None

        # gpiod_chip_iter_next_noclose
        if self.offset < len(self.chips):
            index = self.offset
            self.offset += 1
            return self.chips[index]

        raise StopIteration
