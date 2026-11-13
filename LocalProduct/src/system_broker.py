"""Local beta has no elevated service and never contacts an existing installation."""

REASON='Системное обслуживание с правами SYSTEM не включено в локальную beta.'


def status():
    """Static capability report: do not inspect SCM, registry, pipes or old logs."""
    return {'available':False,'installed':False,'state':'not_included','reason':REASON}


def request(command):
    """Keep the GUI/MCP interface explicit without invoking a privileged helper."""
    raise RuntimeError(REASON)
