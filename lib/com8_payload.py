"""The daemon's install/pair payload, shared by every distribution consumer.

The bus UI is packaged separately by Communicate. Historical phone, board and
cockpit applications are not prerequisites for durable identities or seats.
"""

KERNEL_FILES = tuple(sorted((
    "cc_peer.py",
    "com8.py",
    "com8_adopt.py",
    "com8_payload.py",
    "com8_seat.py",
    "com8_workspace.py",
    "model_connections.py",
)))


if __name__ == "__main__":
    import json
    print(json.dumps(KERNEL_FILES))
