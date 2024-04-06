import dataclasses
from typing import Optional
import re
from functools import reduce
from operator import concat
from configparser import ConfigParser
from urllib.parse import urlparse
import logging

from .utils import SentryWrapper

_logger = logging.getLogger('obico.config')

@dataclasses.dataclass
class MoonrakerConfig:
    host: str = '127.0.0.1'
    port: int = 7125
    api_key: Optional[str] = None

    def http_address(self):
        if not self.host or not self.port:
            return None
        return f'http://{self.host}:{self.port}'

    def ws_url(self):
        return f'ws://{self.host}:{self.port}/websocket'


@dataclasses.dataclass
class ServerConfig:
    url: str = 'https://app.obico.io'
    auth_token: Optional[str] = None
    upload_dir: str = ''  # relative to virtual sdcard

    # feedrates for printer control, mm/s
    DEFAULT_FEEDRATE_XY = 100
    DEFAULT_FEEDRATE_Z = 10
    feedrate_xy : int = DEFAULT_FEEDRATE_XY
    feedrate_z : int = DEFAULT_FEEDRATE_Z

    def canonical_endpoint_prefix(self):
        if not self.url:
            return None

        endpoint_prefix = self.url.strip()
        if endpoint_prefix.endswith('/'):
            endpoint_prefix = endpoint_prefix[:-1]

        return endpoint_prefix

    def canonical_ws_prefix(self):
        return re.sub(r'^http', 'ws', self.canonical_endpoint_prefix())

    def ws_url(self):
        return f'{self.canonical_ws_prefix()}/ws/dev/'


@dataclasses.dataclass
class TunnelConfig:
    dest_host: Optional[str]
    dest_port: Optional[str]
    dest_is_ssl: Optional[str]
    url_blacklist: []


@dataclasses.dataclass
class WebcamConfig:

    def __init__(self, webcam_config_section):
        self.webcam_config_section = webcam_config_section
        self.moonraker_webcam_config = {}

    @property
    def snapshot_url(self):

        def guess_snapshot_url_from_stream_url(stream_url):
            if stream_url and '?action=stream' in stream_url:
                return stream_url.replace('?action=stream', '?action=snapshot')
            else:
                return None

        return self.webcam_full_url( \
                self.webcam_config_section.get('snapshot_url') or \
                self.moonraker_webcam_config.get('snapshot_url') or \
                guess_snapshot_url_from_stream_url(
                    self.webcam_config_section.get('stream_url') or self.moonraker_webcam_config.get('stream_url') # Fluidd flavor webcam settings doesn't have snapshot_url. Derive from stream_url instead
                )
            )

    @property
    def disable_video_streaming(self):
        try:
            return self.webcam_config_section.getboolean('disable_video_streaming', False)
        except:
            _logger.warn(f'Invalid disable_video_streaming value. Using default.')
            return False

    def get_target_fps(self, fallback_fps=25):
        try:
            fps = float( self.webcam_config_section.get('target_fps'))
        except:
            fps = fallback_fps
        return min(fps, 30)

    @property
    def snapshot_ssl_validation(self):
        return False

    @property
    def stream_url(self):
        return self.webcam_full_url(self.webcam_config_section.get('stream_url') or self.moonraker_webcam_config.get('stream_url'))

    @property
    def flip_h(self):
        if 'flip_h' in self.webcam_config_section:
            try:
                return self.webcam_config_section.getboolean('flip_h')
            except:
                _logger.warn(f'Invalid flip_h value. Using default.')

        return self.moonraker_webcam_config.get('flip_h')

    @property
    def flip_v(self):
        if 'flip_v' in self.webcam_config_section:
            try:
                return self.webcam_config_section.getboolean('flip_v')
            except:
                _logger.warn(f'Invalid flip_v value. Using default.')

        return self.moonraker_webcam_config.get('flip_v')

    @property
    def rotation(self):
        invalid_value_message = f'Invalid rotation value. Valid values: [0, 90, 180, 270]. Using default.'
        try:
            rotation = self.webcam_config_section.getint('rotation', 0)
            if not rotation in [0, 90, 180, 270]:
                _logger.warn(invalid_value_message)
                return 0
            return rotation
        except:
            _logger.warn(invalid_value_message)
            return 0

    @property
    def aspect_ratio_169(self):
        try:
            return self.webcam_config_section.getboolean('aspect_ratio_169', False)
        except:
            _logger.warn(f'Invalid aspect_ratio_169 value. Using default.')
            return False

    @classmethod
    def webcam_full_url(cls, url):
        if not url or not url.strip():
            return ''

        full_url = url.strip()
        if not urlparse(full_url).scheme:
            full_url = "http://127.0.0.1/" + re.sub(r"^\/", "", full_url)

        return full_url


