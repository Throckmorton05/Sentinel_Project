"""Authenticate Unix-socket peers using kernel credentials and group membership."""
from __future__ import annotations

import grp
import pwd
import socket
import struct

from sentinel.daemon.core.constants import CONTROL_GROUP


def _get_peer_credentials(
    conn: socket.socket,
) -> tuple[int, int, int]:
    """
    Return PID, UID, and GID for the process connected to the socket.

    Linux supplies these credentials directly through SO_PEERCRED.
    The client cannot simply claim to be another UID.
    """

    credentials = conn.getsockopt(
        socket.SOL_SOCKET,
        socket.SO_PEERCRED,
        struct.calcsize("3i"),
    )

    pid, uid, gid = struct.unpack(
        "3i",
        credentials,
    )

    return pid, uid, gid


def _uid_is_authorized(uid: int) -> bool:
    """
    Allow root or users belonging to the Sentinel control group.
    """

    if uid == 0:
        return True

    try:
        user = pwd.getpwuid(uid)
        group = grp.getgrnam(CONTROL_GROUP)

    except KeyError:
        return False

    if user.pw_gid == group.gr_gid:
        return True

    return user.pw_name in group.gr_mem
