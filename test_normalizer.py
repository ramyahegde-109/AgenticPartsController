import re

def normalize_electrical(value_str):
    """
    Converts millivolts (mV) to Volts (V) and milliwatts (mW) to Watts (W).
    Returns a standardized lowercase string like '20 v' or '10 w'.
    """
    if not value_str or not isinstance(value_str, str):
        return None
        
    value_str = value_str.lower().strip()
    
    # Use regex to separate the numbers from the letters
    match = re.match(r"([\d\.]+)\s*(mv|v|mw|w)", value_str)
    if not match:
        return value_str # Return as-is if it's strangely formatted
        
    number = float(match.group(1))
    unit = match.group(2)
    
    # Convert milli to base units
    if unit == 'mv':
        number = number / 1000.0
        unit = 'v'
    elif unit == 'mw':
        number = number / 1000.0
        unit = 'w'
        
    # Return formatted string (e.g., '20 v'). The :g removes trailing zeros.
    return f"{number:g} {unit}"


def normalize_connector(conn_str):
    """
    Standardizes connector aliases so 'Type-C', 'USB C', and 'USB-C' all become 'usbc'.
    """
    if not conn_str or not isinstance(conn_str, str):
        return None
        
    # Lowercase and remove all spaces and hyphens
    c = conn_str.lower().strip()
    c = re.sub(r'[\s\-]', '', c)
    
    # Dictionary mapping known aliases to a single standard name
    aliases = {
        'typec': 'usbc',
        'barreljack': 'barrel',
    }
    
    return aliases.get(c, c)


# --- PHASE 7: RIGOROUS TESTING ---
# This block only runs if you execute this file directly.
if __name__ == "__main__":
    print("Testing Electrical Normalization...")
    
    # Test Voltage
    assert normalize_electrical("20000 mV") == "20 v", "Failed 20000 mV"
    assert normalize_electrical("20V") == "20 v", "Failed 20V"
    assert normalize_electrical("20.0 v") == "20 v", "Failed 20.0 v"
    
    # Test Wattage
    assert normalize_electrical("10000mw") == "10 w", "Failed 10000mw"
    assert normalize_electrical("10 W") == "10 w", "Failed 10 W"
    
    print("Electrical tests passed! 20V and 20000mV are now mathematically identical.")
    
    print("\nTesting Connector Normalization...")
    assert normalize_connector("USB-C") == "usbc", "Failed USB-C"
    assert normalize_connector("Type C") == "usbc", "Failed Type C"
    assert normalize_connector("usb c") == "usbc", "Failed usb c"
    assert normalize_connector("Barrel-Jack") == "barrel", "Failed Barrel-Jack"
    
    print("Connector tests passed! Aliases are now identical.")