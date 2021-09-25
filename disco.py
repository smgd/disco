import asyncio
import math
import signal
import subprocess
import time
import uuid
import wave
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import cast, Type, Any

import dbus
import pulsectl

DBUS_BACKENDS = {
    'systemd': {
        'name': 'org.freedesktop.login1',
        'path': '/org/freedesktop/login1/session/auto',
        'interface': 'org.freedesktop.login1.session',
        'getter': 'GetBrightness',
        'setter': 'SetBrightness',
        # 'max_getter': 'GetMaxBrightness',
    },
    'gnome': {
        'name': 'org.gnome.SettingsDaemon.Power',
        'path': '/org/gnome/SettingsDaemon/Power',
        'interface': 'org.gnome.SettingsDaemon.Power.Screen',
        'getter': 'GetPercentage',
        'setter': 'SetPercentage',
    },
    'upower': {
        'name': 'org.freedesktop.UPower',
        'path': '/org/freedesktop/UPower/KbdBacklight',
        'interface': 'org.freedesktop.UPower.KbdBacklight',
        'getter': 'GetBrightness',
        'setter': 'SetBrightness',
        'max_getter': 'GetMaxBrightness',
    },
}


class DBusBrightnessManager:
    def __init__(self, name, path, interface, getter, setter, max_getter=None) -> None:
        self.bus = dbus.SystemBus()
        self.proxy = self.bus.get_object(name, path)
        self.iface = dbus.Interface(self.proxy, interface)
        self.getter = getattr(self.iface, getter)
        self.setter = getattr(self.iface, setter)
        self.max_getter = getattr(self.iface, max_getter) if max_getter else (lambda: 100)

    @property
    def current(self) -> int:
        return self.getter()

    @current.setter
    def current(self, value: int) -> None:
        self.setter(value)

    @property
    def maximum(self) -> int:
        return self.max_getter()

    @property
    def value(self) -> Decimal:
        return Decimal(self.current) / Decimal(self.maximum)

    @value.setter
    def value(self, v) -> None:
        self.current = int(v * self.maximum)


class FileVar:
    def __init__(self, path: str, type_: Type[Any] = str) -> None:
        self.path: Path = Path(path)
        self.type_ = type_

    def get(self):
        return self.type_(self.path.read_text())

    def set(self, value):
        return self.path.write_text(str(value))


class FileProperty:
    def __init__(self, var: FileVar) -> None:
        self.var = var

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return self.var.get()

    def __set__(self, instance, value):
        self.var.set(value)


class ScreenBrightness:
    maximal = FileProperty(FileVar('/sys/class/backlight/intel_backlight/max_brightness', int))
    current = FileProperty(FileVar('/sys/class/backlight/intel_backlight/brightness', int))

    @property
    def value(self) -> Decimal:
        return Decimal(self.current) / Decimal(self.maximal)

    @value.setter
    def value(self, v) -> None:
        self.current = int(v * self.maximal)


def f(v, for_keyboard=False):
    if for_keyboard:
        return 0.05 + math.sin(0.5 * math.pi + 2 * math.pi * v)
    return 0.05 + 0.95 * (0.5 + 0.45 * math.sin(2 * math.pi * v))


def itertime():
    return iter(time.monotonic, 0)


class Pulse(pulsectl.Pulse):
    async def __aenter__(self):
        await asyncio.to_thread(self.__enter__)
        return self

    async def __aexit__(self, *exc_info):
        await asyncio.to_thread(self.__exit__, *exc_info)

    async def upload_sample(self, filename, name):
        return await asyncio.to_thread(self._upload_sample, filename, name)

    @staticmethod
    def _upload_sample(filename, name):
        with subprocess.Popen(['pactl', 'upload-sample', filename, name]) as proc:
            proc.wait()

    async def play_sample(self, name, *__, **___):
        return await asyncio.to_thread(super().play_sample, name, *__, **___)


def read_wav_info(path) -> wave._wave_params:
    with wave.open(path) as wav:
        return wav.getparams()


async def keyboard_alert(event):
    brightness = DBusBrightnessManager(**DBUS_BACKENDS['upower'])
    for v in map(partial(f, for_keyboard=True), itertime()):
        await event.wait()
        await asyncio.to_thread(setattr, brightness, 'value', v)
        await asyncio.sleep(1 / 60)


async def screen_alert(event):
    brightness = ScreenBrightness()
    for v in map(f, itertime()):
        await event.wait()
        await asyncio.to_thread(setattr, brightness, 'value', v)
        await asyncio.sleep(1 / 60)


async def audio_alert(event, path='alert.wav'):
    async with Pulse() as pa:
        sample_name = str(uuid.uuid4())

        await pa.upload_sample(path, sample_name)
        info = await asyncio.to_thread(read_wav_info, path)
        delay = info.nframes / info.framerate
        while True:
            await event.wait()
            await pa.play_sample(sample_name)
            await asyncio.sleep(delay)


def toggle(event):
    if event.is_set():
        event.clear()
    else:
        event.set()


async def main():
    event = asyncio.Event()
    event.set()
    loop = asyncio.get_running_loop()
    current_task = asyncio.current_task()
    loop.add_signal_handler(signal.SIGALRM, lambda *_: toggle(event))
    loop.add_signal_handler(signal.SIGINT, lambda *_: loop.call_soon(current_task.cancel))

    try:
        await asyncio.gather(audio_alert(event), keyboard_alert(event), screen_alert(event))
    except asyncio.CancelledError:
        exit()

if __name__ == '__main__':
    asyncio.run(main(), debug=cast(bool, int(True)))
