import asyncio
import json
import logging
import time
import uuid
import websockets


logger = logging.getLogger(__name__)


class Server:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.workflows = {}   # {path: workflow}

    def register(self, workflow, path):
        if path in self.workflows:
            raise ServerException(f'a workflow is already registered at path {path}')
        self.workflows[path] = workflow

    def start(self):
        start_server = websockets.serve(self.handler, self.host, self.port)
        asyncio.get_event_loop().run_until_complete(start_server)

        try:
            asyncio.get_event_loop().run_forever()

        except KeyboardInterrupt:
            logger.debug('server terminated')

    async def handler(self, websocket, path):
        workflow = self.workflows.get(path, None)
        if workflow:
            relay = Relay(workflow)
            await relay.handle(websocket)

        else:
            raise ServerException(f'no workflow registered for path {path}')


class ServerException(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(self.message)


class Workflow:
    def __init__(self, name):
        self.name = name
        self.type_handlers = {}  # {(type, args): func}

    def on_start(self, func):
        self.type_handlers[('wf_api_start_event')] = func

    def on_button(self, _func=None, *, button='*', taps='*'):
        def on_button_decorator(func):
            self.type_handlers[('wf_api_button_event', button, taps)] = func

        if _func:
            return on_button_decorator(_func)

        else:
            return on_button_decorator

    
    def on_notification(self, _func=None, *, event='*', source='*'):
        def on_notification_decorator(func):
            self.type_handlers[('wf_api_notification_event', event, source)] = func

        if _func:
            return on_notification_decorator(_func)

        else:
            return on_notification_decorator


    def on_timer(self, func):
        self.type_handlers[('wf_api_timer_event')] = func


    def get_handler(self, event):
        t = event['_type']

        # assume no-arg handler; if not, check the handlers that require args
        # for args, check for handler registered with specific values first; if not, then check variations with wildcard values
        h = self.type_handlers.get((t), None)
        if not h:
            if t == 'wf_api_button_event':
                h = self.type_handlers.get((t, event['button'], event['taps']), None)
                if not h:
                    # prefer button match over taps
                    h = self.type_handlers.get((t, event['button'], '*'), None)
                    if not h:
                        h = self.type_handlers.get((t, '*', event['taps']), None)
                        if not h:
                            h = self.type_handlers.get((t, '*', '*'), None)

            elif t == 'wf_api_notification_event':
                h = self.type_handlers.get((t, event['event'], event['source']), None)
                if not h:
                    # prefer event match over source
                    h = self.type_handlers.get((t, event['event'], '*'), None)
                    if not h:
                        h = self.type_handlers.get((t, '*', event['source']), None)
                        if not h:
                            h = self.type_handlers.get((t, '*', '*'), None)

        return h


class WorkflowException(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(self.message)


class Relay:
    def __init__(self, workflow):
        self.workflow = workflow
        self.websocket = None
        self.id_futures = {}  # {_id: future}

    async def handle(self, websocket):
        self.websocket = websocket

        try:
            async for m in websocket:
                logger.debug(f'{self.workflow.name} - recv: {m}')
                e = json.loads(m)
                _id = e.get('_id', None)
    
                if _id:
                    fut = self.id_futures.pop(_id, None)
                    if fut:
                        fut.set_result(e)
    
                    else:
                        logger.warning(f'{self.workflow.name} - found response for unknown _id {_id}')
    
                else:
                    h = self.workflow.get_handler(e)
                    if h:
                        t = e['_type']
                        if t == 'wf_api_start_event':
                            asyncio.create_task(self.wrapper(h))
    
                        elif t == 'wf_api_button_event':
                            asyncio.create_task(self.wrapper(h, e['button'], e['taps']))
    
                        elif t == 'wf_api_notification_event':
                            asyncio.create_task(self.wrapper(h, e['source'], e['event']))
    
                        elif t == 'wf_api_timer_event':
                            asyncio.create_task(self.wrapper(h))
    
                    else:
                        logger.warning(f'{self.workflow.name} - no handler found for _type {e["_type"]}')

        except Exception as x:
            logger.error(f'{self.workflow.name} - {x}', exc_info=True)

        finally:
            logger.debug(f'{self.workflow.name} - websocket closed')


    # run handlers with exception logging; needed since we cannot await handlers
    async def wrapper(self, h, *args):
        try:
            await h(self, *args)
        except Exception as x:
            logger.error(f'{self.workflow.name} - {x}', exc_info=True)


    async def send(self, obj):
        _id = uuid.uuid4().hex
        obj['_id'] = _id

        # TODO: ibot add responses to all _request events? if so, await them here ... and check for error responses

        await self._send(json.dumps(obj))


    async def sendReceive(self, obj):
        _id = uuid.uuid4().hex
        obj['_id'] = _id

        fut = asyncio.get_event_loop().create_future()
        self.id_futures[_id] = fut

        await self._send(json.dumps(obj))

        # wait on the response
        await fut

        rsp = fut.result()
        if rsp['_type'] == 'wf_api_error_response':
            raise WorkflowException(rsp['error'])

        return fut.result()


    async def _send(self, s):
        logger.debug(f'{self.workflow.name} - send: {s}')
        await self.websocket.send(s)


    async def get_var(self, name: str, default=None):
        event = {
            '_type': 'wf_api_get_var_request',
            'name': name
        }
        v = await self.sendReceive(event)
        return v.get('value', default)

    async def set_var(self, name: str, value: str):
        event = {
            '_type': 'wf_api_set_var_request',
            'name': name,
            'value': value
        }
        await self.sendReceive(event)


    async def listen(self, phrases=None):
        if not phrases:
            phrases = []

        event = {
            '_type': 'wf_api_listen_request',
            'phrases': phrases,
            'transcribe': True,
            'timeout': 60
        }
        v = await self.sendReceive(event)
        return v['text']

    async def play(self, fname):
        event = {
            '_type': 'wf_api_play_request',
            'filename': fname
        }
        await self.sendReceive(event)

    async def say(self, text):
        event = {
            '_type': 'wf_api_say_request',
            'text': text
        }
        await self.sendReceive(event)


    async def broadcast(self, text: str, targets):
        await self._notify('broadcast', text, targets)

    async def notify(self, text: str, targets):
        await self._notify('background', text, targets)

    async def alert(self, text: str, targets):
        await self._notify('foreground', text, targets)

    async def _notify(self, ntype, text, targets):
        event = {
            '_type': 'wf_api_notification_request',
            'type': ntype,
            'text': text,
            'target': targets
        }
        await self.sendReceive(event)


    async def get_device_name(self):
        v = await self._get_device_info('name', False)
        return v['name']

    async def get_device_location(self):
        # TODO: also return latlong?
        v = await self._get_device_info('location', False)
        return v['address']

    async def get_device_indoor_location(self):
        v = await self._get_device_info('indoor_location', False)
        return v['indoor_location']

    async def get_device_battery(self):
        v = await self._get_device_info('battery', False)
        return v['battery']

    async def _get_device_info(self, query, refresh):
        event = {
            '_type': 'wf_api_get_device_info_request',
            'query': query,
            'refresh': refresh
        }
        v = await self.sendReceive(event)
        return v


    async def set_device_name(self, name):
        await self._set_device_info('name', name)

    async def set_device_channel(self, channel: str):
        await self._set_device_info('channel', channel)

    async def _set_device_info(self, field, value):
        event = {
            '_type': 'wf_api_set_device_info_request',
            'field': field,
            'value': value
        }
        v = await self.sendReceive(event)
        return event


    async def set_led(self, effect: str, args):
        event = {
            '_type': 'wf_api_set_led_request',
            'effect': effect,
            'args': args
        }
        await self.sendReceive(event)

    # convenience functions
    async def set_led_on(self, color):
        await self.set_led('static', {'colors':{'ring': color}})

    async def set_led_rainbow(self, rotations=-1):
        await self.set_led('rainbow', {'rotations': rotations})

    async def set_led_flash(self, color, count=-1):
        await self.set_led('flash', {'colors': {'ring': color}, 'count': count})

    async def set_led_breathe(self, color, count=-1):
        await self.set_led('breathe', {'colors': {'ring': color}, 'count': count})

    async def set_led_rotate(self, color, rotations=-1):
        await self.set_led('rotate', {'colors': {'1': color}, 'rotations': rotations})

    async def set_led_off(self):
        await self.set_led('off', {})


    async def vibrate(self, pattern=None):
        if not pattern:
            pattern = [100, 500, 500, 500, 500, 500]

        event = {
            '_type': 'wf_api_vibrate_request',
            'pattern': pattern
        }
        await self.sendReceive(event)


    async def start_timer(self, timeout: int):
        event = {
            '_type': 'wf_api_start_timer_request',
            'timeout': timeout
        }
        await self.sendReceive(event)

    async def stop_timer(self):
        event = {
            '_type': 'wf_api_stop_timer_request'
        }
        await self.sendReceive(event)


    async def terminate(self):
        event = {
            '_type': 'wf_api_terminate_request'
        }
        await self.sendReceive(event)


    async def create_incident(self, itype):
        event = {
            '_type': 'wf_api_create_incident_request',
            'type': itype
        }
        await self.send(event)

    async def resolve_incident(self):
        event = {
            '_type': 'wf_api_resolve_incident_request'
        }
        await self.send(event)


