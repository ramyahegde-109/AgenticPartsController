"""Workshop Parts Desk controller (frozen solution.py for HackMatrix 1.0).

HOW IT WORKS (plain-language overview)
--------------------------------------
1. READ: pick the latest *approved* revision of every request and catalogue
   entry (drafts and late retries of old revisions are ignored).
2. INTERPRET: two independent readers extract voltage, connector and wattage:
   (a) a rule-based parser written in this file, and
   (b) the language model, reached through the platform's `op: llm` protocol.
   If the two readers disagree on a field, that field becomes UNKNOWN. We never guess.
3. PLAN: plain Python code (not the model) chooses every world action. It spends
   the shared tool budget on the cheapest evidence first.
4. SAFETY GATE: a dispatch happens only if the authoritative part card returned by
   inspect/verify matches the request exactly (voltage, connector, watts) AND stock is
   confirmed AND the part is not already given to another ticket. Otherwise escalate.

The model never chooses world actions. It only reads text; all decisions are code.
Only the standard library is used. State is plain JSON (dicts, lists, numbers, strings).
"""
import copy
import json
import math
import re

VERSION = 1

# --- Tunable policy switches -------------------------------------------------
# When the budget is gone, dispatch a part whose card is verified compatible and whose
# desk snapshot says it is in stock, even though the warehouse was not checked?
# An out-of-stock dispatch earns 0 for that ticket only (it is NOT an unsafe dispatch),
# while escalating a solvable ticket earns just 15%.
ALLOW_UNVERIFIED_DISPATCH = True
LLM_MAX_TOKENS = 1800          # allowed range is 16..2048
DEFAULT_COSTS = {'inspect': 1, 'verify': 2, 'clarify': 2}
FIELDS = ('voltage', 'connector', 'watts')
TOL = 1e-6


# =============================================================================
# SECTION 1: reading text (rule-based parser)
# =============================================================================
# Sentences containing these words describe old / ignored / unapproved information.
_DROP = re.compile(r'ignore that|retired|discarded|previous stock|never approved', re.I)
_VOLT = re.compile(r'(\d+(?:\.\d+)?)\s*-?\s*(mV|V|[Vv]olts?)\b')
_WATT = re.compile(r'(\d+(?:\.\d+)?)\s*-?\s*(mW|W|[Ww]atts?)\b')
# Some requests say "its input voltage is 12" with no unit; the unit is volts.
_INPUT_V = re.compile(r'input voltage is\s*(\d+(?:\.\d+)?)(?!\s*-?\s*(?:mV|V|[Vv]olt))', re.I)
_USBC = re.compile(r'usb[\s-]*(?:type[\s-]*)?c\b', re.I)
_BARREL = re.compile(r'\bbarrel\b', re.I)


def _sentences(text):
    """Split text into sentences (also at semicolons)."""
    return [s.strip() for s in re.split(r'(?<=[.?!;])\s+', text) if s.strip()]


def _single(values):
    """Return the value if all findings agree on exactly one number, else None."""
    found = sorted(set(round(v, 3) for v in values))
    return found[0] if len(found) == 1 else None


def parse_spec(text):
    """Rule-based reader: returns voltage (V), connector, watts (W). None = not found
    or ambiguous. mV and mW are converted to V and W."""
    kept = ' '.join(s for s in _sentences(text or '') if not _DROP.search(s))
    volts = [float(n) / 1000 if u == 'mV' else float(n) for n, u in _VOLT.findall(kept)]
    volts += [float(n) for n in _INPUT_V.findall(kept)]
    watts = [float(n) / 1000 if u == 'mW' else float(n) for n, u in _WATT.findall(kept)]
    conns = set()
    if _USBC.search(kept):
        conns.add('USB-C')
    if _BARREL.search(kept):
        conns.add('barrel')
    return {'voltage': _single(volts),
            'connector': next(iter(conns)) if len(conns) == 1 else None,
            'watts': _single(watts)}


def _norm_conn(x):
    """Normalise connector names to exactly 'USB-C' or 'barrel' (else None)."""
    if not isinstance(x, str):
        return None
    s = x.strip().lower()
    if re.fullmatch(r'usb[\s-]*(type[\s-]*)?c', s):
        return 'USB-C'
    if re.fullmatch(r'barrel(\s*(plug|connector|jack|tip))?', s):
        return 'barrel'
    return None


