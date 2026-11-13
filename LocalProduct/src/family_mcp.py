"""Fixed family-only MCP bridge. No chat scraping, token setup or arbitrary recipients."""
import json
import re
import sys

from family_bridge import FamilyBridge
from family_telegram import TelegramError

TOOLS = [
    {'name': 'family_channel_status', 'description': 'Проверить подключение фиксированного Telegram родителя без раскрытия токена и переписки.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
     'annotations': {'readOnlyHint': True, 'openWorldHint': False}},
    {'name': 'send_family_message', 'description': 'Отправить только текст, который пользователь явно просит передать папе, в уже подключённый личный Telegram. Не пересылать автоматически разговор или историю. request_id сохранять при повторе той же отправки. queued не означает доставку.',
     'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
                        'request_id': {'type': 'string', 'pattern': '^[A-Za-z0-9_-]{1,100}$'}},
                     'required': ['text', 'request_id'], 'additionalProperties': False},
     'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': True}},
    {'name': 'family_message_status', 'description': 'Проверить квитанцию одной исходящей отправки по полученному message_id. Не раскрывает текст или всю переписку. sent — принятие Telegram, не прочтение.',
     'inputSchema': {'type': 'object', 'properties': {'message_id': {'type': 'string', 'pattern': '^[A-Za-z0-9_-]{1,100}$'}},
                     'required': ['message_id'], 'additionalProperties': False},
     'annotations': {'readOnlyHint': True, 'openWorldHint': False}},
]

def call(bridge, name, arguments):
    if not isinstance(arguments, dict):
        raise ValueError('invalid_arguments')
    if name == 'family_channel_status' and not arguments:
        return bridge.channel_status()
    if name == 'send_family_message' and set(arguments) == {'text', 'request_id'}:
        if not isinstance(arguments['text'], str) or not 1 <= len(arguments['text']) <= 4000:
            raise ValueError('invalid_text')
        if not isinstance(arguments['request_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', arguments['request_id']):
            raise ValueError('invalid_request_id')
        return bridge.send_message(arguments['text'], arguments['request_id'])
    if name == 'family_message_status' and set(arguments) == {'message_id'}:
        if not isinstance(arguments['message_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', arguments['message_id']):
            raise ValueError('invalid_message_id')
        return bridge.message_status(arguments['message_id'])
    raise ValueError('unknown_tool_or_arguments')

def main(input_stream=None, output_stream=None, bridge=None):
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    bridge = bridge or FamilyBridge()
    for stream in (input_stream, output_stream):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    for line in input_stream:
        request = None
        try:
            if len(line) > 1024 * 1024:
                raise ValueError('request_too_large')
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError('invalid_request')
            ident, method = request.get('id'), request.get('method')
            if ident is None:
                continue
            if method == 'initialize':
                result = {'protocolVersion': request.get('params', {}).get('protocolVersion', '2024-11-05'),
                          'capabilities': {'tools': {}}, 'serverInfo': {'name': 'prostranstvo-family', 'version': '1.0.0'},
                          'instructions': 'Use only for messages the user intentionally asks to send to the already paired parent. Never forward the whole conversation, other chats, history, files or tokens. Check family_channel_status first. If not configured/paired/enabled, explain the one-time local Telegram setup; never ask for its token in chat. Preserve request_id across retries of one send. queued is local queue acceptance; sent means Telegram accepted it, not a phone notification or reading. Never blindly resend uncertain messages. Parent replies currently appear in the local Prostranstvo message window, not automatically in the pet chat.'}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': TOOLS}
            elif method == 'tools/call':
                try:
                    params = request.get('params', {})
                    value = call(bridge, params.get('name'), params.get('arguments', {}))
                    result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}]}
                except TelegramError as error:
                    result = {'isError': True, 'content': [{'type': 'text', 'text': json.dumps({'error': error.code})}]}
                except (ValueError, KeyError):
                    result = {'isError': True, 'content': [{'type': 'text', 'text': '{"error":"invalid_or_unavailable_request"}'}]}
                except Exception:
                    result = {'isError': True, 'content': [{'type': 'text', 'text': '{"error":"family_operation_failed"}'}]}
            else:
                output_stream.write(json.dumps({'jsonrpc': '2.0', 'id': ident, 'error': {'code': -32601, 'message': 'Method not found'}}) + '\n')
                output_stream.flush()
                continue
            output_stream.write(json.dumps({'jsonrpc': '2.0', 'id': ident, 'result': result}, ensure_ascii=False) + '\n')
            output_stream.flush()
        except Exception:
            output_stream.write(json.dumps({'jsonrpc': '2.0', 'id': request.get('id') if isinstance(request, dict) else None,
                                           'error': {'code': -32600, 'message': 'Invalid request'}}) + '\n')
            output_stream.flush()

if __name__ == '__main__':
    main()