@dataclasses.dataclass
class LoggingConfig:
    path: str
    level: str = 'DEBUG'


class Config:

    def __init__(self, config_path: str):
        self.moonraker_objects = {
            'heater_mapping': {},
        }

        self._config_path = config_path

    def load_from_config_file(self):
        config = ConfigParser()
        config.read([self._config_path, ])

        self._config = config

        self.moonraker = MoonrakerConfig(
            host=config.get(
                'moonraker', 'host',
                fallback='127.0.0.1'
            ),
            port=config.get(
                'moonraker', 'port',
                fallback=7125
            ),
            api_key=config.get(
                'moonraker', 'api_key',
                fallback=None
            ),
        )

        self.server = ServerConfig(
            url=config.get(
                'server', 'url',
                fallback='https://app.obico.io'),
            auth_token=config.get(
                'server', 'auth_token',
                fallback=None),
            upload_dir=config.get(
                'server', 'upload_dir',
                fallback='Obico_Upload').strip().lstrip('/').rstrip('/'),
            feedrate_xy=config.getint(
                'server', 'feedrate_xy',
                fallback=ServerConfig.DEFAULT_FEEDRATE_XY,
            ),
            feedrate_z=config.getint(
                'server', 'feedrate_z',
                fallback=ServerConfig.DEFAULT_FEEDRATE_Z,
            )
        )

        dest_is_ssl = False
        try:
            dest_is_ssl = config.getboolean('tunnel', 'dest_is_ssl', fallback=False,)
        except:
            _logger.warn(f'Invalid dest_is_ssl value. Using default.')

        self.tunnel = TunnelConfig(
            dest_host=config.get(
                'tunnel', 'dest_host',
                fallback='127.0.0.1',
            ),
            dest_port=config.get(
                'tunnel', 'dest_port',
                fallback='80',
            ),
            dest_is_ssl=dest_is_ssl,
            url_blacklist=[],
        )

        self.webcam = WebcamConfig(webcam_config_section=config['webcam'])

        self.logging = LoggingConfig(
            path=config.get(
                'logging', 'path',
                fallback=''
            ),
            level=config.get(
                'logging', 'level',
                fallback=''
            ),
		)

        self.sentry_opt = config.get(
            'misc', 'sentry_opt',
            fallback='in'
        )

    def get_meta_as_dict(self):
        if self._config.has_section('meta'):
            meta_items = self._config.items('meta')
            return dict(meta_items)
        else:
            return {}


    def write(self) -> None:
        with open(self._config_path, 'w') as f:
            self._config.write(f)

    def update_server_auth_token(self, auth_token: str):
        self.server.auth_token = auth_token
        self._config.set('server', 'auth_token', auth_token)
        self.write()


    def get_mapped_server_heater_name(self, mr_heater_name):
        return self.moonraker_objects['heater_mapping'].get(mr_heater_name)

    def get_mapped_mr_heater_name(self, server_heater_name):
        mr_heater_name = list(self.moonraker_objects['heater_mapping'].keys())[list(self.moonraker_objects['heater_mapping'].values()).index(server_heater_name)]
        return mr_heater_name

    def all_mr_heaters(self):
         return self.moonraker_objects['heater_mapping'].keys()


    # Methods to update config based on Moonraker objects

    def update_moonraker_objects(self, moonraker_conn):
        self.update_heater_mapping(moonraker_conn)
        self.update_webcam_config_from_moonraker(moonraker_conn)

    def update_webcam_config_from_moonraker(self, moonraker_conn):
        def webcams_configured_in_moonraker():
            # TODO: Rotation is not handled correctly

            # Check for the webcam API in the newer Moonraker versions
            result = moonraker_conn.api_get('server.webcams.list', raise_for_status=False)
            if result and len(result.get('webcams', [])) > 0:  # Apparently some Moonraker versions support this endpoint but mistakenly returns an empty list even when webcams are present
                _logger.debug(f'Found config in Moonraker webcams API: {result}')
                webcam_configs = [ dict(
                            snapshot_url = cfg.get('snapshot_url', None),
                            stream_url = cfg.get('stream_url', None),
                            flip_h = cfg.get('flip_horizontal', False),
                            flip_v = cfg.get('flip_vertical', False),
                            rotation = cfg.get('rotation', 0),
                         ) for cfg in result.get('webcams', []) if 'mjpeg' in cfg.get('service', '').lower() ]

                if len(webcam_configs) > 0:
                    return  webcam_configs

                # In case of WebRTC webcam
                webcam_configs = [ dict(
                            snapshot_url = cfg.get('snapshot_url', None),
                            stream_url = cfg.get('snapshot_url', '').replace('action=snapshot', 'action=stream'), # TODO: Webrtc stream_url is not compatible with MJPEG stream url. Let's guess it. it is a little hacky.
                            flip_h = cfg.get('flip_horizontal', False),
                            flip_v = cfg.get('flip_vertical', False),
                            rotation = cfg.get('rotation', 0),
                         ) for cfg in result.get('webcams', []) if 'webrtc' in cfg.get('service', '').lower() ]
                return  webcam_configs

            # Check for the standard namespace for webcams
            result = moonraker_conn.api_get('server.database.item', raise_for_status=False, namespace='webcams')
            if result:
                _logger.debug(f'Found config in Moonraker webcams namespace: {result}')
                return [ dict(
                            snapshot_url = cfg.get('urlSnapshot', None),
                            stream_url = cfg.get('urlStream', None),
                            flip_h = cfg.get('flipX', False),
                            flip_v = cfg.get('flipY', False),
                            rotation = cfg.get('rotation', 0), # TODO Verify the key name for rotation
                        ) for cfg in result.get('value', {}).values() if 'mjpeg' in cfg.get('service', '').lower() ]

            # webcam configs not found in the standard location. Try fluidd's flavor
            result = moonraker_conn.api_get('server.database.item', raise_for_status=False, namespace='fluidd', key='cameras')
            if result:
                _logger.debug(f'Found config in Moonraker fluidd/cameras namespace: {result}')
                return [ dict(
                            stream_url = cfg.get('url', None),
                            flip_h = cfg.get('flipX', False),
                            flip_v = cfg.get('flipY', False),
                            rotation = cfg.get('rotation', 0), # TODO Verify the key name for rotation
                        ) for cfg in result.get('value', {}).get('cameras', []) if cfg.get('enabled', False) ]

            #TODO: Send notification to user that webcam configs not found when moonraker's announcement api makes to stable
            return []

        mr_webcam_config = webcams_configured_in_moonraker()

        if len(mr_webcam_config) > 0:
            _logger.debug(f'Retrieved webcam config from Moonraker: {mr_webcam_config[0]}')
            self.webcam.moonraker_webcam_config = mr_webcam_config[0]

            # Add all webcam urls to the blacklist so that they won't be tunnelled
            url_list = [[ cfg.get('snapshot_url', None), cfg.get('stream_url', None) ] for cfg in mr_webcam_config ]
            self.tunnel.url_blacklist = [ url for url in reduce(concat, url_list) if url ]
        else:
            #TODO: Send notification to user that webcam configs not found when moonraker's announcement api makes to stable
            pass

    # Adopted from getHeaters, getTemperatureObjects, getTemperatureSensors in mainsail:/src/store/printer/getters.ts
    def update_heater_mapping(self, moonraker_conn):
        def capwords(s):
            return ' '.join(elem.capitalize() for elem in s.split(' '))

        heaters = moonraker_conn.find_all_heaters()  # We need to find all heaters as their names have to be specified in the objects query request

        for heater in sorted(heaters.get('available_heaters', [])):
            name = heater
            name_split = name.split(' ')
            if len(name_split) > 1 and name_split[0] == 'heater_generic':
                name = name_split[1]

            if name.startswith('_'):
                continue

            self.moonraker_objects['heater_mapping'][heater] = name

        for sensor in sorted(heaters.get('available_sensors', [])):
            name_split = sensor.split(' ')
            if len(name_split) > 1 and name_split[0] == 'temperature_sensor' and not name_split[1].startswith('_'):
                self.moonraker_objects['heater_mapping'][sensor] = name_split[1]

