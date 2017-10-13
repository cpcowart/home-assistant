"""Module to handle messages from Home Assistant cloud."""
import asyncio
import logging

from aiohttp import hdrs, client_exceptions

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.components.alexa import smart_home
from homeassistant.util.decorator import Registry
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from . import auth_api


HANDLERS = Registry()
_LOGGER = logging.getLogger(__name__)


class CloudIoT:
    """Class to manage the IoT connection."""

    def __init__(self, cloud):
        """Initialize the CloudIoT class."""
        self.cloud = cloud
        self.client = None
        self._remove_hass_stop_listener = None
        self.is_connected = False
        self.close_requested = False
        self.tries = 0

    @asyncio.coroutine
    def connect(self):
        """Connect to the IoT broker."""
        if self.is_connected:
            raise Exception('Cannot connect while already connected')

        self.close_requested = False

        hass = self.cloud.hass

        yield from hass.async_add_job(auth_api.check_token, self.cloud)

        session = async_get_clientsession(self.cloud.hass)
        headers = {
            hdrs.AUTHORIZATION: 'Bearer {}'.format(self.cloud.access_token)
        }

        @asyncio.coroutine
        def _handle_hass_stop(event):
            """Handle Home Assistant shutting down."""
            yield from self.disconnect()

        self._remove_hass_stop_listener = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, _handle_hass_stop)

        client = None

        try:
            self.client = client = yield from session.ws_connect(
                'ws://localhost:8002/websocket', headers=headers)
            self.is_connected = True
            self.tries = 0

            msg = yield from client.receive_json()

            while msg:
                response = {
                    'msgid': msg['msgid'],
                }
                try:
                    result = yield from async_handle_message(
                        hass, self.cloud, msg['handler'], msg['payload'])

                    response['payload'] = result

                except Exception:
                    _LOGGER.exception('Error handling message')
                    response['error'] = True

                _LOGGER.debug('Publishing message: %s', response)
                yield from client.send_json(response)

                msg = yield from client.receive_json()

        except client_exceptions.WSServerHandshakeError as err:
            if err.code == 401:
                _LOGGER.warning('Unable to connect: invalid auth')
                self.close_requested = True
                # TODO notify user?

        except client_exceptions.ClientError as err:
            _LOGGER.warning('Unable to connect: %s', err)

        except ValueError as err:
            print("RECEIVED INVALID JSON", err.doc)

        except TypeError as err:
            if client.closed:
                _LOGGER.warning('Disconnected from server.')
            else:
                _LOGGER.warning('Unknown error: %s', err)

        except Exception:
            if not self.close_requested:
                _LOGGER.exception('Something happened, connection closed?')

        finally:
            self._remove_hass_stop_listener()
            self._remove_hass_stop_listener = None
            if client is not None:
                self.client = None
                yield from client.close()
                self.is_connected = False

            if not self.close_requested:
                self.tries += 1

                # Sleep 0, 5, 10, 15 … up to 30 seconds between retries
                yield from asyncio.sleep(
                    min(30, (self.tries - 1) * 5), loop=hass.loop)

                hass.async_add_job(self.connect())

    @asyncio.coroutine
    def disconnect(self):
        """Disconnect the client."""
        self.close_requested = True
        yield from self.client.close()


@asyncio.coroutine
def async_handle_message(hass, cloud, handler_name, payload):
    """Handle incoming IoT message."""
    handler = HANDLERS.get(handler_name)

    if handler is None:
        _LOGGER.warning('Unable to handle message for %s', handler_name)
        return

    return (yield from handler(hass, cloud, payload))


@HANDLERS.register('alexa')
@asyncio.coroutine
def async_handle_alexa(hass, cloud, payload):
    """Handle an incoming IoT message for Alexa."""
    return (yield from smart_home.async_handle_message(hass, payload))
