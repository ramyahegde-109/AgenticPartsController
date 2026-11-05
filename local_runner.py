"""Standalone full-episode public evaluation; runs your own code locally.

python local_runner.py solution.py practice.json --mock --limit 2
python local_runner.py solution.py practice.json --gateway-url https://your.host/gateway

Gateway credentials come only from TEAM_GATEWAY_TOKEN (or --token-env). Mock
mode deterministically escalates tickets and measures simulation wiring only.
This local process is not the official sandbox or an official eligibility check.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import importlib.util
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import queue
import socket
import threading
import time
import ssl
import sys
from urllib.parse import unquote, urlsplit

# Also works with `python -I` in a clean extracted directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agentic_simulator as sim


PUBLIC_KEYS = ('case_id', 'policy', 'budget_remaining', 'steps_remaining', 'tools',
               'tickets', 'catalog', 'decided_ticket_ids', 'done')


def project_observation(record, observation):
    """Keep the released document projection and overlay canonical live receipts."""
    result = deepcopy(record['observation'])
    for key in PUBLIC_KEYS:
        if key not in ('tickets', 'catalog'):
            result[key] = deepcopy(observation[key])
    result.pop('result', None)
    if 'result' in observation:
        result['result'] = deepcopy(observation['result'])
    return result


class _DiscardOutput:
    """Bound and discard controller diagnostics without logging their contents."""
    def __init__(self):
        self.size = 0

    def write(self, value):
        self.size += len(value.encode('utf-8'))
        if self.size > 65536:
            raise sim.ParticipantError('diagnostic_limit', 'Controller diagnostics exceeded 64 KiB')
        return len(value)

    def flush(self):
        pass


class LocalSession:
    def __init__(self, decide):
        self.decide = decide

    def exchange(self, kind, body, *, cap):
        # JSON roundtrips reproduce the controller boundary and prevent mutation
        # of the simulator observation. This is not process/security isolation.
        body = sim.decode(sim.encode(body))
        try:
            with redirect_stdout(_DiscardOutput()), redirect_stderr(_DiscardOutput()):
                result = self.decide(body['observation'], body['state'])
            raw = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
            if len(raw) > cap:
                raise sim.ParticipantError('output_limit', 'Controller response exceeds 1 MiB')
            return 'result', sim.decode(raw)
        except sim.RunError:
            raise
        except (KeyboardInterrupt, SystemExit, Exception):
            raise sim.ParticipantError('solution_exception', 'Controller failed; check decide() locally') from None


class MockProvider:
    """Simulation-only conservative substitute; reads public observations only."""
    mode = 'mock'

    def __init__(self):
        self.observation = {}

    def call(self, action):
        sim.model_request(action)
        decided = set(self.observation.get('decided_ticket_ids', []))
        remaining = [ticket['id'] for ticket in self.observation.get('tickets', [])
                     if ticket['id'] not in decided]
        choice = {'op': 'escalate', 'ticket_id': remaining[0]} if remaining else {'op': 'finish'}
        return {'text': json.dumps(choice), 'usage': {}}


_DNS_SLOTS = threading.BoundedSemaphore(16)


def _resolve_address(hostname, port, timeout):
    """DNS consumes the same overall deadline; a late lookup never sends a request."""
    if not _DNS_SLOTS.acquire(blocking=False):
        raise TimeoutError
    result = queue.Queue(maxsize=1)
    def work():
        try:
            result.put(socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM,
                                          proto=socket.IPPROTO_TCP))
        except Exception:
            result.put([])
        finally:
            _DNS_SLOTS.release()
    threading.Thread(target=work, daemon=True).start()
    answers = result.get(timeout=timeout)
    for family, kind, protocol, _, address in answers:
        if family in (socket.AF_INET, socket.AF_INET6) and kind == socket.SOCK_STREAM:
            return str(ipaddress.ip_address(address[0]))
    raise ValueError('No gateway address')


class _LocalHTTPSConnection(http.client.HTTPConnection):
    """Canonical abortable transport pattern, with a configurable local TLS port."""
    def __init__(self, hostname, address, port, context, deadline):
        super().__init__(hostname, port, timeout=max(.001, deadline-time.monotonic()))
        self._address, self._context, self._deadline = address, context, deadline
        self._connected_socket = None
        self._socket_lock = threading.Lock()
        self._aborted = threading.Event()

    def _remaining(self):
        remaining = self._deadline-time.monotonic()
        if self._aborted.is_set() or remaining <= 0:
            raise TimeoutError
        return remaining

    def connect(self):
        raw = socket.socket(socket.AF_INET6 if ':' in self._address else socket.AF_INET,
                            socket.SOCK_STREAM)
        try:
            with self._socket_lock:
                raw.settimeout(self._remaining())
                self.sock = self._connected_socket = raw
            raw.connect((self._address, self.port))
            with self._socket_lock:
                raw.settimeout(self._remaining())
                secured = self._context.wrap_socket(raw, server_hostname=self.host,
                                                    do_handshake_on_connect=False)
                self.sock = self._connected_socket = secured
                secured.settimeout(self._remaining())
            secured.do_handshake()
            secured.settimeout(self._remaining())
        except Exception:
            if self._connected_socket is not None:
                self._connected_socket.close()
            raw.close()
            raise

    def abort(self):
        self._aborted.set()
        with self._socket_lock:
            connected = self._connected_socket or self.sock
        if connected is not None:
            try:
                connected.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.close()


class GatewayProvider:
    """Explicit local HTTPS gateway; same payload/reply as gateway_example.py.

    Loopback HTTPS and nondefault ports are permitted for local development.
    Portal registration separately requires a public HTTPS endpoint on port 443.
    """
    mode = 'gateway'

    def __init__(self, url, token):
        try:
            if (not isinstance(url, str) or not 1 <= len(url) <= 2048
                    or any(ord(c) <= 32 or ord(c) >= 127 for c in url)
                    or any(c in url for c in ('\\', '?', '#'))):
                raise ValueError
            parsed = urlsplit(url)
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.port == 0
                    or any(ord(c) < 32 or ord(c) == 127 for c in unquote(parsed.path))
                    or not isinstance(token, str)
                    or not re.fullmatch(r'[A-Za-z0-9._~+/-]{1,512}={0,2}', token)):
                raise ValueError
        except (TypeError, ValueError, UnicodeError):
            raise ValueError('Use an HTTPS gateway URL and a valid token environment variable') from None
        self._url, self._token = url, token
        # create_default_context() enables SSLKEYLOGFILE, so construct explicitly.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_default_certs()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._context = context

    @classmethod
    def from_env(cls, url, token_env='TEAM_GATEWAY_TOKEN'):
        return cls(url, os.environ.get(token_env, ''))

    def call(self, action):
        payload = sim.model_request(action)
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
        if len(raw) > sim.REQUEST_CAP:
            raise sim.ParticipantError('llm_request', 'Model request exceeds the body limit')
        connection = timer = None
        try:
            deadline = time.monotonic()+sim.CALL_TIMEOUT
            parts = urlsplit(self._url)
            port = parts.port or 443
            address = _resolve_address(parts.hostname, port, sim.CALL_TIMEOUT)
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            connection = _LocalHTTPSConnection(parts.hostname, address, port, self._context, deadline)
            timer = threading.Timer(remaining, connection.abort)
            timer.daemon = True
            timer.start()
            authority = '['+parts.hostname+']' if ':' in parts.hostname else parts.hostname
            if port != 443:
                authority += ':'+str(port)
            connection.request('POST', parts.path or '/', raw, {
                'Content-Type':'application/json', 'Accept':'application/json',
                'Authorization':'Bearer '+self._token, 'Host':authority, 'Connection':'close'})
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError
            raw = response.read(sim.BODY_CAP+1)
            if len(raw)>sim.BODY_CAP or time.monotonic()>=deadline:
                raise ValueError
            return sim.model_reply(sim.decode(raw))
        except Exception:
            raise sim.InfrastructureError('endpoint_unavailable',
                                          'Gateway request failed; check the gateway configuration') from None
        finally:
            if timer is not None:
                timer.cancel()
            if connection is not None:
                connection.close()



class _TraceProvider:
    def __init__(self, provider, trace):
        self.provider, self.trace = provider, trace

    def call(self, action):
        reply = sim.model_reply(self.provider.call(action))
        if self.trace is not None:
            self.trace.append({'op': 'llm', 'usage': reply['usage']})
        return reply


def _safe_action(action, world):
    # Do not save prompts, model text, controller state, or arbitrary invalid
    # action strings. Trace IDs must belong to the public world.
    op = action.get('op')
    if not isinstance(op, str) or op not in sim.TOOLS:
        return {'op': '<invalid>'}
    result = {'op': op}
    for field, entities in (('part_id', world['parts']), ('ticket_id', world['tickets'])):
        if field in action:
            value = action[field]
            result[field] = value if isinstance(value, str) and value in {x['id'] for x in entities} else '<invalid>'
    return result


def evaluate(decide, records, provider, *, capture_trace=False):
    """Run complete public worlds through the exact 1..8-call ModelTransport."""
    rows, results, total_calls = [], [], 0
    for index, record in enumerate(records):
        trace = [] if capture_trace else None
        relay = sim.ModelTransport(LocalSession(decide), _TraceProvider(provider, trace))
        state = None
        row = {'case_index': index}
        try:
            world = deepcopy(record['world'])
            row['case_id'] = world['case_id']
            state, observation = sim.start(world)
            student = {}
            relay.start_case()
            for _ in range(sim.MAX_STEPS):
                observation = project_observation(record, observation)
                if isinstance(provider, MockProvider):
                    provider.observation = deepcopy(observation)
                response = relay.decide(observation, student)
                student = response['state']
                state, observation, done = sim.step(world, state, response['action'])
                if trace is not None:
                    trace.append({'action': _safe_action(response['action'], world),
                                  'result': deepcopy(observation.get('result')),
                                  'budget_remaining': observation['budget_remaining'],
                                  'steps_remaining': observation['steps_remaining']})
                if done:
                    break
            else:
                raise sim.ParticipantError('step_limit', 'Episode exceeded the published action limit')
            relay.finish_case()
            result = sim.grade(world, state)
            results.append(result)
            row.update(status='scored', result=result)
        except sim.ParticipantError as exc:
            row.update(status='participant_error', code=exc.code, message=exc.message)
        except sim.InfrastructureError as exc:
            row.update(status='runtime_error', code=exc.code, message=exc.message)
        except (KeyError, TypeError, ValueError, RecursionError):
            row.update(status='data_error', code='public_record',
                       message='Invalid public world record; use the supplied dataset')
        row['model_calls'] = relay.audit['model_calls']
        row['world_steps'] = state['steps'] if state is not None else 0
        total_calls += row['model_calls']
        if trace is not None:
            row['trace'] = trace
        rows.append(row)
    failures = sum(row['status'] != 'scored' for row in rows)
    complete = bool(rows) and not failures
    mock = provider.mode == 'mock'
    summary = {'mode': provider.mode, 'simulation_only': mock,
               'official_eligible': False,
               'real_gateway_protocol_passed': bool(complete and not mock),
               'notice': ('Simulation-only mock; no real inference or official eligibility evidence.' if mock else
                          'Local gateway protocol check only; official eligibility requires portal evaluation and review.'),
               'cases': len(rows), 'scored_cases': len(results), 'failures': failures,
               'model_calls': total_calls, 'score': None, 'components': {}, 'diagnostics': {}}
    if complete:
        summary.update(sim.aggregate(results))
    return {'summary': summary, 'records': rows}


def _load_decide(path):
    path = Path(path).resolve()
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location('participant_solution', path)
    if spec is None or spec.loader is None:
        raise ValueError('Controller file is unavailable')
    module = importlib.util.module_from_spec(spec)
    with redirect_stdout(_DiscardOutput()), redirect_stderr(_DiscardOutput()):
        spec.loader.exec_module(module)
    if not callable(getattr(module, 'decide', None)):
        raise ValueError('Controller must define decide(observation, state)')
    return module.decide


def _positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be positive')
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('solution', help='Your controller Python file')
    parser.add_argument('datafile', help='Released training.json, practice.json, or adaptation.json')
    parser.add_argument('--limit', type=_positive, help='Evaluate only the first N public cases')
    parser.add_argument('--trace', action='store_true', help='Include safe world-action and model-usage trace in JSON output')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--mock', action='store_true', help='Explicit deterministic simulation-only model substitute')
    mode.add_argument('--gateway-url', help='Explicit HTTPS gateway endpoint; may spend your model credits')
    parser.add_argument('--token-env', default='TEAM_GATEWAY_TOKEN', help='Environment variable holding the gateway bearer token')
    args = parser.parse_args(argv)
    try:
        records = sim.decode(Path(args.datafile).read_bytes())
        if not isinstance(records, list) or not records:
            raise ValueError('Expected a nonempty JSON array')
        if args.limit:
            records = records[:args.limit]
        provider = MockProvider() if args.mock else GatewayProvider.from_env(args.gateway_url, args.token_env)
        decide = _load_decide(args.solution)
        report = evaluate(decide, records, provider, capture_trace=args.trace)
        print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
        return 1 if report['summary']['failures'] else 0
    except (KeyboardInterrupt, SystemExit, Exception):
        # Never echo controller exceptions, endpoint URLs, HTTP bodies, or tokens.
        print(json.dumps({'error': 'local_setup', 'message':
                         'Could not load the public data, controller, or gateway configuration. Check paths, JSON, decide(), and token environment.'}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
