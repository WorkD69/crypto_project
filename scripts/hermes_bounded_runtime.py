"""Approved process-only settings and wire accounting around the installed Hermes CLI.

Executed from source over existing SSH, without installing or editing Hermes.
No request bodies, headers, URLs, credentials or model answers enter the ledger.
"""
import copy
import json
import sys
import threading


class InferenceBlocked(BaseException):
    """Bypass SDK/application retry handlers before a second network attempt."""


def bounded_config(config):
    config = copy.deepcopy(config)
    config.setdefault('agent', {}).update(api_max_retries=1, auto_recovery_cycles=0)
    config.setdefault('auxiliary', {}).setdefault('title_generation', {})['enabled'] = False
    return config


class WireBudget:
    def __init__(self, emit):
        self.count = 0
        self.emit = emit
        self.lock = threading.Lock()

    def check(self, request):
        path = request.url.path.rstrip('/')
        if request.method != 'POST' or not path.endswith(('/responses', '/chat/completions', '/messages')):
            return
        with self.lock:
            payload = json.loads(request.content)
            if self.count >= 1 or payload.get('model') != 'gpt-6-luna':
                raise InferenceBlocked('MODEL_OR_ONE_REQUEST_BUDGET_BLOCKED')
            self.count += 1
            self.emit({'type':'model_invocation', 'model':'gpt-6-luna',
                       'ordinal':self.count, 'outcome':'ATTEMPTED',
                       'scope':'all_httpx_model_requests_including_auxiliary'})


def main():
    # These patches live only in this short-lived process, before CLI construction.
    import httpx
    from hermes_cli import config
    original = config.load_config_readonly
    config.load_config_readonly = lambda *a, **k: bounded_config(original(*a, **k))
    budget = WireBudget(lambda item: print(json.dumps(item), flush=True))
    send = httpx.Client.send
    async_send = httpx.AsyncClient.send

    def guarded_send(client, request, *args, **kwargs):
        budget.check(request)
        return send(client, request, *args, **kwargs)

    async def guarded_async_send(client, request, *args, **kwargs):
        budget.check(request)
        return await async_send(client, request, *args, **kwargs)

    httpx.Client.send = guarded_send
    httpx.AsyncClient.send = guarded_async_send
    import cli
    cli.CLI_CONFIG = bounded_config(cli.CLI_CONFIG)
    if '--crp-preflight' in sys.argv:
        from model_tools import get_tool_definitions
        definitions = get_tool_definitions(['no_mcp'])
        if definitions:
            raise InferenceBlocked('NATIVE_TOOLS_NOT_EMPTY')
        assert cli.CLI_CONFIG['agent']['api_max_retries'] == 1
        assert config.load_config_readonly()['auxiliary']['title_generation']['enabled'] is False
        print(json.dumps({'type':'preflight','native_tools':len(definitions),
                          'model_attempts':budget.count,'process_only_overrides':True}))
        return
    from hermes_cli.main import main as hermes_main
    try:
        hermes_main()
    finally:
        print(json.dumps({'type':'inference_audit','wire_model_attempts':budget.count,
                          'process_only_overrides':True}), flush=True)


if __name__ == '__main__':
    main()
