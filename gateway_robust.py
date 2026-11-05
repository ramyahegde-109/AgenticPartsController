"""Team-side gateway: same protocol as gateway_example.py, plus retries.

Differences from the example:
  * Retries the provider call up to 3 times on temporary errors (429/500/502/503/504,
    timeouts, empty replies) with a short pause between attempts.
  * Prints ONE short status line per request to this terminal (no prompts, no replies,
    no keys) so you can see whether failures come from the tunnel or from Gemini.
Run behind your HTTPS tunnel/proxy. Never put this file or any key in the submission ZIP.
Set TEAM_PROVIDER_URL, TEAM_PROVIDER_KEY, TEAM_MODEL, TEAM_GATEWAY_TOKEN first.
"""
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

ATTEMPTS = 3
PAUSES = (4.0, 12.0)         # waits after attempt 1 and 2 (a longer Retry-After from Google wins, max 30 s)
PROVIDER_TIMEOUT = 20        # seconds per attempt (3 x 20 + 2 x 30 stays under 120 s)
RETRY_CODES = {429, 500, 502, 503, 504}


class NoProviderRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def say(msg):
    print(time.strftime('%H:%M:%S'), msg, flush=True)


def call_provider(frame):
    """Return (text, usage). Raises on failure after all attempts."""
    url = os.environ['TEAM_PROVIDER_URL']
    if not url.startswith('https://'):
        raise ValueError('provider url must be https')
    payload = json.dumps({'model': os.environ['TEAM_MODEL'],
                          'messages': frame['messages'],
                          'max_tokens': frame['max_output_tokens']}).encode()
    headers = {'Content-Type': 'application/json',
               'Authorization': 'Bearer ' + os.environ['TEAM_PROVIDER_KEY']}
    opener = build_opener(ProxyHandler({}), HTTPSHandler(), NoProviderRedirect())
    last = 'unknown'
    wait_extra = 0.0
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with opener.open(Request(url, data=payload, headers=headers), timeout=PROVIDER_TIMEOUT) as r:
                raw = r.read(131073)
            if len(raw) > 131072:
                raise ValueError('provider reply too large')
            result = json.loads(raw)
            text = result['choices'][0]['message']['content']
            if not isinstance(text, str) or not 0 < len(text.encode()) <= 65536:
                last = 'empty or oversized reply'
            else:
                usage = result.get('usage', {}) or {}
                return text, usage
        except HTTPError as e:
            last = 'provider HTTP %s' % e.code
            try:                           # Google's own reason (quota name etc.); no prompts in it
                detail = e.read(600).decode('utf-8', 'replace')
                say('google says: ' + ' '.join(detail.split())[:300])
                ra = e.headers.get('Retry-After')
                wait_extra = min(30.0, float(ra)) if ra else 0.0
            except Exception:
                wait_extra = 0.0
            if e.code not in RETRY_CODES:
                break                      # e.g. 400/401/404: retrying will not help
        except Exception as e:             # timeouts, connection resets, bad JSON
            last = 'provider error: %s' % type(e).__name__
        say('attempt %d/%d failed (%s)' % (attempt, ATTEMPTS, last))
        if attempt < ATTEMPTS:
            time.sleep(max(PAUSES[attempt - 1], wait_extra))
            wait_extra = 0.0
    raise RuntimeError(last)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass

    def do_POST(self):
        t0 = time.time()
        try:
            if self.path != '/gateway' or not hmac.compare_digest(
                    self.headers.get('Authorization', ''), 'Bearer ' + os.environ['TEAM_GATEWAY_TOKEN']):
                say('rejected: bad path or password (403)')
                self.send_error(403)
                return
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 65536:
                raise ValueError('bad size')
            frame = json.loads(self.rfile.read(size))
            if set(frame) != {'messages', 'max_output_tokens'}:
                raise ValueError('bad fields')
            if not isinstance(frame['messages'], list) or not 1 <= len(frame['messages']) <= 24:
                raise ValueError('bad messages')
            if type(frame['max_output_tokens']) is not int or not 16 <= frame['max_output_tokens'] <= 2048:
                raise ValueError('bad max_output_tokens')
            text, usage = call_provider(frame)
            body = json.dumps({'text': text, 'usage': {
                out: usage[key] for key, out in (('prompt_tokens', 'input_tokens'),
                                                 ('completion_tokens', 'output_tokens'))
                if type(usage.get(key)) is int and usage[key] >= 0}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            say('ok (%.1fs)' % (time.time() - t0))
        except Exception as e:
            say('FAILED -> 503 (%s, %.1fs)' % (e, time.time() - t0))
            try:
                self.send_error(503, 'Team gateway unavailable')
            except Exception:
                pass


if __name__ == '__main__':
    for key in ('TEAM_PROVIDER_URL', 'TEAM_PROVIDER_KEY', 'TEAM_MODEL', 'TEAM_GATEWAY_TOKEN'):
        if not os.environ.get(key):
            raise SystemExit('Configure team provider and gateway environment')
    say('gateway listening on 127.0.0.1:8080 (model %s)' % os.environ['TEAM_MODEL'])
    HTTPServer(('127.0.0.1', 8080), Handler).serve_forever()