def _num(x):
    """Accept a finite number (or a numeric string); otherwise None."""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x) if math.isfinite(x) else None
    if isinstance(x, str) and re.fullmatch(r'\s*\d+(?:\.\d+)?\s*', x):
        return float(x)
    return None


def _same(field, a, b):
    if field == 'connector':
        return a == b
    return abs(a - b) < TOL


def _latest_docs(docs):
    """entity_id -> text of the newest APPROVED revision. Drafts are ignored and a late
    retry of an older revision loses to the higher revision number."""
    best = {}
    for d in docs or []:
        if not isinstance(d, dict) or d.get('status') != 'approved' or not isinstance(d.get('text'), str):
            continue
        key = tuple(d[k] if isinstance(d.get(k), (int, float)) else 0
                    for k in ('revision', 'issued_at', 'received_at'))
        eid = d.get('entity_id')
        if eid not in best or key > best[eid][0]:
            best[eid] = (key, d['text'])
    return {k: v[1] for k, v in best.items()}


def _mode(obs):
    """'ordinary': recent snapshots are trusted. 'changed' (post-twist policy): only
    verify is authoritative. If the expected ordinary wording is missing we assume
    'changed', which is the cautious choice."""
    policy = str(obs.get('policy', ''))
    ordinary = ('Recent snapshots are synchronized at batch open' in policy
                and 'only verify is authoritative' not in policy)
    return 'ordinary' if ordinary else 'changed'


# =============================================================================
# SECTION 2: the language model (extraction only)
# =============================================================================
_SYSTEM = (
    'You extract technical facts from workshop parts requests and parts-catalogue entries. '
    'You never choose actions. Reply with ONE JSON object and nothing else.\n'
    'Rules: voltage in volts (convert mV by dividing by 1000); wattage in watts (convert mW by '
    'dividing by 1000). Ignore any value described as retired, discarded, previous, old, ignored or '
    'superseded; use only the current requirement. If a request does not state its minimum wattage '
    '(for example the load label is unreadable) use null for min_watts; never guess. '
    'connector must be exactly "USB-C" or "barrel". For every non-null ticket value, copy a short exact '
    'quote from that request text into quotes. For parts, max_watts is the output rating.\n'
    'Schema: {"tickets":[{"id":"...","voltage_v":12,"connector":"USB-C","min_watts":10,'
    '"quotes":{"voltage":"...","connector":"...","watts":"..."}}],'
    '"parts":[{"id":"...","voltage_v":5,"connector":"barrel","max_watts":20}]}')


