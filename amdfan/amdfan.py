""" Main amdfan script """
# noqa: E501
import logging
import os
import re
import sys
import time
import yaml
import numpy as np
import click

from rich import console
from rich.traceback import install
from rich.prompt import Prompt
from rich.table import Table
from rich.live import Live

# from rich import inspect # can remove this after debugging
from rich.logging import RichHandler

install()  # install traceback formatter

CONFIG_LOCATIONS = [
    "/etc/amdgpu-fan.yml",
    "/home/j/amdgpu-fan.yml",  # remove later
]

DEBUG = bool(os.environ.get("DEBUG", False))

ROOT_DIR = "/sys/class/drm"
HWMON_DIR = "device/hwmon"

LOGGER = logging.getLogger("rich")

logging.basicConfig(
    level="NOTSET",
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True)],
)

c = console.Console(style="green on black")


class Card:
    """
    This class is used to map to each card that supports HWMON
    """

    HWMON_REGEX = r"^hwmon\d$"
    AMD_FIELDS = ["temp1_input", "pwm1_max", "pwm1_min", "pwm1_enable", "pwm1"]

    def __init__(self, card_id):
        self._id = card_id

        for node in os.listdir(os.path.join(ROOT_DIR, self._id, HWMON_DIR)):
            if re.match(self.HWMON_REGEX, node):
                self._monitor = node
        self._endpoints = self._load_endpoints()

    def _verify_card(self):
        for endpoint in self.AMD_FIELDS:
            if endpoint not in self._endpoints:
                LOGGER.info(
                        "skipping card: %s missing endpoint %s",
                        self._id, endpoint
                        )
                raise FileNotFoundError

    def _load_endpoints(self):
        _endpoints = {}
        _dir = os.path.join(ROOT_DIR, self._id, HWMON_DIR, self._monitor)
        for endpoint in os.listdir(_dir):
            if endpoint not in ("device", "power", "subsystem", "uevent"):
                _endpoints[endpoint] = os.path.join(_dir, endpoint)
        return _endpoints

    def read_endpoint(self, endpoint):
        with open(self._endpoints[endpoint], "r") as e:
            return e.read()

    def write_endpoint(self, endpoint, data):
        try:
            with open(self._endpoints[endpoint], "w") as e:
                return e.write(str(data))
        except PermissionError:
            LOGGER.error(
                    "Failed writing to devfs file, are you running as root?"
                    )
            sys.exit(1)

    @property
    def fan_speed(self):
        try:
            return int(self.read_endpoint("fan1_input"))
        except KeyError:  # better to return no speed then explode
            return 0

    @property
    def gpu_temp(self):
        return float(self.read_endpoint("temp1_input")) / 1000

    @property
    def fan_max(self):
        return int(self.read_endpoint("pwm1_max"))

    @property
    def fan_min(self):
        return int(self.read_endpoint("pwm1_min"))

    def set_system_controlled_fan(self, state):

        system_controlled_fan = 2
        manual_control = 1

        self.write_endpoint(
            "pwm1_enable", system_controlled_fan if state else manual_control
        )

    def set_fan_speed(self, speed):
        if speed >= 100:
            speed = self.fan_max
        elif speed <= 0:
            speed = self.fan_min
        else:
            speed = self.fan_max / 100 * speed
        self.set_system_controlled_fan(False)
        return self.write_endpoint("pwm1", int(speed))


class Scanner:
    """ Used to scan the available cards to see if they are usable """

    CARD_REGEX = r"^card\d$"

    def __init__(self, cards=None):
        self.cards = self._get_cards(cards)

    def _get_cards(self, cards_to_scan):
        """
        only directories in ROOT_DIR that are card1, card0, card3 etc.
        :return: a list of initialized Card objects
        """
        cards = {}
        for node in os.listdir(ROOT_DIR):
            if re.match(self.CARD_REGEX, node):
                if cards_to_scan and node.lower() not in [
                    c.lower() for c in cards_to_scan
                ]:
                    continue
                try:
                    cards[node] = Card(node)
                except FileNotFoundError:
                    # if card lacks hwmon or required devfs files, its not
                    # amdgpu, and definitely not compatible with this software
                    continue
        return cards


class FanController:
    """ Used to apply the curve at regular intervals """

    def __init__(self, config):
        self._scanner = Scanner(config.get("cards"))
        if len(self._scanner.cards) < 1:
            LOGGER.error("no compatible cards found, exiting")
            sys.exit(1)
        self.curve = Curve(config.get("speed_matrix"))
        self._frequency = 1

    def main(self):
        LOGGER.info("starting amdgpu-fan")
        while True:
            for name, card in self._scanner.cards.items():
                temp = card.gpu_temp
                speed = int(self.curve.get_speed(int(temp)))
                if speed < 0:
                    speed = 0

                LOGGER.debug(
                    "%s: Temp %d, \
                            Setting fan speed to: %d, \
                            fan speed %d, \
                            min: %d, max: %d",
                    name,
                    temp,
                    speed,
                    card.fan_speed,
                    card.fan_min,
                    card.fan_max,
                )

                card.set_fan_speed(speed)
            time.sleep(self._frequency)


