"""Explicit, single-key native title setup without per-launch CLI overrides."""
from pathlib import Path

from .client_counter import TITLE_ITEMS
from .rpc import CodexRPC, RpcError


class TitleSetupError(RpcError):
    pass


def _error(message, applied=False):
    return TitleSetupError(message, {'code': 'native_title_setup_failed', 'applied': applied})


def _absolute(value):
    if not isinstance(value, (str, Path)) or not str(value) or '\0' in str(value):
        raise ValueError('An absolute Codex home/config path is required')
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError('An absolute Codex home/config path is required')
    return path.resolve()


def _title(config):
    if not isinstance(config, dict):
        raise _error('Codex returned an invalid configuration object')
    tui = config.get('tui')
    if tui is None:
        return None
    if not isinstance(tui, dict):
        raise _error('Codex returned an invalid tui configuration object')
    return tui.get('terminal_title')


def _user_layer(reply, config_file):
    if not isinstance(reply, dict) or not isinstance(reply.get('layers'), list):
        raise _error('Cannot verify the writable user configuration layer; no settings were written')
    layers = []
    for layer in reply['layers']:
        if not isinstance(layer, dict):
            raise _error('Codex returned an invalid configuration layer')
        name = layer.get('name')
        if not isinstance(name, dict) or name.get('type') != 'user' or name.get('profile') is not None:
            continue
        try:
            matches = _absolute(name.get('file')) == config_file
        except (OSError, RuntimeError, ValueError):
            matches = False
        if matches:
            layers.append(layer)
    if len(layers) != 1:
        raise _error('Cannot verify one base user layer for the requested Codex home. '
                     'Run Codex normally once, then retry setup against its shared server; '
                     'otherwise configure tui.terminal_title manually in the intended config.toml. '
                     'No settings were written')
    layer = layers[0]
    if layer.get('disabledReason') is not None:
        raise _error('The requested user configuration layer is disabled; no settings were written')
    if not isinstance(layer.get('version'), str) or not layer['version']:
        raise _error('The user configuration version is unavailable; no settings were written')
    _title(layer.get('config'))
    return layer


def configure_native_title(home, binary, socket_path=None, apply=False):
    """Preview, or explicitly persist, the native title needed by the counter.

    Writes use the verified base user file and a compare-and-swap version.
    Only tui.terminal_title is changed; no thread is loaded and no runtime
    configuration reload or model request is made. A rejected/uncertain write
    is never automatically retried.
    """
    if type(apply) is not bool:
        raise ValueError('apply must be a boolean')
    actual_home = _absolute(home)
    config_file = actual_home / 'config.toml'
    if '.claude' in actual_home.parts or config_file.resolve() != config_file:
        raise ValueError('Refusing a Claude directory or a redirected Codex config file')
    rpc = CodexRPC(str(actual_home), binary, socket_path)
    try:
        before = rpc.request('config/read', {'includeLayers': True})
        layer = _user_layer(before, config_file)
        previous = _title(layer['config'])
        effective = _title(before.get('config'))
        desired = list(TITLE_ITEMS)
        would_change = previous != desired
        result = {'config_file': str(config_file), 'would_change': would_change,
                  'applied': False, 'affects_next_launch': True,
                  'terminal_title': desired, 'previous_terminal_title': previous,
                  'effective_terminal_title': effective,
                  'effective_matches': effective == desired}
        if not apply:
            return result
        if not would_change:
            if effective != desired:
                raise _error('The saved title is already configured but another layer overrides it; '
                             'resolve that override before launching the automatic counter')
            return result
        write = rpc.request('config/value/write', {
            'keyPath': 'tui.terminal_title', 'value': desired, 'mergeStrategy': 'replace',
            'filePath': str(config_file), 'expectedVersion': layer['version']}, mutating=True)
        if not isinstance(write, dict):
            raise _error('Codex returned an invalid title write acknowledgement; inspect config before retrying', True)
        try:
            written_path = _absolute(write.get('filePath'))
        except (OSError, RuntimeError, ValueError):
            written_path = None
        if written_path != config_file or write.get('status') != 'ok' or write.get('overriddenMetadata') is not None:
            raise _error('The title write was not confirmed effective in the requested config file; '
                         'inspect the configuration and higher-priority overrides before retrying', True)
        try:
            after = rpc.request('config/read', {'includeLayers': True})
            saved = _user_layer(after, config_file)
            verified = _title(saved['config']) == desired and _title(after.get('config')) == desired
        except (RpcError, OSError, ValueError) as exc:
            raise _error('Title configuration was written but readback could not be verified: ' + str(exc), True) from exc
        if not verified:
            raise _error('Title configuration was written but readback differs; inspect overrides before retrying', True)
        result.update(applied=True, effective_terminal_title=desired, effective_matches=True,
                      version=write.get('version'))
        return result
    finally:
        rpc.close()
