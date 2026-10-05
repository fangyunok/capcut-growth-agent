"""Persistent SSH port forwarder using paramiko.

Forwards a local TCP port to a remote host:port over a single long-lived
SSH session. Unlike ``ssh -fN -L``, this process keeps retrying the SSH
connection if it drops and never lets the OS kill it on idle.

Usage::

    python scripts/ssh_tunnel.py \\
        --key ~/.ssh/claim_studio_autodl \\
        --ssh-host connect.nmb2.seetacloud.com --ssh-port 26277 \\
        --ssh-user root \\
        --local-host 127.0.0.1 --local-port 11435 \\
        --remote-host 127.0.0.1 --remote-port 11434

Listens forever. Send SIGINT to stop.
"""

from __future__ import annotations

import argparse
import logging
import select
import socket
import threading
import time
from pathlib import Path

import paramiko


logging.basicConfig(level=logging.INFO, format="%(asctime)s [tunnel] %(message)s")
log = logging.getLogger("tunnel")


class _ForwardHandler:
    """Marker kept for documentation. The actual forwarding happens in ``_pump``."""


def _open_channel(client: paramiko.SSHClient, remote_host: str, remote_port: int):
    transport = client.get_transport()
    return transport.open_channel(
        kind="direct-tcpip",
        dest_addr=(remote_host, remote_port),
        src_addr=("127.0.0.1", 0),
    )


def _load_private_key(path: str):
    """Load any of the key types paramiko supports from a file."""
    candidates = [
        paramiko.Ed25519Key,
        paramiko.RSAKey,
        paramiko.ECDSAKey,
    ]
    for cls in candidates:
        try:
            return cls.from_private_key_file(path)
        except paramiko.ssh_exception.SSHException:
            continue
    raise RuntimeError(f"could not parse SSH key file {path!r}")


def _serve_once(args) -> bool:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    pkey = _load_private_key(args.key)
    client.connect(
        hostname=args.ssh_host,
        port=args.ssh_port,
        username=args.ssh_user,
        pkey=pkey,
        allow_agent=False,
        look_for_keys=False,
        timeout=10,
    )
    transport = client.get_transport()
    transport.set_keepalive(10)
    log.info("SSH connected to %s:%d", args.ssh_host, args.ssh_port)

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.local_host, args.local_port))
    server.listen(64)
    log.info("listening on %s:%d", args.local_host, args.local_port)

    server.settimeout(1.0)
    try:
        while True:
            try:
                sock, peer = server.accept()
            except socket.timeout:
                continue
            try:
                chan = _open_channel(client, args.remote_host, args.remote_port)
            except (paramiko.ssh_exception.SSHException, socket.error, OSError) as exc:
                # SSH transport died (idle-timeout kicked, server reset, etc.).
                # Bail out of this serve loop so the outer `while True` can
                # reconnect instead of becoming a zombie that accepts TCP but
                # can never open a channel again.
                log.warning("SSH channel open failed for %s: %s; will reconnect", peer, exc)
                sock.close()
                return True
            except Exception as exc:
                log.warning("could not open channel for %s: %s", peer, exc)
                sock.close()
                continue

            t = threading.Thread(
                target=_pump,
                args=(sock, chan, peer),
                daemon=True,
            )
            t.start()
    except KeyboardInterrupt:
        log.info("shutting down")
        return False
    finally:
        try:
            server.close()
        except Exception:
            pass
        client.close()
        client.close()


def _pump(sock, chan, peer):
    try:
        while True:
            r, _, _ = select.select([sock, chan], [], [], 60)
            if sock in r:
                data = sock.recv(4096)
                if not data:
                    break
                chan.sendall(data)
            if chan in r:
                data = chan.recv(4096)
                if not data:
                    break
                sock.sendall(data)
    except Exception as exc:
        log.debug("pump %s exit: %s", peer, exc)
    finally:
        for closer in (sock, chan):
            try:
                closer.close()
            except Exception:
                pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", default=str(Path.home() / ".ssh" / "claim_studio_autodl"))
    parser.add_argument("--ssh-host", default="connect.nmb2.seetacloud.com")
    parser.add_argument("--ssh-port", type=int, default=26277)
    parser.add_argument("--ssh-user", default="root")
    parser.add_argument("--local-host", default="127.0.0.1")
    parser.add_argument("--local-port", type=int, default=11435)
    parser.add_argument("--remote-host", default="127.0.0.1")
    parser.add_argument("--remote-port", type=int, default=11434)
    args = parser.parse_args()

    while True:
        try:
            _serve_once(args)
            return 0
        except (paramiko.SSHException, socket.error, OSError) as exc:
            log.warning("SSH connection lost: %s; reconnecting in 3s", exc)
            time.sleep(3)


if __name__ == "__main__":
    raise SystemExit(main())