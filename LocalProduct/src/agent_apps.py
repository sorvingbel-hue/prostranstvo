"""Bounded agent tools for application launch; inert until explicit launch."""
from application_launcher import ApplicationLauncher
from browser_access import build_browser_access_plan, inventory_browsers, launch_visible_browser

BROWSERS = ('edge', 'chrome', 'yandex')
APPS = (*BROWSERS, 'notepad', 'calculator', 'vscode', 'notion')
TOOLS = [
    {'name': 'agent_apps_status',
     'description': 'Известные браузеры и приложения: только чтение. Не подтверждает вход на сайты или управление.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'prepare_app_launch',
     'description': 'План запуска известного приложения без открытия окон. Для браузера нужен HTTPS URL.',
     'inputSchema': {'type': 'object', 'properties': {'app': {'type': 'string', 'enum': list(APPS)}, 'url': {'type': 'string'}}, 'required': ['app'], 'additionalProperties': False}},
    {'name': 'launch_agent_app',
     'description': ('Запуск разрешённого приложения может занять экран. foreground_authorized=true допускается только '
                     'после явного согласования с человеком времени использования экрана. При текущем запрете не запускать. '
                     'Запуск не подтверждает управление или вход на сайт.'),
     'inputSchema': {'type': 'object', 'properties': {'app': {'type': 'string', 'enum': list(APPS)}, 'url': {'type': 'string'}, 'foreground_authorized': {'type': 'boolean', 'default': False}}, 'required': ['app'], 'additionalProperties': False}},
    {'name': 'prepare_browser_background',
     'description': ('Только план отдельного headless-профиля и stdio Playwright MCP. Не запускает сервер/браузер '
                     'и не копирует текущую сессию; нужны подключение клиента и отдельный вход.'),
     'inputSchema': {'type': 'object', 'properties': {'browser': {'type': 'string', 'enum': list(BROWSERS)}, 'url': {'type': 'string'}}, 'required': ['browser', 'url'], 'additionalProperties': False}},
]
TOOL_NAMES = frozenset(tool['name'] for tool in TOOLS)


def _arguments(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise ValueError('Неверные параметры инструмента запуска')


def call(name, arguments):
    if name == 'agent_apps_status':
        _arguments(arguments, ())
        return {'browsers': inventory_browsers(), 'desktop': ApplicationLauncher().catalog(),
                'screen_permission_required': True, 'existing_session_control_verified': False,
                'this_call_starts_nothing': True}
    if name == 'prepare_browser_background':
        _arguments(arguments, ('browser', 'url'))
        if arguments['browser'] not in BROWSERS:
            raise ValueError('Неизвестный браузер')
        return build_browser_access_plan(arguments['browser'], arguments['url'])
    if name not in ('prepare_app_launch', 'launch_agent_app'):
        raise ValueError('Неизвестный инструмент запуска')
    _arguments(arguments, ('app',), ('url', 'foreground_authorized') if name == 'launch_agent_app' else ('url',))
    app = arguments['app']
    if not isinstance(app, str) or app not in APPS:
        raise ValueError('Приложение отсутствует в разрешённом каталоге')
    authorized = arguments.get('foreground_authorized', False)
    if type(authorized) is not bool:
        raise ValueError('Требуется явное разрешение на использование экрана')
    if app in BROWSERS:
        if 'url' not in arguments:
            raise ValueError('Для браузера требуется HTTPS URL')
        if name == 'launch_agent_app':
            return launch_visible_browser(app, arguments['url'], foreground_authorized=authorized)
        return build_browser_access_plan(app, arguments['url'], mode='visible')
    if 'url' in arguments:
        raise ValueError('Для этого приложения аргументы и URL не поддерживаются')
    launcher = ApplicationLauncher()
    return launcher.plan(app) if name == 'prepare_app_launch' else launcher.launch(app, foreground_authorized=authorized)