def load_config(path):
    LOGGER.debug("loading config from %s", path)
    with open(path) as config_file:
        return yaml.safe_load(config_file)


@click.command()
@click.option(
        '--daemon/--monitor',
        default=False,
        help='Run as daemon applying the fan curve')
def cli(daemon):
    if daemon:
        run_as_daemon()
    else:
        monitor()


def run_as_daemon():

    default_fan_config = """#Fan Control Matrix. [<Temp in C>,<Fanspeed in %>]
speed_matrix:
- [4, 4]
- [30, 33]
- [45, 50]
- [60, 66]
- [65, 69]
- [70, 75]
- [75, 89]
- [80, 100]

# Current Min supported value is 4 due to driver bug
# optional
# cards:
# can be any card returned from `ls /sys/class/drm | grep "^card[[:digit:]]$"`
# - card0
"""
    config = None
    for location in CONFIG_LOCATIONS:
        if os.path.isfile(location):
            config = load_config(location)
            break

    if config is None:
        LOGGER.info(
                "no config found, creating one in %s", CONFIG_LOCATIONS[-1]
                )
        with open(CONFIG_LOCATIONS[-1], "w") as config_file:
            config_file.write(default_fan_config)
            config_file.flush()

        config = load_config(CONFIG_LOCATIONS[-1])

    FanController(config).main()


class Curve:
    """
    creates a fan curve based on user defined points
    """

    def __init__(self, points: list):
        self.points = np.array(points)
        self.temps = self.points[:, 0]
        self.speeds = self.points[:, 1]

        if np.min(self.speeds) < 0:
            raise ValueError(
                "Fan curve contains negative speeds, \
                        speed should be in [0,100]"
            )
        if np.max(self.speeds) > 100:
            raise ValueError(
                "Fan curve contains speeds greater than 100, \
                        speed should be in [0,100]"
            )
        if np.any(np.diff(self.temps) <= 0):
            raise ValueError(
                "Fan curve points should be strictly monotonically increasing, \
                        configuration error ?"
            )
        if np.any(np.diff(self.speeds) < 0):
            raise ValueError(
                "Curve fan speeds should be monotonically increasing, \
                        configuration error ?"
            )
        if np.min(self.speeds) <= 3:
            raise ValueError('Lowest speed value to be set to 4')  # Driver BUG

    def get_speed(self, temp):
        """
        returns a speed for a given temperature
        :param temp: int
        :return:
        """

        return np.interp(x=temp, xp=self.temps, fp=self.speeds)


def test_curve():
    curve = Curve([[10, 10], [20, 20], [30, 30], [70, 80], [80, 100]])
    c.print(curve.get_speed(60))
    c.print(curve.get_speed(50))
    c.print(curve.get_speed(80))
    c.print(curve.get_speed(0))


def show_table(cards):
    table = Table(title="amdgpu")
    table.add_column("Card")
    table.add_column("fan_speed (RPM)")
    table.add_column("gpu_temp ℃")
    for card in cards:
        fan_speed = cards.get(card).fan_speed
        gpu_temp = cards.get(card).gpu_temp
        table.add_row(f"{card}", f"{fan_speed}", f"{gpu_temp}")
    return table


def monitor():
    c.print("AMD Fan Control")
    command = Prompt.ask(
        "Please select get or set", choices=["get", "set"], default="get"
    )
    scanner = Scanner()
    if command == "get":
        with Live(refresh_per_second=4) as live:
            for _ in range(40):
                time.sleep(0.4)
                live.update(show_table(scanner.cards))

    elif command == "set":
        card_to_set = Prompt.ask("Which card?", choices=scanner.cards.keys())
        while True:
            input_fan_speed = Prompt.ask(
                    "Fan speed, [1..100]% or 'auto'", default="auto"
                    )

            if input_fan_speed.isdigit():
                if int(input_fan_speed) >= 1 and int(input_fan_speed) <= 100:
                    LOGGER.info("good %d", input_fan_speed)
                    break
            elif input_fan_speed == "auto":
                LOGGER.info("good %d", input_fan_speed)
                break
            c.print("maybe try picking one of the options")

        selected_card = scanner.cards.get(card_to_set)
        if not input_fan_speed.isdigit() and input_fan_speed == "auto":
            LOGGER.info("Setting fan speed to system controlled")
            selected_card.set_system_controlled_fan(True)
        else:
            LOGGER.info("Setting fan speed to %d", input_fan_speed)
            c.print(selected_card.set_fan_speed(int(input_fan_speed)))
    sys.exit(1)
