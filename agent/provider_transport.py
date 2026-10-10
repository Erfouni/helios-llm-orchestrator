"""One HTTP exchange in a killable child, with secrets only on private stdin.

The parent imposes an overall deadline including connect, headers and body.
Never log this input or include the exception message (which can contain URLs).
"""
import json
import sys
import os
import signal
import threading
import time
import urllib.error
import urllib.request


def main():
    spec = json.load(sys.stdin)
    parent = spec.get('parent_pid', os.getppid())
    if sys.platform.startswith('linux'):
        import ctypes
        if ctypes.CDLL(None).prctl(1, signal.SIGKILL) != 0:
            os._exit(124)
        if os.getppid() != parent:
            os._exit(124)
    def guard():
        deadline = time.monotonic() + spec['timeout']
        while time.monotonic() < deadline and os.getppid() == parent:
            time.sleep(0.05)
        os._exit(124)
    threading.Thread(target=guard, daemon=True).start()
    request = urllib.request.Request(
        spec['url'], method=spec['method'], headers=spec['headers'],
        data=None if spec.get('payload') is None else json.dumps(spec['payload']).encode('utf-8'),
    )
    try:
        with urllib.request.urlopen(request, timeout=spec['timeout']) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise ValueError('response too large')
            value = json.loads(raw.decode('utf-8'))
            if not isinstance(value, dict):
                raise ValueError('expected object')
            result = {'response': value}
    except urllib.error.HTTPError as exc:
        result = {'error': 'upstream_http_error', 'upstream_status': exc.code}
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        result = {'error': 'provider_transport_error', 'error_type': type(exc).__name__}
    except (UnicodeError, ValueError):
        result = {'error': 'invalid_provider_response'}
    sys.stdout.write(json.dumps(result))


if __name__ == '__main__':
    main()
