import json
import re

# --- PHASE 1: THE FILTER (PRE-PROCESSOR) ---
def get_latest_approved_docs(documents):
    best_docs = {}
    for doc in documents:
        if doc.get('status') != 'approved':
            continue
        entity_id = doc.get('entity_id')
        if entity_id not in best_docs or doc.get('revision', 0) > best_docs[entity_id].get('revision', 0):
            best_docs[entity_id] = doc
    return list(best_docs.values())

# --- PHASE 2: THE NORMALIZERS ---
def normalize_electrical(value_str):
    if not value_str or not isinstance(value_str, str): 
        return None
    match = re.match(r"([\d\.]+)\s*(mv|v|mw|w)", value_str.lower().strip())
    if not match: 
        return value_str
    num, unit = float(match.group(1)), match.group(2)
    if unit == 'mv': 
        num, unit = num / 1000.0, 'v'
    if unit == 'mw': 
        num, unit = num / 1000.0, 'w'
    return f"{num:g} {unit}"

def normalize_connector(conn_str):
    if not conn_str or not isinstance(conn_str, str): 
        return None
    c = re.sub(r'[\s\-]', '', conn_str.lower().strip())
    return {'typec': 'usbc', 'barreljack': 'barrel'}.get(c, c)

# --- PHASE 6 & 7: THE MAIN CONTROLLER ---
def decide(observation, state):
    state = dict(state or {})
    
    # 1. CATCH SIMULATOR ANSWERS (Turn 2+)
    
    # Catch the AI's response and save it to memory
    if 'llm_response' in observation:
        text = observation['llm_response'].strip()
        if text.startswith('```'):
            text = '\n'.join(text.splitlines()[1:-1])
        try:
            state['ai_checklist'] = json.loads(text)
        except json.JSONDecodeError:
            state['ai_checklist'] = {"requests": [], "catalog": []}
            
    # Catch tool results (inspect or verify) and save the stock status
    if 'result' in observation:
        res = observation['result']
        if res.get('tool') == 'inspect':
            state[f"stock_{res['part_id']}"] = res.get('reliability') # 'recent' or 'aged'
        elif res.get('tool') == 'verify':
            state[f"stock_{res['part_id']}"] = 'verified_available' if res.get('available') else 'verified_empty'

    # 2. ASK THE AI FOR THE CHECKLIST (Turn 1)
    if 'ai_checklist' not in state:
        clean_requests = get_latest_approved_docs(observation.get('request_documents', []))
        clean_catalog = get_latest_approved_docs(observation.get('catalog_documents', []))
        
        prompt = (
            "You are a strict data extractor. Read the following requests and catalog parts. "
            "Extract their electrical specs and connector types. Return ONLY a valid JSON object "
            "with this exact schema (no markdown, no explanations):\n"
            '{"requests": [{"id": "...", "voltage": "...", "wattage": "...", "connector": "..."}], '
            '"catalog": [{"id": "...", "voltage": "...", "wattage": "...", "connector": "..."}]}\n\n'
            f"Requests: {json.dumps(clean_requests)}\n"
            f"Catalog: {json.dumps(clean_catalog)}"
        )
        return {
            'action': {
                'op': 'llm', 
                'messages': [{'role': 'user', 'content': prompt}],
                'max_output_tokens': 1024
            }, 
            'state': state
        }
        
    # 3. THE MATCHER & BUDGET MANAGER (Turn 3+)
    state.setdefault('decided_requests', [])
    state.setdefault('used_parts', [])
    budget_spent = state.setdefault('budget_spent', 0)
    
    ai_data = state.get('ai_checklist', {})
    requests_data = ai_data.get('requests', [])
    catalog_data = ai_data.get('catalog', [])
    
    # Find the next customer request we haven't handled yet
    current_request = None
    for req in observation.get('request_documents', []):
        req_id = req.get('entity_id')
        if req_id not in state['decided_requests']:
            current_request = req_id
            break
            
    # If all 6 are done, we finish the game!
    if not current_request:
        return {'action': {'op': 'finish'}, 'state': state}
        
    # Find the AI's extracted specs for this request
    req_specs = next((r for r in requests_data if r.get('id') == current_request), {})
    req_v = normalize_electrical(req_specs.get('voltage'))
    req_c = normalize_connector(req_specs.get('connector'))
    req_w_raw = normalize_electrical(req_specs.get('wattage'))
    req_w = float(req_w_raw.split()[0]) if req_w_raw else None 

    # Look for a compatible part in the catalog
    for part in observation.get('catalog_documents', []):
        part_id = part.get('entity_id')
        
        # Skip parts we already shipped
        if part_id in state['used_parts']:
            continue
            
        part_specs = next((p for p in catalog_data if p.get('id') == part_id), {})
        part_v = normalize_electrical(part_specs.get('voltage'))
        part_c = normalize_connector(part_specs.get('connector'))
        part_w_raw = normalize_electrical(part_specs.get('wattage'))
        part_w = float(part_w_raw.split()[0]) if part_w_raw else 0.0
        
        # SAFETY GATE 1: Check strict compatibility
        if req_v != part_v or req_c != part_c:
            continue
        if req_w and part_w < req_w:
            continue
        if not req_w and part_w < 20:
            continue
            
        # SAFETY GATE 2: Stock Verification
        stock_status = state.get(f"stock_{part_id}")
        
        if not stock_status and state['budget_spent'] <= 12:
            state['budget_spent'] += 1
            return {'action': {'op': 'inspect', 'part_id': part_id}, 'state': state}
            
        if stock_status == 'aged' and state['budget_spent'] <= 11:
            state['budget_spent'] += 2
            return {'action': {'op': 'verify', 'part_id': part_id}, 'state': state}
            
        if stock_status in ['recent', 'verified_available']:
            state['decided_requests'].append(current_request)
            state['used_parts'].append(part_id)
            return {'action': {'op': 'dispatch', 'ticket_id': current_request, 'part_id': part_id}, 'state': state}

    # If we checked every part and found no safe match, Escalate to human.
    state['decided_requests'].append(current_request)
    return {'action': {'op': 'escalate', 'ticket_id': current_request}, 'state': state}