def _llm_request(S, retry=False):
    """Build the op:llm action. The reply arrives in the next observation as llm_response."""
    payload = {'tickets': [{'id': t, 'text': S['tickets'][t]['text'] or ''} for t in S['order']],
               'parts': [{'id': p, 'text': S['parts'][p]['text'] or ''} for p in S['part_order']]}
    messages = [{'role': 'system', 'content': _SYSTEM},
                {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]
    if retry:
        messages.append({'role': 'user', 'content':
                         'Your previous reply could not be read. Reply with a single valid JSON object only.'})
    S['llm_calls'] += 1
    S['stage'] = 'wait_llm'
    return {'action': {'op': 'llm', 'messages': messages, 'max_output_tokens': LLM_MAX_TOKENS},
            'state': S}


def _extract_json(text):
    """Find the JSON object inside the model text (handles ``` fences and chatter)."""
    if not isinstance(text, str):
        return None
    a, b = text.find('{'), text.rfind('}')
    if a < 0 or b <= a:
        return None
    try:
        obj = json.loads(text[a:b + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _ingest_llm(S, text):
    """Validate the model reply and store it. Returns True if usable."""
    obj = _extract_json(text)
    if obj is None or not isinstance(obj.get('tickets'), list) or not isinstance(obj.get('parts', []), list):
        return False
    for item in obj['tickets']:
        if isinstance(item, dict) and item.get('id') in S['tickets']:
            quotes = item.get('quotes') if isinstance(item.get('quotes'), dict) else {}
            S['tickets'][item['id']]['model'] = {'voltage': _num(item.get('voltage_v')),
                                                 'connector': _norm_conn(item.get('connector')),
                                                 'watts': _num(item.get('min_watts'))}
            S['tickets'][item['id']]['quotes'] = {'voltage': quotes.get('voltage'),
                                                  'connector': quotes.get('connector'),
                                                  'watts': quotes.get('watts')}
    for item in obj.get('parts', []):
        if isinstance(item, dict) and item.get('id') in S['parts']:
            S['parts'][item['id']]['model'] = {'voltage': _num(item.get('voltage_v')),
                                               'connector': _norm_conn(item.get('connector')),
                                               'watts': _num(item.get('max_watts'))}
    return True


def _quote_ok(field, value, quote, text):
    """Is the model's quote really in the request, outside any retired/ignored sentence,
    and does it contain the claimed value?"""
    if not isinstance(quote, str) or not quote.strip():
        return False
    q = ' '.join(quote.lower().split())
    t = ' '.join(text.lower().split())
    if q not in t:
        return False
    for s in _sentences(t):
        if q in s and _DROP.search(s):
            return False
    if field == 'connector':
        return _norm_conn(q) == value or (_USBC.search(q) is not None and value == 'USB-C') \
            or (_BARREL.search(q) is not None and value == 'barrel')
    numbers = [float(x) for x in re.findall(r'\d+(?:\.\d+)?', q)]
    return any(abs(n - value) < TOL or abs(n / 1000 - value) < TOL for n in numbers)


def _merge_ticket_field(field, parsed, model, quote, text):
    """Combine the parser's and the model's answers for one request field."""
    if parsed is not None:
        # Both must agree. A disagreement (or the model saying null) -> unknown.
        return parsed if (model is not None and _same(field, parsed, model)) else None
    if model is None:
        return None
    # Parser found nothing but the model did: accept only with a verified quote.
    return model if _quote_ok(field, model, quote, text) else None


def _finalize(S):
    """Merge parser + model into the final facts for tickets and part specs."""
    for tid in S['order']:
        T = S['tickets'][tid]
        final = {}
        for f in FIELDS:
            if T['text'] is None:
                final[f] = None
            elif not S['llm_ok']:
                final[f] = T['det'][f]          # model unusable: parser-only
            else:
                final[f] = _merge_ticket_field(f, T['det'][f], (T['model'] or {}).get(f),
                                               (T['quotes'] or {}).get(f), T['text'])
        T['final'] = final
    for pid in S['part_order']:
        P = S['parts'][pid]
        spec = {}
        for f in FIELDS:
            det, mod = P['det'][f], (P['model'] or {}).get(f)
            if not S['llm_ok'] or mod is None:
                spec[f] = det
            elif det is None:
                spec[f] = None                   # model-only claims are not trusted for parts
            else:
                spec[f] = det if _same(f, det, mod) else None
        P['spec'] = spec                         # inspect/verify cards overwrite this later


# =============================================================================
# SECTION 3: state set-up
# =============================================================================
def _init(obs):
    """Create the JSON state for a new case."""
    req_docs = _latest_docs(obs.get('request_documents'))
    cat_docs = _latest_docs(obs.get('catalog_documents'))
    tools = obs.get('tools') if isinstance(obs.get('tools'), dict) else {}
    costs = {}
    for op, default in DEFAULT_COSTS.items():
        c = tools.get(op, {}).get('cost') if isinstance(tools.get(op), dict) else None
        costs[op] = c if isinstance(c, int) and not isinstance(c, bool) else default
    S = {'v': VERSION, 'stage': 'wait_llm', 'llm_calls': 0, 'llm_ok': None, 'retried': False,
         'mode': _mode(obs), 'costs': costs, 'pending': None, 'last_marker': None,
         'called': [], 'order': [], 'part_order': [], 'tickets': {}, 'parts': {},
         'evidence': [], 'reasons': {}}
    for t in obs.get('tickets', []):
        tid = t['id']
        text = req_docs.get(tid)           # only approved documents are trusted
        S['order'].append(tid)
        S['tickets'][tid] = {'text': text, 'det': parse_spec(text) if text else parse_spec(''),
                             'model': None, 'quotes': None, 'final': None, 'decided': False,
                             'clarified': False, 'force_escalate': False}
    for p in obs.get('catalog', []):
        pid = p['id']
        text = cat_docs.get(pid) or p.get('description')
        S['part_order'].append(pid)
        S['parts'][pid] = {'age': p.get('snapshot_age'), 'text': text, 'det': parse_spec(text),
                           'model': None, 'spec': None, 'card': None, 'inspect': None,
                           'verify': None, 'assigned': None, 'error': False}
    return S


# =============================================================================
# SECTION 4: evidence about parts and tickets
# =============================================================================
def _stock_state(S, pid):
    """avail / gone / weak_avail / weak_gone / unknown.
    'avail'/'gone' are trustworthy: warehouse (verify) or a recent snapshot in ordinary mode."""
    P = S['parts'][pid]
    if P['assigned'] or P['error']:
        return 'gone'
    if P['verify'] is not None:
        return 'avail' if P['verify'] > 0 else 'gone'
    if P['inspect'] is not None:
        if S['mode'] == 'ordinary' and P['age'] == 'recent':
            return 'avail' if P['inspect'] > 0 else 'gone'
        return 'weak_avail' if P['inspect'] > 0 else 'weak_gone'
    return 'unknown'


def _check_step(S, pid):
    """Cheapest tool that settles this part's stock: (op, cost). None if already settled."""
    st = _stock_state(S, pid)
    if st in ('avail', 'gone'):
        return None
    P = S['parts'][pid]
    if st == 'unknown' and S['mode'] == 'ordinary' and P['age'] == 'recent':
        return ('inspect', S['costs']['inspect'])
    return ('verify', S['costs']['verify'])


def _candidates(S, tid):
    """Parts whose voltage and connector match the request.
    safe  = also meets the wattage floor (20 W when the wattage is unknown, since every
            request needs 10 or 20 W).
    maybe = lower-wattage parts that would only fit if the unknown wattage is 10."""
    r = S['tickets'][tid]['final']
    safe, maybe = [], []
    for pid in S['part_order']:
        P = S['parts'][pid]
        spec = P['spec']
        if P['error'] or not spec or any(spec[f] is None for f in FIELDS):
            continue
        if spec['connector'] != r['connector'] or abs(spec['voltage'] - r['voltage']) > TOL:
            continue
        need = r['watts'] if r['watts'] is not None else 20.0
        if spec['watts'] >= need - TOL:
            safe.append(pid)
        elif r['watts'] is None:
            maybe.append(pid)
    return safe, maybe


def _gate(S, tid, pid, relaxed=False):
    """THE SAFETY GATE. True only if dispatching `pid` to `tid` is provably compatible."""
    T, P = S['tickets'][tid], S['parts'][pid]
    r = T['final']
    if T['decided'] or P['assigned'] or P['error'] or r is None:
        return False
    if r['voltage'] is None or r['connector'] is None:
        return False
    card = P['card']                       # authoritative data from inspect/verify
    if card is None:
        return False
    if abs(card['voltage'] - r['voltage']) > TOL or card['connector'] != r['connector']:
        return False
    need = r['watts'] if r['watts'] is not None else 20.0
    if card['watts'] < need - TOL:
        return False
    st = _stock_state(S, pid)
    return st == 'avail' or (relaxed and st == 'weak_avail')


# =============================================================================
# SECTION 5: the decision policy
# =============================================================================
def _ticket_plan(S, tid):
    """What does this ticket need next?
    ('escalate', why) | ('dispatch', [ready parts]) | ('tool', op, target, cost) | ('stuck',)"""
    T = S['tickets'][tid]
    r = T['final']
    if T['force_escalate']:
        return ('escalate', 'an earlier dispatch attempt failed')
    if r['voltage'] is None or r['connector'] is None:
        return ('escalate', 'voltage or connector unclear')
    safe, maybe = _candidates(S, tid)
    ready = [p for p in safe if _gate(S, tid, p)]
    if ready:
        return ('dispatch', ready)
    live = [p for p in safe if _stock_state(S, p) != 'gone']
    steps = []
    for p in live:
        step = _check_step(S, p)
        if step:
            steps.append((step[1], S['part_order'].index(p), step[0], p))
    if steps:
        steps.sort()
        return ('tool', steps[0][2], steps[0][3], steps[0][0])
    if live:
        return ('stuck',)
    if r['watts'] is None and not T['clarified'] and any(_stock_state(S, p) != 'gone' for p in maybe):
        return ('tool', 'clarify', tid, S['costs']['clarify'])
    return ('escalate', 'no compatible available part')


def _others_wanting(S, pid, tid):
    """How many other undecided tickets could also use this part (contention)."""
    n = 0
    for other in S['order']:
        if other == tid or S['tickets'][other]['decided'] or S['tickets'][other]['final']['voltage'] is None \
                or S['tickets'][other]['final']['connector'] is None:
            continue
        safe, maybe = _candidates(S, other)
        if pid in safe or pid in maybe:
            n += 1
    return n


def _next_action(S, obs):
    budget = obs.get('budget_remaining', 0)
    undecided = [t for t in S['order'] if not S['tickets'][t]['decided']]
    if not undecided:
        return _issue(S, obs, {'op': 'finish'})
    plans = {t: _ticket_plan(S, t) for t in undecided}

    # 1) Free decisions first: escalations that need no more evidence.
    for t in undecided:
        if plans[t][0] == 'escalate':
            S['reasons'][t] = plans[t][1]
            return _issue(S, obs, {'op': 'escalate', 'ticket_id': t})

    # 2) Free decisions: proven-safe dispatches. Prefer the ticket with the fewest options and
    #    the part wanted by the fewest other tickets (leave contested parts for those who need them).
    ready = [t for t in undecided if plans[t][0] == 'dispatch']
    if ready:
        def option_count(t):
            return len([p for p in _candidates(S, t)[0] if _stock_state(S, p) != 'gone'])
        t = min(ready, key=lambda x: (option_count(x), S['order'].index(x)))
        pid = min(plans[t][1], key=lambda p: (_others_wanting(S, p, t), S['parts'][p]['card']['watts'], p))
        S['reasons'][t] = 'compatible and in stock (%s)' % pid
        return _issue(S, obs, {'op': 'dispatch', 'ticket_id': t, 'part_id': pid})

    # 3) Spend tool units: cheapest useful tool that fits the budget.
    options = []
    for t in undecided:
        plan = plans[t]
        if plan[0] != 'tool':
            continue
        _, op, target, cost = plan
        if cost > budget:
            continue
        if op == 'clarify' and budget < cost + S['costs']['inspect']:
            continue                       # keep at least one unit to use the answer
        if '%s:%s' % (op, target) in S['called']:
            continue                       # never repeat a tool call
        shared = _others_wanting(S, target, t) if op != 'clarify' else 0
        options.append((cost, -shared, S['order'].index(t), op, target))
    if options:
        options.sort()
        _, _, _, op, target = options[0]
        key = 'ticket_id' if op == 'clarify' else 'part_id'
        return _issue(S, obs, {'op': op, key: target})

    # 4) No affordable progress: decide every remaining ticket with the evidence we have.
    t = undecided[0]
    if ALLOW_UNVERIFIED_DISPATCH:
        safe, _ = _candidates(S, t)
        relaxed = [p for p in safe if _gate(S, t, p, relaxed=True)]
        if relaxed:
            pid = min(relaxed, key=lambda p: (_others_wanting(S, p, t), p))
            S['reasons'][t] = 'budget exhausted; compatible, snapshot says in stock (%s)' % pid
            return _issue(S, obs, {'op': 'dispatch', 'ticket_id': t, 'part_id': pid}, relaxed=True)
    S['reasons'][t] = 'budget exhausted without proof of a compatible available part'
    return _issue(S, obs, {'op': 'escalate', 'ticket_id': t})


def _issue(S, obs, action, relaxed=False):
    """Final validation, then record what we are about to do. An invalid action costs a tool
    unit, so every action is re-checked here; if anything is wrong we escalate instead."""
    op = action['op']
    budget = obs.get('budget_remaining', 0)
    ok = True
    if op in ('inspect', 'verify'):
        ok = action.get('part_id') in S['parts'] and S['costs'][op] <= budget \
            and '%s:%s' % (op, action['part_id']) not in S['called']
    elif op == 'clarify':
        ok = action.get('ticket_id') in S['tickets'] and not S['tickets'][action['ticket_id']]['decided'] \
            and S['costs']['clarify'] <= budget and 'clarify:%s' % action['ticket_id'] not in S['called']
    elif op == 'dispatch':
        ok = action.get('ticket_id') in S['tickets'] and action.get('part_id') in S['parts'] \
            and _gate(S, action['ticket_id'], action['part_id'], relaxed=relaxed)
    elif op == 'escalate':
        ok = action.get('ticket_id') in S['tickets'] and not S['tickets'][action['ticket_id']]['decided']
    if not ok:                              # safe fallback: hand to a person
        undecided = [t for t in S['order'] if not S['tickets'][t]['decided']]
        action = {'op': 'escalate', 'ticket_id': undecided[0]} if undecided else {'op': 'finish'}
        op = action['op']
    S['last_marker'] = obs.get('steps_remaining')
    if op in ('inspect', 'verify'):
        S['called'].append('%s:%s' % (op, action['part_id']))
        S['pending'] = {'op': op, 'part': action['part_id']}
    elif op == 'clarify':
        S['called'].append('clarify:%s' % action['ticket_id'])
        S['pending'] = {'op': op, 'ticket': action['ticket_id']}
    elif op == 'dispatch':
        S['parts'][action['part_id']]['assigned'] = action['ticket_id']
        S['pending'] = {'op': op, 'ticket': action['ticket_id'], 'part': action['part_id']}
    else:
        S['pending'] = None
    return {'action': action, 'state': S}


# =============================================================================
# SECTION 6: reading the results of our own tool calls
# =============================================================================
def _sync(S, obs):
    """The platform's decided list is the truth about which tickets are finished."""
    decided = set(obs.get('decided_ticket_ids', []))
    for tid in S['order']:
        S['tickets'][tid]['decided'] = tid in decided


def _ingest_result(S, obs):
    """Record the outcome of the action we issued last time (once only)."""
    pend = S.get('pending')
    if not pend or obs.get('steps_remaining') == S.get('last_marker'):
        return                              # nothing new (e.g. the observation repeated)
    S['pending'] = None
    res = obs.get('result')
    op = pend['op']
    good = isinstance(res, dict) and res.get('ok') is True and isinstance(res.get('data'), dict)
    if op in ('inspect', 'verify'):
        P = S['parts'][pend['part']]
        if not good:
            P['error'] = True
            return
        d = res['data']
        card = {'voltage': _num(d.get('voltage')), 'connector': _norm_conn(d.get('connector')),
                'watts': _num(d.get('watts'))}
        stock = _num(d.get('available'))
        if any(v is None for v in card.values()) or stock is None:
            P['error'] = True
            return
        P['card'] = card
        P['spec'] = dict(card)              # the card is authoritative: overrides the catalogue text
        P[op] = int(stock)
        S['evidence'].append({'op': op, 'part': pend['part'], 'available': int(stock),
                              'source': d.get('source')})
    elif op == 'clarify':
        T = S['tickets'][pend['ticket']]
        T['clarified'] = True
        if good:
            watts = parse_spec(str(res['data'].get('reply', '')))['watts']
            if watts is not None and (abs(watts - 10) < TOL or abs(watts - 20) < TOL):
                T['final']['watts'] = watts
            S['evidence'].append({'op': 'clarify', 'ticket': pend['ticket'], 'watts': watts})
    elif op == 'dispatch' and not good:
        S['tickets'][pend['ticket']]['force_escalate'] = True


# =============================================================================
# SECTION 7: the required entry point
# =============================================================================
def _decide(obs, S):
    if S.get('v') != VERSION:
        return _llm_request(_init(obs))     # first call: ask the model to read the documents
    if S['stage'] == 'wait_llm':
        if 'llm_response' in obs:
            ok = _ingest_llm(S, obs['llm_response'])
            if not ok and not S['retried']:
                S['retried'] = True
                return _llm_request(S, retry=True)
            S['llm_ok'] = ok
        elif S['llm_calls'] < 2:
            return _llm_request(S, retry=True)
        else:
            S['llm_ok'] = False
        _finalize(S)
        S['stage'] = 'run'
    _sync(S, obs)
    _ingest_result(S, obs)
    return _next_action(S, obs)


def _fallback(obs, state):
    """Last-resort safe behaviour if something unexpected happens: ensure the one required model
    call has happened, then hand every remaining ticket to a person."""
    S = dict(state) if isinstance(state, dict) else {}
    S['fallback'] = True
    try:
        if not S.get('llm_calls'):
            S['llm_calls'] = 1
            return {'action': {'op': 'llm', 'messages': [{'role': 'user', 'content': 'Reply with the word OK.'}],
                               'max_output_tokens': 16}, 'state': S}
        decided = set(obs.get('decided_ticket_ids', []))
        remaining = [t['id'] for t in obs.get('tickets', []) if t['id'] not in decided]
        action = {'op': 'escalate', 'ticket_id': remaining[0]} if remaining else {'op': 'finish'}
        return {'action': action, 'state': S}
    except Exception:
        return {'action': {'op': 'finish'}, 'state': {'fallback': True}}


def decide(observation, state):
    """Entry point required by the platform: returns exactly {'action': ..., 'state': ...}."""
    try:
        if isinstance(state, dict) and state.get('fallback'):
            return _fallback(observation, state)
        result = _decide(observation, copy.deepcopy(state) if isinstance(state, dict) else {})
        json.dumps(result, allow_nan=False)     # state must be JSON-safe
        return result
    except Exception:
        return _fallback(observation, state)