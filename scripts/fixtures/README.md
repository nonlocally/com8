# Released broker compatibility fixture

`bus_broker_v04.py` is the unmodified `lib/bus_broker.py` from COM8 v0.4.0,
commit `f043986d2c4509870d11325911d96395cd13bdc3` (MIT license).
SHA-256: `bb5aeb1cf17dad3c36ab84c346c0bbdf8f8285c5b6ee8a21e51c329f9ef7ea63`.

The ownership regression imports it only to exercise the real released database
startup, bus creation, enrollment and delivery during an upgrade/rollback/upgrade
cycle in a temporary directory. It never starts a server. Do not update this
fixture when changing the current broker: its frozen behavior is the check.

The fixture belongs to the test tree and is not included in the runtime package.
