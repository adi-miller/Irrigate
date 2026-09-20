import ipaddress
import socket
import sys
import threading
import types

import pytest
import requests
from paho.mqtt import client as mqtt_client


def pytest_addoption(parser):
    parser.addoption("--record-contracts", action="store_true", default=False)


@pytest.fixture(autouse=True)
def forbid_external_io(monkeypatch):
    violations = []
    context = threading.local()

    def forbidden(*args, **kwargs):
        violations.append("Unexpected external network or physical GPIO access")
        raise AssertionError("External network or physical GPIO access is forbidden in tests")

    socketpair = socket.socketpair

    def internal_socketpair(*args, **kwargs):
        previous = getattr(context, "socketpair", False)
        context.socketpair = True
        try:
            return socketpair(*args, **kwargs)
        finally:
            context.socketpair = previous

    def guarded_socket_call(original):
        def guarded(sock, address, *args, **kwargs):
            # Windows event loops build their private wake-up pair over loopback.
            if getattr(context, "socketpair", False) and isinstance(address, tuple):
                if ipaddress.ip_address(address[0]).is_loopback:
                    return original(sock, address, *args, **kwargs)
            return forbidden()
        return guarded

    monkeypatch.setattr(socket, "socketpair", internal_socketpair)
    for name in ("connect", "connect_ex", "bind"):
        monkeypatch.setattr(socket.socket, name, guarded_socket_call(getattr(socket.socket, name)))
    monkeypatch.setattr(socket.socket, "sendto", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(mqtt_client.Client, "connect", forbidden)
    monkeypatch.setattr(mqtt_client.Client, "connect_async", forbidden)
    monkeypatch.setattr(mqtt_client.Client, "publish", forbidden)
    gpio = types.ModuleType("RPi.GPIO")
    for name, value in {"BCM": 11, "OUT": 0, "LOW": 0, "HIGH": 1}.items():
        setattr(gpio, name, value)
    for name in ("setmode", "setwarnings", "setup", "output", "cleanup"):
        setattr(gpio, name, forbidden)
    rpi = types.ModuleType("RPi")
    rpi.GPIO = gpio
    monkeypatch.setitem(sys.modules, "RPi", rpi)
    monkeypatch.setitem(sys.modules, "RPi.GPIO", gpio)
    yield violations
    assert not violations, "An application catch suppressed a forbidden network/GPIO attempt"
