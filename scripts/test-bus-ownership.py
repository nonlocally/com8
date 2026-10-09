#!/usr/bin/env python3
"""Hosted account ownership without granting device tokens account authority."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from bus_broker import Broker, BusHTTPServer, handler_factory


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='bus-ownership-')
        self.addCleanup(self.tmp.cleanup)
        self.env = {'BUS_GATEWAY_SHARED_SECRET': 'test-only-' + 's' * 40,
                    'BUS_READER_USERS': json.dumps({'gh-a': 'alice', 'gh-b': 'bob', 'gh-c': 'carol'}),
                    'BUS_ADMIN_READERS': '', 'BUS_CHAT_READERS': '{}', 'BUS_OPENWEBUI_READERS': '1',
                    'BUS_ACCOUNT_LABELS': '{}'}
        self.patch = mock.patch.dict(os.environ, self.env)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.now = 1800000000
        self.b = Broker(self.tmp.name, clock=lambda: self.now)
        self.sessions = {}
        for reader in ('gh-a', 'gh-b', 'gh-c'):
            digest = hashlib.sha256(reader.encode()).hexdigest()
            self.sessions[reader] = (self.b.browser_session(reader, digest)['token'], digest)

    def raw(self, op, who='gh-a', **fields):
        if who in self.sessions:
            token, digest = self.sessions[who]
            return self.b.handle(token, dict(fields, op=op), reader=who, reader_hash=digest)
        return self.b.handle(self.b.admin_token if who == 'admin' else who, dict(fields, op=op))

    def call(self, op, who='gh-a', **fields):
        result = self.raw(op, who, **fields)
        self.assertTrue(result.get('ok'), result)
        return result

    def denied(self, op, who='gh-a', code='forbidden', **fields):
        result = self.raw(op, who, **fields)
        self.assertFalse(result.get('ok'), result)
        self.assertEqual(result['code'], code, result)
        return result

    def buses(self, who='gh-a'):
        return {row['name']: row for row in self.call('snapshot', who)['buses']}

    def enroll(self, bus, issuer='gh-a', user='alice', token=''):
        invite = self.call('invite', issuer, bus=bus, user=user)['invite']
        return self.call('redeem', token, invite=invite, device='fixture')

    def test_private_owner_visibility_persistence_and_name_collision(self):
        created = self.call('create', bus='alice-private', owner_user='bob', user='bob')
        self.assertEqual(created['owner_user'], 'alice')
        self.call('create', bus='alice-private')
        self.call('create', 'gh-b', bus='bob-private')
        self.denied('create', 'gh-b', code='conflict', bus='alice-private')
        snap = self.call('snapshot')
        self.assertTrue(snap['can_create_bus']); self.assertFalse(snap['is_admin'])
        self.assertEqual(set(self.buses()), {'general', 'alice-private'})
        self.assertEqual(set(self.buses('gh-b')), {'general', 'bob-private'})
        private = self.buses()['alice-private']
        self.assertEqual(private['role'], 'owner')
        self.assertTrue(private['capabilities']['manage_members'])
        self.assertEqual(private['members'], [{'user': 'alice', 'role': 'owner'}])
        self.b = Broker(self.tmp.name, clock=lambda: self.now)
        token, digest = self.sessions['gh-a']
        self.assertEqual(self.b.browser_session('gh-a', digest)['token'], token)
        self.assertEqual(self.buses()['alice-private']['owner_user'], 'alice')
        self.assertFalse(self.call('snapshot')['chat']['enabled'])

    def test_account_labels_are_display_only_and_never_identity_authority(self):
        os.environ['BUS_ACCOUNT_LABELS'] = json.dumps({'alice': 'Alice on GitHub', 'bob': '<literal github name>'})
        self.b = Broker(self.tmp.name, clock=lambda: self.now)
        accounts = {row['id']: row for row in self.call('snapshot')['users']}
        self.assertEqual(accounts['alice']['label'], 'Alice on GitHub')
        self.assertEqual(accounts['bob']['label'], '<literal github name>')
        self.assertNotIn('label', accounts['carol'])
        self.call('create', bus='project')
        self.assertFalse(self.raw('member_add', bus='project', user='Alice on GitHub')['ok'])
        self.call('member_add', bus='project', user='bob')
        self.assertEqual(self.buses('gh-b')['project']['role'], 'member')
        device = self.enroll('project', user='bob')
        self.assertNotIn('users', self.call('snapshot', device['token']))
        for labels in ({'unknown-account': 'Somebody'}, {'alice': ''}, {'alice': 'bad\nlabel'},
                       {'alice': 12}, {'alice': ' x '}, ['alice']):
            with self.subTest(labels=labels):
                os.environ['BUS_ACCOUNT_LABELS'] = json.dumps(labels)
                with self.assertRaises(ValueError):
                    Broker(self.tmp.name, clock=lambda: self.now)

    def test_collaborator_assignment_and_invitation_target_scope(self):
        self.call('create', bus='lab')
        self.denied('invite', bus='lab', user='bob')
        self.call('member_add', bus='lab', user='bob')
        self.call('member_add', bus='lab', user='bob')
        self.assertEqual(self.buses('gh-b')['lab']['role'], 'member')
        self.assertNotIn('members', self.buses('gh-b')['lab'])
        self.assertNotIn('lab', self.buses('gh-c'))
        self.denied('member_add', 'gh-b', bus='lab', user='carol')
        self.denied('member_add', bus='lab', user='unknown')
        self.denied('invite', 'gh-b', bus='lab', user='alice')
        own = self.call('invite', 'gh-b', bus='lab')
        self.assertEqual(own['user'], 'bob')
        self.call('invite_revoke', 'gh-b', invite=own['invite'])
        foreign = self.call('invite', bus='lab', user='alice')
        self.denied('invite_revoke', 'gh-b', invite=foreign['invite'])
        self.call('invite_revoke', invite=foreign['invite'])
        self.denied('member_remove', bus='lab', user='alice')
        self.denied('member_remove', 'admin', bus='lab', user='alice')

    def test_device_cannot_impersonate_attributed_account_or_gain_other_bus(self):
        self.call('create', bus='lab')
        self.call('create', 'gh-b', bus='bob-private')
        self.call('member_add', bus='lab', user='bob')
        # Alice can hold an enrollment invite labelled Bob: it is not Bob's login.
        device = self.enroll('lab', user='bob')
        token = device['token']
        self.assertEqual(set(self.buses(token)), {'lab'})
        self.assertFalse(self.call('snapshot', token)['can_create_bus'])
        for op, fields in (('create', {'bus': 'stolen'}), ('invite', {'bus': 'lab', 'user': 'carol'}),
                           ('invite', {'bus': 'bob-private', 'user': 'bob'}),
                           ('member_add', {'bus': 'lab', 'user': 'carol'}),
                           ('member_remove', {'bus': 'lab', 'user': 'bob'}),
                           ('revoke', {'principal': device['principal']})):
            self.denied(op, token, **fields)
        self.denied('register', token, bus='bob-private', session_key='x', name='x')
        # The device may enroll another device for its own attributed account on
        # the bus it already reaches (which the bus owner could already do), and
        # nothing more: no other account, no other bus, no management role.
        self.assertEqual(self.call('invite', token, bus='lab')['user'], 'bob')
        self.assertEqual(self.buses(token)['lab']['role'], 'viewer')

    def test_remove_is_bus_scoped_cancels_leases_and_old_invites(self):
        for bus in ('lab', 'other'):
            self.call('create', bus=bus)
            self.call('member_add', bus=bus, user='bob')
        a = self.enroll('lab')
        b = self.enroll('lab', 'gh-b', 'bob')
        self.enroll('other', 'gh-b', 'bob', b['token'])
        for device in (a, b):
            device['agent'] = self.call('register', device['token'], bus='lab', session_key='s', name=device['user'])['id']
        other_agent = self.call('register', b['token'], bus='other', session_key='other', name='other')['id']
        message = self.call('send', a['token'], bus='lab', sender=a['agent'], target=b['agent'], message='hello')
        envelope = self.call('poll', b['token'], agent=b['agent'])['messages'][0]
        old = self.call('invite', 'gh-b', bus='lab')['invite']
        self.call('member_remove', bus='lab', user='bob')
        self.assertEqual(self.call('receipt', a['token'], id=message['id'])['status'], 'cancelled')
        self.denied('ack', b['token'], code='conflict', agent=b['agent'], id=message['id'], lease=envelope['lease'], status='delivered')
        self.assertNotIn('lab', self.buses('gh-b')); self.assertEqual(set(self.buses(b['token'])), {'other'})
        self.assertEqual(self.buses(b['token'])['other']['agents'][0]['id'], other_agent)
        self.denied('redeem', '', invite=old)
        self.call('member_add', bus='lab', user='bob')
        self.denied('redeem', '', invite=old)
        self.assertNotIn('lab', self.buses(b['token']))
        self.enroll('lab', 'gh-b', 'bob', b['token'])
        self.assertNotIn(b['agent'], [agent['id'] for agent in self.buses(b['token'])['lab']['agents']])

    def test_self_leave_and_cross_bus_denials(self):
        self.call('create', bus='lab'); self.call('member_add', bus='lab', user='bob')
        self.call('create', 'gh-c', bus='carol-private')
        for op in ('member_add', 'member_remove'):
            self.denied(op, bus='carol-private', user='bob')
        self.assertTrue(self.buses('gh-b')['lab']['capabilities']['leave'])
        self.call('member_remove', 'gh-b', bus='lab', user='bob')
        self.assertNotIn('lab', self.buses('gh-b'))
        self.denied('member_remove', 'gh-b', bus='lab', user='alice')

    def test_general_legacy_and_admin_compatibility(self):
        self.call('create', 'admin', bus='legacy')
        self.assertIsNone(self.buses('admin')['legacy']['owner_user'])
        for bus in ('general', 'legacy'):
            for op, extra in (('create', {}), ('member_add', {'user': 'bob'}),
                              ('member_remove', {'user': 'alice'}), ('invite', {'user': 'alice'})):
                self.denied(op, code='conflict' if op == 'create' else 'forbidden', bus=bus, **extra)
        self.call('create', bus='lab')
        self.call('member_add', 'admin', bus='lab', user='bob')
        self.assertEqual(self.buses('admin')['lab']['role'], 'admin')
        self.enroll('legacy', 'admin', 'bob')
        self.denied('member_add', 'admin', bus='general', user='bob')

    def test_browser_context_and_unmapped_openweb_readers_cannot_manage(self):
        token, digest = self.sessions['gh-a']
        for kwargs in ({}, {'reader': 'gh-b', 'reader_hash': digest}, {'reader': 'gh-a', 'reader_hash': '0' * 64}):
            result = self.b.handle(token, {'op': 'create', 'bus': 'spoof'}, **kwargs)
            self.assertEqual(result['code'], 'unauthorized')
        reader = 'owui.12345678-1234-4234-8234-123456789abc'
        view = {'v': 1, 'reader': reader, 'reader_hash': digest, 'iat': self.now, 'exp': self.now + 60, 'buses': ['general']}
        token = self.b.browser_session(reader, digest, view)['token']
        snap = self.b.handle(token, {'op': 'snapshot'}, reader=reader, reader_hash=digest, view=view)
        self.assertFalse(snap['can_create_bus'])
        result = self.b.handle(token, {'op': 'create', 'bus': 'spoof'}, reader=reader, reader_hash=digest, view=view)
        self.assertEqual(result['code'], 'forbidden')

    def test_limits_idempotence_and_concurrent_cross_owner_create(self):
        with mock.patch('bus_broker.MAX_ACCOUNT_BUSES', 1):
            self.call('create', bus='one'); self.call('create', bus='one')
            self.denied('create', code='limit', bus='two')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda who: self.raw('create', who, bus='race'), ('gh-a', 'gh-b')))
        self.assertEqual(sum(result['ok'] for result in results), 1)
        self.assertEqual(next(result for result in results if not result['ok'])['code'], 'conflict')

    def test_account_invites_stay_bounded_single_use_and_expiring(self):
        self.call('create', bus='lab')
        self.call('member_add', bus='lab', user='bob')
        with mock.patch('bus_broker.MAX_ACCOUNT_INVITES', 1):
            expired = self.call('invite', bus='lab', ttl=60)['invite']
            self.denied('invite', code='limit', bus='lab')
            # A different member has an independent issuer quota.
            self.call('invite', 'gh-b', bus='lab')
            self.now += 60
            self.denied('redeem', '', invite=expired)
            invitation = self.call('invite', bus='lab')['invite']
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: self.raw('redeem', '', invite=invitation), range(4)))
            self.assertEqual(sum(result['ok'] for result in results), 1)
            token = next(result for result in results if result['ok'])['token']
            self.call('invite', bus='lab')
        data = self.b.db_path.read_bytes()
        self.assertNotIn(invitation.encode(), data)
        self.assertNotIn(token.encode(), data)

    def test_account_removal_closes_explicit_chat_without_enabling_chat_for_others(self):
        self.call('create', bus='lab'); self.call('member_add', bus='lab', user='bob')
        device = self.enroll('lab')
        agent = self.call('register', device['token'], bus='lab', session_key='s', name='target')['id']
        self.assertFalse(self.call('snapshot', 'gh-b')['chat']['enabled'])
        self.b.chat_readers = {'gh-b': {'identity': 'bob-chat', 'buses': ['lab']}}
        chat = self.call('chat_open', 'gh-b', bus='lab', agent=agent)['chat']['id']
        sent = self.call('chat_send', 'gh-b', chat=chat, message='question', request_id='12345678-1234-4234-8234-123456789abc')
        self.call('member_remove', bus='lab', user='bob')
        self.assertFalse(self.call('snapshot', 'gh-b')['chat']['enabled'])
        self.denied('chat_messages', 'gh-b', code='not_found', chat=chat)
        with self.b._connect() as db:
            row = db.execute('SELECT status,closed FROM human_chat_messages WHERE id=?', (sent['message']['id'],)).fetchone()
            self.assertEqual(tuple(row), ('cancelled', 1))
        self.call('member_add', bus='lab', user='bob')
        self.assertFalse(self.call('snapshot')['chat']['enabled'])
        with self.b._connect() as db:
            self.assertEqual(db.execute('SELECT closed FROM human_chat_messages WHERE id=?', (sent['message']['id'],)).fetchone()[0], 1)

    def test_refresh_preserves_membership_and_removed_roster_account_cannot_manage(self):
        self.call('create', bus='lab'); self.call('member_add', bus='lab', user='bob')
        device = self.enroll('lab', 'gh-b', 'bob')
        token, digest = self.sessions['gh-b']
        self.b.browser_session('gh-b', digest)
        self.assertEqual(self.buses('gh-b')['lab']['role'], 'member')
        with mock.patch.dict(os.environ, {'BUS_READER_USERS': json.dumps({'gh-a': 'alice', 'gh-c': 'carol'})}):
            self.b = Broker(self.tmp.name, clock=lambda: self.now)
        self.denied('snapshot', 'gh-b')
        self.denied('invite', bus='lab', user='bob')
        # Browser roster admission and already-enrolled device revocation remain
        # separate, as in the existing product; account removal is explicit.
        self.assertEqual(set(self.buses(device['token'])), {'lab'})
        self.call('member_remove', bus='lab', user='bob')
        self.assertEqual(self.buses(device['token']), {})

    def test_old_private_bus_and_credentials_migrate_without_guessing_owner(self):
        with tempfile.TemporaryDirectory(prefix='bus-old-owner-') as old:
            db = sqlite3.connect(str(Path(old) / 'bus.sqlite3'))
            db.execute('CREATE TABLE buses(name TEXT PRIMARY KEY,visibility TEXT NOT NULL)')
            db.execute("INSERT INTO buses VALUES('alice-private','private')")
            db.executescript('''CREATE TABLE principals(id TEXT PRIMARY KEY,device TEXT NOT NULL,created_at REAL NOT NULL,
                                 revoked INTEGER NOT NULL DEFAULT 0,is_admin INTEGER NOT NULL DEFAULT 0);
                              CREATE TABLE tokens(digest TEXT PRIMARY KEY,principal TEXT NOT NULL REFERENCES principals(id));
                              CREATE TABLE grants(principal TEXT NOT NULL REFERENCES principals(id),
                                 bus TEXT NOT NULL REFERENCES buses(name),PRIMARY KEY(principal,bus));''')
            legacy_token = 'legacy-device-token-' + 'x' * 40
            db.execute("INSERT INTO principals VALUES('legacy','alice',0,0,0)")
            db.execute("INSERT INTO tokens VALUES(?,'legacy')", (hashlib.sha256(legacy_token.encode()).hexdigest(),))
            db.execute("INSERT INTO grants VALUES('legacy','alice-private')")
            db.commit(); db.close()
            old_broker = self.b
            self.b = Broker(old, clock=lambda: self.now)
            digest = self.sessions['gh-a'][1]
            self.sessions['gh-a'] = (self.b.browser_session('gh-a', digest)['token'], digest)
            self.assertIsNone(self.buses('admin')['alice-private']['owner_user'])
            legacy = self.call('snapshot', legacy_token)
            self.assertIsNone(legacy['user']); self.assertFalse(legacy['can_create_bus'])
            self.assertEqual(legacy['buses'][0]['name'], 'alice-private')
            self.denied('create', code='conflict', bus='alice-private')
            self.assertNotIn('alice-private', self.buses())
            self.b = old_broker

    def test_released_broker_rollback_and_reupgrade_preserve_owned_bus_data(self):
        # Exact published v0.4.0 source, kept locally so shallow CI checkouts
        # exercise the real old broker without network or Git-history access.
        fixture = Path(__file__).parent / 'fixtures/bus_broker_v04.py'
        self.assertEqual(hashlib.sha256(fixture.read_bytes()).hexdigest(),
                         'bb5aeb1cf17dad3c36ab84c346c0bbdf8f8285c5b6ee8a21e51c329f9ef7ea63')
        spec = importlib.util.spec_from_file_location('released_bus_broker_v04', fixture)
        released = importlib.util.module_from_spec(spec)
        prior_bytecode = sys.dont_write_bytecode
        try:
            sys.dont_write_bytecode = True
            spec.loader.exec_module(released)
        finally:
            sys.dont_write_bytecode = prior_bytecode

        def old_call(broker, token, op, **fields):
            result = broker.handle(token, dict(fields, op=op))
            self.assertTrue(result.get('ok'), result)
            return result

        with tempfile.TemporaryDirectory(prefix='bus-runtime-roundtrip-') as state:
            old = released.Broker(state, clock=lambda: self.now)
            old_call(old, old.admin_token, 'create', bus='old-private')
            self.b = Broker(state, clock=lambda: self.now)
            for reader in self.sessions:
                digest = self.sessions[reader][1]
                self.sessions[reader] = (self.b.browser_session(reader, digest)['token'], digest)
            self.call('create', bus='account-private')
            self.call('member_add', bus='account-private', user='bob')
            alice = self.enroll('account-private')
            bob = self.enroll('account-private', 'gh-b', 'bob')
            for device in (alice, bob):
                device['agent'] = self.call('register', device['token'], bus='account-private',
                                           session_key='roundtrip', name=device['user'])['id']
            sent = self.call('send', alice['token'], bus='account-private', sender=alice['agent'],
                             target=bob['agent'], message='Persist through rollback and re-upgrade')
            pending_invite = self.call('invite', 'gh-b', bus='account-private')['invite']
            # Startup, positional bus creation and named-column invitation
            # inserts all use the unmodified released implementation.
            rolled = released.Broker(state, clock=lambda: self.now)
            with rolled._connect() as db:
                self.assertEqual([row[1] for row in db.execute('PRAGMA table_info(buses)')], ['name', 'visibility'])
            self.assertEqual(rolled.admin_token, old.admin_token)
            old_call(rolled, rolled.admin_token, 'create', bus='created-during-rollback')
            invite = old_call(rolled, rolled.admin_token, 'invite', bus='created-during-rollback', user='bob')['invite']
            old_call(rolled, bob['token'], 'redeem', invite=invite, device='existing-bob')
            leased = old_call(rolled, bob['token'], 'poll', agent=bob['agent'])['messages'][0]
            self.assertEqual(leased['message'], 'Persist through rollback and re-upgrade')
            old_call(rolled, bob['token'], 'ack', agent=bob['agent'], id=sent['id'], lease=leased['lease'], status='delivered')

            self.b = Broker(state, clock=lambda: self.now)
            owned = self.buses()['account-private']
            self.assertEqual(owned['owner_user'], 'alice')
            self.assertEqual(owned['members'], [{'user': 'alice', 'role': 'owner'}, {'user': 'bob', 'role': 'member'}])
            self.assertIsNone(self.buses('admin')['created-during-rollback']['owner_user'])
            self.assertEqual(set(self.buses(bob['token'])), {'account-private', 'created-during-rollback'})
            self.assertEqual(self.call('receipt', alice['token'], id=sent['id'])['status'], 'delivered')
            self.call('redeem', '', invite=pending_invite, device='new-bob')
            self.denied('create', 'gh-b', code='conflict', bus='account-private')

    def test_http_rejects_forged_gateway_context_and_cross_origin_mutation(self):
        server = BusHTTPServer(('127.0.0.1', 0), handler_factory(self.b))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            token, digest = self.sessions['gh-a']
            def request(gateway, origin=None):
                headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token,
                           'X-Communicate-Bus-Reader': 'gh-a', 'X-Communicate-Bus-Reader-Hash': digest}
                if gateway is not None: headers['X-Communicate-Bus-Gateway'] = gateway
                if origin: headers['Origin'] = origin
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
                conn.request('POST', '/v1', json.dumps({'op': 'create', 'bus': 'http-owned'}), headers)
                response = conn.getresponse(); data = response.read(); conn.close()
                return response.status, json.loads(data)
            self.assertEqual(request(None)[0], 401)
            self.assertEqual(request('wrong')[0], 401)
            self.assertEqual(request(self.env['BUS_GATEWAY_SHARED_SECRET'], 'https://elsewhere.invalid')[0], 403)
            status, result = request(self.env['BUS_GATEWAY_SHARED_SECRET'])
            self.assertEqual(status, 200, result); self.assertEqual(result['owner_user'], 'alice')
        finally:
            server.shutdown(); thread.join(); server.server_close()


if __name__ == '__main__': unittest.main(verbosity=2)
