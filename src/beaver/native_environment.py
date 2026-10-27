"""Bounded Editor environment and ASCII transport for UTF-8 job paths."""
import base64
import os
from .errors import fail


_KEYS = frozenset((
    'PATH', 'HOME', 'USER', 'LOGNAME', 'TMPDIR', 'TMP', 'TEMP',
    'LANG', 'LC_ALL', 'LC_CTYPE', 'SystemRoot', 'WINDIR', 'COMSPEC',
    'USERPROFILE', 'APPDATA', 'LOCALAPPDATA',
    'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
    'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
    'SSL_CERT_FILE', 'SSL_CERT_DIR', 'NODE_EXTRA_CA_CERTS',
    'UNITY_LICENSE_FILE', 'UNITY_CLI_HOME',
))


def editor_environment(control, job_path):
    if control not in ('BEAVER_BATCH_JOB', 'BEAVER_NATIVE_JOB', 'BEAVER_ADOPT_JOB', 'BEAVER_UI_JOB'):
        fail('NATIVE_ENVIRONMENT_CONTROL', 'control', 'Known native job channel required')
    env = {k: v for k, v in os.environ.items() if k in _KEYS}
    for name, value in env.items():
        try:
            value.encode('utf-8', errors='strict')
        except UnicodeError:
            fail('NATIVE_ENVIRONMENT_ENCODING', name, 'Required Editor environment value is not valid UTF-8; value omitted from diagnostics')
    env[control + '_BASE64'] = base64.b64encode(str(job_path).encode('utf-8')).decode('ascii')
    return env
