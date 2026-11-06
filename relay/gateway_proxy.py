"""Local workaround for vllm-project/vllm#56297; no prompt/key logging."""
import copy
import hmac
import json
import os
import pathlib
from aiohttp import web, ClientSession, ClientTimeout, ClientError

# GPU gateway base URL: $CODEX_GPU_GATEWAY_URL or ~/.codex/gpu-gateway.url (not versioned).
def configured_upstream():
    value = os.environ.get('CODEX_GPU_GATEWAY_URL', '').strip()
    if not value:
        try:
            value = (ROOT / 'gpu-gateway.url').read_text().strip()
        except OSError:
            value = ''
    return value.rstrip('/')

ROOT = pathlib.Path.home() / '.codex'
SESSION = web.AppKey('upstream_session', ClientSession)


def normalize_request(payload):
    result = copy.deepcopy(payload)
    items = result.get('input')
    if not isinstance(items, list):
        return result
    for item in items:
        if not isinstance(item, dict) or item.get('role') not in ('user', 'assistant', 'system', 'developer'):
            continue
        content = item.get('content')
        if isinstance(content, list) and content and all(
            isinstance(part, dict) and part.get('type') in ('input_text', 'output_text', 'text')
            and isinstance(part.get('text'), str) for part in content
        ):
            item['content'] = ''.join(part['text'] for part in content)
    return result


def create_app(upstream=None, key_path=ROOT/'gateway.key', token_path=ROOT/'gateway-local.token'):
    if upstream is None:
        upstream = configured_upstream()
    app = web.Application(client_max_size=64*1024*1024)

    async def session_context(app):
        async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=10, sock_read=300), trust_env=False, auto_decompress=False) as session:
            app[SESSION] = session
            yield
    app.cleanup_ctx.append(session_context)

    async def relay(request):
        if request.path == '/health' and request.method == 'GET':
            return web.json_response({'status':'ok', 'upstream_key_present':key_path.is_file()})
        if (request.path, request.method) not in [('/v1/responses','POST'),('/v1/models','GET')]:
            raise web.HTTPNotFound()
        if request.headers.get('Origin'):
            raise web.HTTPForbidden(text='Browser requests are not supported')
        expected = 'Bearer ' + token_path.read_text().strip()
        if not hmac.compare_digest(request.headers.get('Authorization',''), expected):
            raise web.HTTPUnauthorized()
        if request.path == '/v1/models':
            return web.json_response({'models': [{
                'slug':'actif', 'display_name':'GPU Gateway — actif',
                'description':'Modèle chargé sur la passerelle GPU privée',
                'base_instructions':'You are Codex, a coding assistant connected to a private GPU model. Help the user complete their requests. Use the available tools when useful, respect the configured permissions, and verify changes before claiming success. State clearly when an operation has not been tested.',
                'default_reasoning_level':'high',
                'supported_reasoning_levels':[{'effort':x,'description':x} for x in ['low','high','xhigh','max']],
                'shell_type':'unified_exec','visibility':'list','supported_in_api':True,'priority':1,
                'support_verbosity':False,'truncation_policy':{'mode':'tokens','limit':10000},
                'experimental_supported_tools':[], 'input_modalities':['text'],
                'context_window':1048576, 'supports_search_tool':False,
                'default_reasoning_summary':'none', 'supports_reasoning_summary_parameter':False
            }]})
        if not upstream:
            return web.json_response({'error':{'message':'GPU gateway URL not configured: write it to ~/.codex/gpu-gateway.url'}}, status=503)
        try:
            key = key_path.read_text().strip()
        except FileNotFoundError:
            return web.json_response({'error':{'message':'Missing ~/.codex/gateway.key'}}, status=503)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError('Expected a JSON object')
            payload = normalize_request(payload)
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest(text='Invalid JSON request')
        headers = {'Authorization':'Bearer '+key, 'Content-Type':'application/json', 'Accept':'text/event-stream' if payload.get('stream') else 'application/json', 'Accept-Encoding':'identity'}
        try:
            async with app[SESSION].post(upstream+'/v1/responses', data=json.dumps(payload).encode(), headers=headers, allow_redirects=False) as response:
                out = web.StreamResponse(status=response.status, headers={'Content-Type':response.headers.get('Content-Type','application/json'), 'Cache-Control':'no-store'})
                await out.prepare(request)
                async for chunk in response.content.iter_any():
                    await out.write(chunk)
                await out.write_eof()
                return out
        except (ClientError, TimeoutError):
            if 'out' in locals() and out.prepared:
                request.transport.close() if request.transport else None
                return out
            return web.json_response({'error':{'message':'GPU gateway unreachable or timed out. Check Tailscale and server availability.'}}, status=502)
    app.router.add_route('*', '/{tail:.*}', relay)
    return app

if __name__ == '__main__':
    web.run_app(create_app(), host='127.0.0.1', port=4011, access_log=None, print=None)
