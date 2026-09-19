import socket
import sys

import pytest
import requests


@pytest.mark.parametrize("operation", [
    lambda sock: sock.connect(("198.51.100.1", 1883)),
    lambda sock: sock.connect_ex(("198.51.100.1", 1883)),
    lambda sock: sock.bind(("127.0.0.1", 0)),
    lambda sock: sock.sendto(b"offline", ("198.51.100.1", 1883)),
    lambda sock: socket.getaddrinfo("broker.invalid", 1883),
    lambda sock: socket.create_connection(("198.51.100.1", 1883)),
    lambda sock: requests.get("https://weather.invalid"),
    lambda sock: sys.modules["RPi.GPIO"].output(1, 1),
])
def test_io_guards_record_even_caught_attempts(operation, forbid_external_io):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(AssertionError, match="forbidden"):
            operation(sock)
    assert len(forbid_external_io) == 1
    forbid_external_io.clear()


def test_private_event_loop_socketpair_still_works(forbid_external_io):
    reader, writer = socket.socketpair()
    try:
        writer.send(b"offline")
        assert reader.recv(7) == b"offline"
        assert forbid_external_io == []
    finally:
        reader.close()
        writer.close()
