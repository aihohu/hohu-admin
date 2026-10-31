"""Local PostgreSQL v3 transport faults; never fabricate a commit exception.

Only message type, backend PID and COMMIT disposition are retained. Startup,
password, statement parameters and returned business rows are not recorded.
"""

import asyncio
import struct
from contextlib import suppress
from dataclasses import dataclass, field


@dataclass
class CommitFault:
    stage: str
    mode: str
    reached: asyncio.Event = field(default_factory=asyncio.Event)
    sent: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    events: list[str] = field(default_factory=list)
    backend_pid: int | None = None


class PgFaultProxy:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.connections = {}
        self.tasks = set()
        self.faults = []
        self.unavailable = False

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        self.listen_port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_exc):
        self.server.close()
        await self.server.wait_closed()
        for fault in self.faults:
            fault.release.set()
        await self.disconnect_all()

    async def disconnect_all(self):
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def arm(self, pid, fault):
        fault.backend_pid = pid
        connection = self.connections[pid]
        assert connection["fault"] is None
        connection["fault"] = fault
        self.faults.append(fault)

    async def _accept(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        upstream = None
        children = []
        state = {"pid": None, "fault": None, "committing": False}
        try:
            if self.unavailable:
                return
            remote, upstream = await asyncio.open_connection(self.host, self.port)
            # Startup has no one-byte type. SSL/GSS are refused only by this
            # loopback test proxy; the client explicitly disables encryption.
            while True:
                header = await reader.readexactly(4)
                length = struct.unpack("!I", header)[0]
                payload = await reader.readexactly(length - 4)
                if length == 8 and payload in (
                    struct.pack("!I", 80877103),
                    struct.pack("!I", 80877104),
                ):
                    writer.write(b"N")
                    await writer.drain()
                    continue
                upstream.write(header + payload)
                await upstream.drain()
                break
            children = [
                asyncio.create_task(self._forward(reader, upstream, state, False)),
                asyncio.create_task(self._forward(remote, writer, state, True)),
            ]
            await asyncio.wait(children, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            for child in children:
                child.cancel()
            await asyncio.gather(*children, return_exceptions=True)
            if state["pid"] is not None:
                self.connections.pop(state["pid"], None)
            for stream in (writer, upstream):
                if stream is not None:
                    stream.transport.abort()
                    with suppress(OSError, asyncio.CancelledError):
                        await stream.wait_closed()
            self.tasks.discard(task)

    async def _forward(self, reader, writer, state, from_server):
        while True:
            kind = await reader.readexactly(1)
            header = await reader.readexactly(4)
            length = struct.unpack("!I", header)[0]
            if not 4 <= length <= 64 * 1024 * 1024:
                raise ValueError("Invalid PostgreSQL frame")
            payload = await reader.readexactly(length - 4)
            if from_server and kind == b"K":
                state["pid"] = struct.unpack("!I", payload[:4])[0]
                self.connections[state["pid"]] = state
            fault = state["fault"]
            if (
                not from_server
                and kind == b"Q"
                and payload.rstrip(b"\x00;").strip().upper() == b"COMMIT"
                and fault is not None
            ):
                state["committing"] = True
                fault.events.append("client_commit_received")
                if fault.mode in {"drop_before", "hold_before"}:
                    fault.events.append("commit_not_forwarded")
                    fault.reached.set()
                    if fault.mode == "hold_before":
                        await fault.release.wait()
                    return
                fault.events.append("commit_forwarded")
                fault.sent.set()
            if from_server and state["committing"] and fault is not None:
                if kind == b"C" and payload == b"COMMIT\x00":
                    fault.events.append("server_command_complete_commit")
                    fault.events.append("commit_ack_not_forwarded")
                    fault.reached.set()
                    if fault.mode == "hold_after":
                        await fault.release.wait()
                    return
            writer.write(kind + header + payload)
            await writer.drain()
