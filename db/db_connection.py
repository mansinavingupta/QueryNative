import psycopg2

# Active connection config — updated when user connects via UI
_config = {
    "host":     None,
    "dbname":   None,
    "user":     None,
    "password": None,
    "port":     5432
}

def set_connection_config(host, dbname, user, password, port=5432):
    """Update the active database connection config."""
    global _config
    _config = {
        "host":     host,
        "dbname":   dbname,
        "user":     user,
        "password": password,
        "port":     int(port)
    }

def get_connection():
    """Return a new psycopg connection using the current config."""
    return psycopg2.connect(**_config)

def test_connection(host, dbname, user, password, port=5432):
    """Try connecting and return (success, error_message)."""
    try:
        conn = psycopg2.connect(
            host=host, dbname=dbname, user=user,
            password=password, port=int(port),
            connect_timeout=5
        )
        conn.close()
        return True, None
    except Exception as e:
        return False, str(e)

def get_current_config():
    return {k: v for k, v in _config.items() if k != "password"}